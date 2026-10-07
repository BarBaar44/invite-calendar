"""Coordinator: one poll cycle per entry.

Same order as the pyscript app (reference/calendar_mailbox.py _poll_one):

1. fetch unflagged messages (IMAP, executor)
2. load the store, snapshot
3. apply each message (REQUEST/CANCEL, validated first; only managed
   events, only by their own organizer); a message that raises is retried
   up to MAX_MESSAGE_ATTEMPTS, then given up and flagged
4. retention prune
5. per UID diff save; on a store error nothing is flagged and nothing is
   committed, so the next poll retries
6. flag processed messages
7. commit state (organizer, failed), forget cancelled and pruned UIDs
8. policy if_free: accept or decline (see freebusy.py); declined events and
   occurrences are taken out of the calendar with a second save
9. missing location replies for new or changed invites (option)
10. RSVP scan per accept policy; "accepted" written after a confirmed send
11. fire invite_calendar_updated / invite_calendar_invite_received

Declines made with the decline_event service are recorded in the same
state.declined as policy if_free, so step 3 keeps them out of the calendar
under every policy.

Replies are sent only after the save succeeded, so a message that is
retried never produces a second reply.

Steps 3 and 4 run as one executor job on a COPY of the state, so a failed
save leaves the committed state untouched.

One asyncio.Lock per entry wraps the whole cycle; the services take the
same lock, so load/modify/save and sends never interleave.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import email
import logging
from dataclasses import dataclass, field
from email.message import Message

from homeassistant.components import persistent_notification
from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from icalendar import Calendar, Event

from . import freebusy, issues
from .const import (
    ACCEPT_IF_FREE,
    DOMAIN,
    EVENT_INVITE_RECEIVED,
    EVENT_UPDATED,
    ISSUE_AFTER,
    MAX_MESSAGE_ATTEMPTS,
)
from .ical import events
from .ical.imip import ReceivedInvite, apply_message
from .mail import imap, smtp
from .options import EntryOptions
from .outbound import (
    EventFields,
    OutboundError,
    cancel_occurrence_own,
    cancel_series,
    describe,
    is_occurrence,
    new_event,
    parse_recurrence_id,
    update_occurrence,
    update_series,
)
from .replies import rsvp_candidates
from .state import EntryState, StateStore
from .store import Diff, StoreAuthError, StoreBackend, StoreError


def merge_diffs(first: Diff, second: Diff) -> Diff:
    """Net effect of two saves in a row (e.g. an invite added, then left out
    again because it was declined: no change at all)."""
    removed2 = set(second.removed)
    added = (set(first.added) - removed2) | (set(second.added) - set(first.removed))
    updated = (set(first.updated) | set(second.updated)) - added - removed2
    updated |= set(second.added) & set(first.removed)
    removed = (set(first.removed) - set(second.added)) | (removed2 - set(first.added))
    return Diff(added=sorted(added), updated=sorted(updated), removed=sorted(removed))


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessOutcome:
    """Result of applying a batch of messages to a calendar."""

    to_flag: list[str] = field(default_factory=list)
    gave_up: list[str] = field(default_factory=list)
    received: list[ReceivedInvite] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)


def process_messages(
    name: str,
    messages: list[tuple[str, bytes]],
    cal: Calendar,
    state: EntryState,
    retention_cutoff: datetime.datetime | None,
    managed_only: bool = False,
    own_address: str | None = None,
) -> ProcessOutcome:
    """Apply messages to `cal` and update `state` (both mutated in place).

    Pure and synchronous: runs in the executor and in tests. `managed_only`
    (shared stores) limits retention to UIDs that came in by mail; events
    people created in the calendar directly are never pruned.
    """
    outcome = ProcessOutcome()

    for imap_uid, raw in messages:
        msg: Message = email.message_from_bytes(raw)
        msg_id = str(msg.get("Message-ID") or f"imap-uid-{imap_uid}")
        try:
            result = apply_message(msg, cal, name, own_address, state.organizer)
        except Exception as err:  # noqa: BLE001 - any failure: retry or give up
            attempts = state.failed.get(msg_id, 0) + 1
            if attempts >= MAX_MESSAGE_ATTEMPTS:
                _LOGGER.error(
                    "[%s] giving up on message %s after %s attempts: %s",
                    name,
                    msg_id,
                    attempts,
                    err,
                )
                state.failed.pop(msg_id, None)
                outcome.to_flag.append(imap_uid)
                outcome.gave_up.append(msg_id)
            else:
                state.failed[msg_id] = attempts
                _LOGGER.warning(
                    "[%s] message %s failed (%s/%s), will retry: %s",
                    name,
                    msg_id,
                    attempts,
                    MAX_MESSAGE_ATTEMPTS,
                    err,
                )
            continue

        outcome.to_flag.append(imap_uid)
        state.failed.pop(msg_id, None)
        state.organizer.update(result.organizers)
        state.forget(result.cancelled)
        outcome.received.extend(result.received)

    # if_free: a resent copy of a declined invitation stays out, a declined
    # occurrence keeps its EXDATE (also catches up after a failed save).
    freebusy.apply_declines(cal, state)

    if retention_cutoff is not None:
        only = set(state.organizer) if managed_only else None
        outcome.pruned = events.prune_past_events(cal, retention_cutoff, only)
        if outcome.pruned:
            _LOGGER.info("[%s] pruned %s past event(s)", name, len(outcome.pruned))

    state.prune_to(events.all_uids(cal))
    return outcome


class InviteCalendarCoordinator(DataUpdateCoordinator[Calendar]):
    """Polls one mailbox into one calendar store."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        imap_settings: imap.ImapSettings,
        store: StoreBackend,
        options: EntryOptions,
    ) -> None:
        """One coordinator per config entry."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=options.scan_interval,
        )
        self.imap_settings = imap_settings
        self.store = store
        self.options = options
        self.retention_days = options.retention_days
        self.lock = asyncio.Lock()
        self._state_store = StateStore(hass, entry.entry_id)
        self.state = EntryState()
        # Since when the mailbox / the store keep failing (Repairs issue
        # after ISSUE_AFTER); None while they work.
        self._imap_failing_since: datetime.datetime | None = None
        self._store_failing_since: datetime.datetime | None = None

    async def _async_setup(self) -> None:
        """Load persisted state once, before the first refresh."""
        self.state = await self._state_store.async_load()

    async def _async_update_data(self) -> Calendar:
        async with self.lock:
            return await self._async_poll()

    async def _async_fetch(self) -> list[tuple[str, bytes]]:
        """Unprocessed messages; [] when the mailbox is unreachable, so the
        calendar keeps working from the store."""
        try:
            messages = await self.hass.async_add_executor_job(
                imap.fetch_unprocessed, self.imap_settings
            )
        except imap.ImapAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="imap_auth_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except imap.ImapError as err:
            now = dt_util.utcnow()
            if self._imap_failing_since is None:
                _LOGGER.warning(
                    "[%s] IMAP fetch failed, retrying every poll: %s",
                    self.config_entry.title,
                    err,
                )
                self._imap_failing_since = now
            elif now - self._imap_failing_since >= ISSUE_AFTER:
                issues.raise_issue(
                    self.hass,
                    self.config_entry.entry_id,
                    issues.IMAP_UNREACHABLE,
                    {
                        "name": self.config_entry.title,
                        "host": self.imap_settings.host,
                        "error": str(err),
                    },
                )
            return []
        if self._imap_failing_since is not None:
            _LOGGER.info("[%s] IMAP reachable again", self.config_entry.title)
            self._imap_failing_since = None
        issues.clear_issue(
            self.hass, self.config_entry.entry_id, issues.IMAP_UNREACHABLE
        )
        return messages

    def _note_store_failure(self, err: StoreError) -> None:
        """A store that keeps failing becomes a Repairs issue. Rejected
        credentials start reauth instead."""
        if isinstance(err, StoreAuthError):
            return
        now = dt_util.utcnow()
        if self._store_failing_since is None:
            self._store_failing_since = now
        elif now - self._store_failing_since >= ISSUE_AFTER:
            issues.raise_issue(
                self.hass,
                self.config_entry.entry_id,
                issues.STORE_UNREACHABLE,
                {
                    "name": self.config_entry.title,
                    "store": self.store.describe(),
                    "error": str(err),
                },
            )

    def _note_store_ok(self) -> None:
        self._store_failing_since = None
        issues.clear_issue(
            self.hass, self.config_entry.entry_id, issues.STORE_UNREACHABLE
        )

    async def _async_poll(self) -> Calendar:
        name = self.config_entry.title
        messages = await self._async_fetch()

        try:
            cal, snapshot = await self.store.async_load()
        except StoreError as err:
            self._note_store_failure(err)
            raise self._store_failed(err, "store_read_failed") from err

        new_state = copy.deepcopy(self.state)
        cutoff = (
            dt_util.now() - datetime.timedelta(days=self.retention_days)
            if self.retention_days
            else None
        )
        outcome = await self.hass.async_add_executor_job(
            process_messages,
            name,
            messages,
            cal,
            new_state,
            cutoff,
            self.store.shared,
            self.options.address,
        )

        try:
            changes: Diff = await self.store.async_save(cal, snapshot)
        except StoreError as err:
            # Nothing flagged, nothing committed: the next poll applies the
            # same messages again, which is idempotent.
            self._note_store_failure(err)
            raise self._store_failed(err, "store_write_failed") from err
        self._note_store_ok()

        if outcome.to_flag:
            try:
                await self.hass.async_add_executor_job(
                    imap.mark_processed, self.imap_settings, outcome.to_flag
                )
            except imap.ImapError as err:
                _LOGGER.warning(
                    "[%s] could not flag %s message(s), they will be applied "
                    "again next poll: %s",
                    name,
                    len(outcome.to_flag),
                    err,
                )

        if new_state.as_dict() != self.state.as_dict():
            self.state = new_state
            await self._state_store.async_save(self.state)

        for msg_id in outcome.gave_up:
            persistent_notification.async_create(
                self.hass,
                f"An email in {self.imap_settings.username} ({msg_id}) could not "
                "be processed and has been skipped. Check the Home Assistant log.",
                title=f"{name}: unreadable invitation",
                notification_id=f"{DOMAIN}_{self.config_entry.entry_id}_bad_message",
            )

        if self.options.accept_policy == ACCEPT_IF_FREE:
            cal, changes = await self._async_free_scan(cal, changes)
        await self._async_missing_location_replies(changes, outcome)
        await self._async_rsvp_scan(cal)
        await self._async_resend_pending(cal)
        self._fire_events(changes, outcome)
        return cal

    # ---------------------------------------------------------------- replies

    async def _async_send(self, build, *args) -> str | None:
        """Build and send one message in the executor; its Message-ID.

        A rejected SMTP login or a refused From: address is a setup problem,
        not a problem with this message: it becomes a Repairs issue, cleared
        by the next message that goes out."""

        def job() -> str | None:
            msg = build(self.options.smtp, *args)
            smtp.send(self.options.smtp, msg)
            return msg["Message-ID"]

        entry_id = self.config_entry.entry_id
        placeholders = {
            "name": self.config_entry.title,
            "host": self.options.smtp.host,
            "username": self.options.smtp.username,
            "address": self.options.address,
        }
        try:
            msg_id = await self.hass.async_add_executor_job(job)
        except smtp.SmtpAuthError as err:
            issues.raise_issue(
                self.hass,
                entry_id,
                issues.SMTP_AUTH_FAILED,
                {**placeholders, "error": str(err)},
            )
            raise
        except smtp.SmtpSenderRefusedError as err:
            issues.raise_issue(
                self.hass,
                entry_id,
                issues.SMTP_SENDER_REFUSED,
                {**placeholders, "error": str(err)},
            )
            raise
        issues.clear_issue(self.hass, entry_id, issues.SMTP_AUTH_FAILED)
        issues.clear_issue(self.hass, entry_id, issues.SMTP_SENDER_REFUSED)
        return msg_id

    async def _async_missing_location_replies(
        self, changes: Diff, outcome: ProcessOutcome
    ) -> None:
        """Ask organizers of new or changed invites without a LOCATION."""
        opts = self.options
        if not opts.missing_location_reply:
            return
        changed = set(changes.changed)
        own = opts.address.lower()
        for invite in outcome.received:
            if invite.uid not in changed or invite.location:
                continue
            if not invite.organizer or invite.organizer.lower() == own:
                continue
            try:
                await self._async_send(
                    smtp.build_missing_location_reply,
                    opts.from_name,
                    opts.address,
                    invite.organizer,
                    smtp.OriginalMessage(
                        invite.message_id, invite.subject, invite.references
                    ),
                    invite.summary or "the event",
                    smtp.human_start(invite.start),
                    opts.missing_location_text,
                )
            except smtp.SmtpError as err:
                _LOGGER.warning(
                    "[%s] missing location reply to %s not sent: %s",
                    self.config_entry.title,
                    invite.organizer,
                    err,
                )
            else:
                _LOGGER.info(
                    "[%s] asked %s for a location for %s",
                    self.config_entry.title,
                    invite.organizer,
                    invite.uid,
                )

    async def _async_send_accept(self, component, partstat: str = "ACCEPTED") -> None:
        opts = self.options
        await self._async_send(
            smtp.build_accept_reply,
            opts.from_name,
            opts.address,
            opts.attendee_cn,
            events.get_organizer_email(component),
            component,
            events.PRODID,
            None,
            partstat,
        )

    async def _async_free_scan(
        self, cal: Calendar, changes: Diff
    ) -> tuple[Calendar, Diff]:
        """Policy if_free: accept what is free, decline what clashes, then
        take the declined events and occurrences out of the calendar.

        Each reply is recorded only after the mail server took it. A
        transient send failure stops the scan; the next poll decides again.
        DECLINED replies for single occurrences are queued in the state
        (`unsent`) and retried every poll, because the series itself is
        already accepted and won't be decided again."""
        name = self.config_entry.title
        decisions = await self.hass.async_add_executor_job(
            freebusy.decide, cal, self.state, self.options.address, dt_util.now()
        )
        dirty = False
        touched = False
        for decision in decisions:
            uid, seq = decision.uid, decision.sequence
            if decision.decline_whole:
                try:
                    await self._async_send_accept(decision.component, "DECLINED")
                except smtp.SmtpRefusedError as err:
                    _LOGGER.error(
                        "[%s] decline for %s refused, leaving it out anyway: %s",
                        name,
                        uid,
                        err,
                    )
                except smtp.SmtpError as err:
                    _LOGGER.warning(
                        "[%s] decline for %s not sent, retrying next poll: %s",
                        name,
                        uid,
                        err,
                    )
                    break
                end = freebusy.event_end(decision.component)
                self.state.declined[uid] = {
                    "sequence": seq,
                    "whole": True,
                    "until": end.timestamp() if end is not None else None,
                }
                dirty = touched = True
                _LOGGER.info(
                    "[%s] declined %s (sequence %s): time taken", name, uid, seq
                )
                continue

            try:
                await self._async_send_accept(decision.component)
            except smtp.SmtpRefusedError as err:
                _LOGGER.error(
                    "[%s] RSVP for %s refused, not retrying: %s", name, uid, err
                )
                self.state.rsvp_failed[uid] = seq
                dirty = True
                continue
            except smtp.SmtpError as err:
                _LOGGER.warning(
                    "[%s] RSVP for %s not sent, retrying next poll: %s", name, uid, err
                )
                break
            self.state.accepted[uid] = seq
            self.state.rsvp_failed.pop(uid, None)
            dirty = True
            _LOGGER.info("[%s] accepted %s (sequence %s)", name, uid, seq)
            # A record from an earlier version no longer applies.
            self.state.declined.pop(uid, None)
            if decision.declined_occurrences:
                isos = [freebusy.rid_iso(rid) for rid in decision.declined_occurrences]
                self.state.declined[uid] = {
                    "sequence": seq,
                    "whole": False,
                    "occurrences": isos,
                    "unsent": list(isos),
                }
                touched = True
                _LOGGER.info(
                    "[%s] declining %s occurrence(s) of %s: time taken",
                    name,
                    len(isos),
                    uid,
                )
        if dirty:
            await self._state_store.async_save(self.state)

        if touched:
            try:
                fresh, snapshot = await self.store.async_load()
                await self.hass.async_add_executor_job(
                    freebusy.apply_declines, fresh, self.state
                )
                second = await self.store.async_save(fresh, snapshot)
            except StoreError as err:
                _LOGGER.warning(
                    "[%s] declined events stay in the calendar until the next "
                    "poll, store failed: %s",
                    name,
                    err,
                )
            else:
                cal, changes = fresh, merge_diffs(changes, second)

        await self._async_send_unsent_declines(cal)
        return cal, changes

    async def _async_send_unsent_declines(self, cal: Calendar) -> None:
        """DECLINED replies for single occurrences that did not go out yet."""
        name = self.config_entry.title
        dirty = False
        for uid, record in self.state.declined.items():
            if not record.get("unsent"):
                continue
            master = events.find_event(cal, uid)
            if master is None or int(master.get("SEQUENCE", 0)) != record["sequence"]:
                record["unsent"] = []  # gone or a newer version: decided again
                dirty = True
                continue
            for iso in list(record["unsent"]):
                rid = freebusy.rid_from_iso(iso)
                try:
                    await self._async_send_accept(
                        freebusy.occurrence_stub(master, rid), "DECLINED"
                    )
                except smtp.SmtpRefusedError as err:
                    _LOGGER.error(
                        "[%s] decline for %s on %s refused: %s", name, uid, iso, err
                    )
                except smtp.SmtpError as err:
                    _LOGGER.warning(
                        "[%s] decline for %s on %s not sent, retrying next poll: %s",
                        name,
                        uid,
                        iso,
                        err,
                    )
                    if dirty:
                        await self._state_store.async_save(self.state)
                    return
                record["unsent"].remove(iso)
                dirty = True
        if dirty:
            await self._state_store.async_save(self.state)

    async def _async_rsvp_scan(self, cal: Calendar) -> None:
        """Accept every managed event the policy allows, once per sequence."""
        opts = self.options
        candidates = rsvp_candidates(
            cal, self.state, opts.accept_policy, opts.address, dt_util.now()
        )
        if not candidates:
            return
        name = self.config_entry.title
        dirty = False
        for component in candidates:
            uid = str(component.get("UID"))
            seq = int(component.get("SEQUENCE", 0))
            try:
                await self._async_send_accept(component)
            except smtp.SmtpRefusedError as err:
                # Permanent for this sequence; an updated invite tries again.
                _LOGGER.error(
                    "[%s] RSVP for %s refused, not retrying: %s", name, uid, err
                )
                self.state.rsvp_failed[uid] = seq
                dirty = True
                continue
            except smtp.SmtpError as err:
                # Server down or login rejected: the rest would fail too.
                _LOGGER.warning(
                    "[%s] RSVP for %s not sent, retrying next poll: %s", name, uid, err
                )
                break
            self.state.accepted[uid] = seq
            self.state.rsvp_failed.pop(uid, None)
            dirty = True
            _LOGGER.info("[%s] accepted %s (sequence %s)", name, uid, seq)
        if dirty:
            await self._state_store.async_save(self.state)

    # --------------------------------------------------------------- services

    def _answerable(self, uid: str) -> Event:
        """The component an RSVP for `uid` answers: a managed event from
        someone else. Raises ServiceValidationError otherwise."""
        cal = self.data
        component = events.find_event(cal, uid) if cal is not None else None
        if component is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="event_not_found",
                translation_placeholders={"uid": uid},
            )
        if uid not in self.state.organizer:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="event_not_managed",
                translation_placeholders={"uid": uid},
            )
        organizer = events.get_organizer_email(component)
        if not organizer or organizer.lower() == self.options.address.lower():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="event_no_organizer",
                translation_placeholders={"uid": uid},
            )
        return component

    async def _async_send_reply_or_fail(self, component, partstat: str) -> None:
        """Send an RSVP for a service call; any send failure fails the call."""
        try:
            await self._async_send_accept(component, partstat)
        except smtp.SmtpError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_accept(self, uid: str) -> dict[str, object]:
        """Service accept_event: RSVP ACCEPTED for one managed event, under
        any policy. Already accepted at this SEQUENCE: nothing is sent."""
        async with self.lock:
            component = self._answerable(uid)
            seq = int(component.get("SEQUENCE", 0))
            if self.state.accepted.get(uid) == seq:
                return {"uid": uid, "sequence": seq, "sent": False}
            await self._async_send_reply_or_fail(component, "ACCEPTED")
            self.state.accepted[uid] = seq
            self.state.rsvp_failed.pop(uid, None)
            await self._state_store.async_save(self.state)
            _LOGGER.info(
                "[%s] accepted %s (sequence %s) on request",
                self.config_entry.title,
                uid,
                seq,
            )
            return {"uid": uid, "sequence": seq, "sent": True}

    async def async_decline(
        self, uid: str, recurrence_id: str | None = None
    ) -> dict[str, object]:
        """Service decline_event: RSVP DECLINED for one managed event, or for
        one occurrence of a series, under any policy; then leave it out of
        the calendar, the same way policy if_free does (state.declined).

        The reply goes out first: if it can't be sent, nothing changes and
        the call fails. An earlier acceptance does not stop a decline; a new
        version from the organizer (higher SEQUENCE) is decided again.
        Already declined at this SEQUENCE: nothing is sent."""
        async with self.lock:
            name = self.config_entry.title
            if not recurrence_id:
                record = self.state.declined.get(uid)
                cal = self.data
                if (
                    record
                    and record.get("whole")
                    and (cal is None or events.find_event(cal, uid) is None)
                ):
                    return {
                        "uid": uid,
                        "sequence": int(record["sequence"]),
                        "sent": False,
                    }
            component = self._answerable(uid)
            seq = int(component.get("SEQUENCE", 0))
            result: dict[str, object] = {"uid": uid, "sequence": seq}

            if recurrence_id:
                result["recurrence_id"] = recurrence_id
                record = self.state.declined.get(uid)
                if (
                    record is None
                    or record.get("whole")
                    or int(record.get("sequence", -1)) != seq
                ):
                    record = None
                done = record.get("occurrences", []) if record else []
                iso = self._declinable_occurrence(component, uid, recurrence_id, done)
                if iso in done:
                    return {**result, "sent": False}
                if record is None:
                    record = {
                        "sequence": seq,
                        "whole": False,
                        "occurrences": [],
                        "unsent": [],
                    }
                await self._async_send_reply_or_fail(
                    freebusy.occurrence_stub(component, freebusy.rid_from_iso(iso)),
                    "DECLINED",
                )
                record.setdefault("occurrences", []).append(iso)
                self.state.declined[uid] = record
                _LOGGER.info(
                    "[%s] declined %s on %s (sequence %s) on request",
                    name,
                    uid,
                    iso,
                    seq,
                )
            else:
                await self._async_send_reply_or_fail(component, "DECLINED")
                end = freebusy.event_end(component)
                self.state.declined[uid] = {
                    "sequence": seq,
                    "whole": True,
                    "until": end.timestamp() if end is not None else None,
                }
                self.state.accepted.pop(uid, None)
                self.state.rsvp_failed.pop(uid, None)
                _LOGGER.info(
                    "[%s] declined %s (sequence %s) on request", name, uid, seq
                )
            await self._state_store.async_save(self.state)

            # Take it out of the calendar. A store failure doesn't fail the
            # call: the reply went out and is recorded, and every poll applies
            # the recorded declines again.
            try:
                cal, snapshot = await self.store.async_load()
                await self.hass.async_add_executor_job(
                    freebusy.apply_declines, cal, self.state
                )
                changes = await self.store.async_save(cal, snapshot)
            except StoreError as err:
                _LOGGER.warning(
                    "[%s] declined %s stays in the calendar until the next poll, "
                    "store failed: %s",
                    name,
                    uid,
                    err,
                )
            else:
                self._after_edit(cal, changes)
            return {**result, "sent": True}

    def _declinable_occurrence(
        self, master: Event, uid: str, recurrence_id: str, declined: list[str]
    ) -> str:
        """The occurrence `recurrence_id` of the series `master` as recorded
        in state.declined (freebusy.rid_iso). Raises ServiceValidationError
        when the event doesn't repeat or has no such occurrence; one in
        `declined` (already declined, so EXDATEd) counts as existing."""
        if not events.is_recurring(master) or events.recurrence_key(master) is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_recurring",
                translation_placeholders={"uid": uid},
            )
        cal = self.data
        try:
            rid = parse_recurrence_id(recurrence_id, master)
        except OutboundError as err:
            raise self._invalid(err) from err
        iso = freebusy.rid_iso(rid)
        if iso in declined:
            return iso
        key = int(events.aware(rid).timestamp())
        override = any(
            c.name == "VEVENT"
            and str(c.get("UID")) == uid
            and events.recurrence_key(c) == key
            for c in cal.subcomponents
        )
        if not override and not is_occurrence(cal, uid, rid):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_recurrence_id",
                translation_placeholders={"recurrence_id": recurrence_id},
            )
        return iso

    def _store_failed(self, err: StoreError, key: str) -> Exception:
        """The exception to raise for a store failure: reauth on rejected
        credentials, otherwise a failed update (entity keeps old data)."""
        placeholders = {"store": self.store.describe(), "error": str(err)}
        if isinstance(err, StoreAuthError):
            return ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="store_auth_failed",
                translation_placeholders=placeholders,
            )
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders,
        )

    def _fire_events(self, changes: Diff, outcome: ProcessOutcome) -> None:
        entity_id = er.async_get(self.hass).async_get_entity_id(
            CALENDAR_DOMAIN, DOMAIN, self.config_entry.entry_id
        )
        changed = set(changes.changed)
        for invite in outcome.received:
            if invite.uid not in changed:
                continue
            start = invite.start.isoformat() if invite.start is not None else None
            self.hass.bus.async_fire(
                EVENT_INVITE_RECEIVED,
                {
                    "entity_id": entity_id,
                    "uid": invite.uid,
                    "organizer": invite.organizer,
                    "summary": invite.summary,
                    "start": start,
                    "location": invite.location,
                },
            )
        if changes:
            self.hass.bus.async_fire(
                EVENT_UPDATED,
                {
                    "entity_id": entity_id,
                    "added": changes.added,
                    "updated": changes.updated,
                    "removed": changes.removed,
                },
            )

    # ------------------------------------------------------ own events

    def _invalid(self, err: OutboundError) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders=err.placeholders or None,
        )

    def _store_error(self, err: StoreError) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="store_write_failed",
            translation_placeholders={
                "store": self.store.describe(),
                "error": str(err),
            },
        )

    async def _async_load_for_edit(self) -> tuple[Calendar, object]:
        try:
            return await self.store.async_load()
        except StoreError as err:
            raise self._store_error(err) from err

    async def _async_save_edit(self, cal: Calendar, snapshot) -> Diff:
        def prepare() -> None:
            try:
                cal.add_missing_timezones()
            except Exception:  # noqa: BLE001 - a missing VTIMEZONE is not fatal
                _LOGGER.debug("Could not add missing timezones", exc_info=True)

        await self.hass.async_add_executor_job(prepare)
        try:
            return await self.store.async_save(cal, snapshot)
        except StoreError as err:
            raise self._store_error(err) from err

    async def _async_send_invite(
        self,
        method: str,
        components: list,
        to: list[str],
        subject: str,
        uid: str,
    ) -> str | None:
        opts = self.options
        master = components[0]
        start = master.get("DTSTART")
        body = describe(master, smtp.human_start(start.dt if start else None))
        return await self._async_send(
            smtp.build_invite,
            opts.from_name,
            opts.address,
            to,
            components,
            method,
            subject,
            body,
            self.state.sent.get(uid),
        )

    async def _async_request(
        self, uid: str, components: list, seq: int, prefix: str
    ) -> bool:
        """Send the REQUEST for an own event; on failure mark it pending
        (resent next poll). True when sent or nobody to send to."""
        attendees = events.get_attendee_emails(components[0])
        if not attendees:
            self.state.pending.pop(uid, None)
            return True
        summary = str(components[0].get("SUMMARY", ""))
        start = components[0].get("DTSTART")
        when = smtp.human_start(start.dt if start else None)
        try:
            msg_id = await self._async_send_invite(
                "REQUEST", components, attendees, f"{prefix}{summary} @ {when}", uid
            )
        except smtp.SmtpError as err:
            _LOGGER.warning(
                "[%s] invitation for %s not sent, retrying next poll: %s",
                self.config_entry.title,
                uid,
                err,
            )
            self.state.pending[uid] = seq
            return False
        self.state.pending.pop(uid, None)
        self.state.sent.setdefault(uid, msg_id or "")
        return True

    async def _async_resend_pending(self, cal: Calendar) -> None:
        """REQUESTs that failed earlier, for the version still stored."""
        if not self.state.pending:
            return
        dirty = False
        for uid, seq in list(self.state.pending.items()):
            comps = sorted(
                (c for c in cal.walk("VEVENT") if str(c.get("UID")) == uid),
                key=lambda c: events.recurrence_key(c) is not None,
            )
            if not comps or int(comps[0].get("SEQUENCE", 0)) != seq:
                self.state.pending.pop(uid)
                dirty = True
                continue
            dirty = True
            if not await self._async_request(uid, comps, seq, "Invitation: "):
                break
        if dirty:
            await self._state_store.async_save(self.state)

    def _after_edit(self, cal: Calendar, changes: Diff) -> None:
        self.async_set_updated_data(cal)
        self._fire_events(changes, ProcessOutcome())

    async def async_create(self, fields: EventFields) -> dict[str, object]:
        """Service create_event."""
        async with self.lock:
            opts = self.options
            try:
                event = new_event(fields, opts.address, opts.from_name)
            except OutboundError as err:
                raise self._invalid(err) from err
            uid = str(event["UID"])
            cal, snapshot = await self._async_load_for_edit()
            cal.add_component(event)
            changes = await self._async_save_edit(cal, snapshot)
            self.state.organizer[uid] = opts.address.lower()
            ok = await self._async_request(uid, [event], 0, "Invitation: ")
            await self._state_store.async_save(self.state)
            self._after_edit(cal, changes)
            _LOGGER.info("[%s] created %s", self.config_entry.title, uid)
            return {
                "uid": uid,
                "invited": events.get_attendee_emails(event),
                "pending": not ok,
            }

    async def async_update(
        self, uid: str, recurrence_id: str | None, fields: EventFields
    ) -> dict[str, object]:
        """Service update_event: the whole event/series, or one occurrence."""
        async with self.lock:
            opts = self.options
            cal, snapshot = await self._async_load_for_edit()
            try:
                if recurrence_id:
                    plan = update_occurrence(
                        cal, uid, recurrence_id, fields, opts.address
                    )
                else:
                    plan = update_series(cal, uid, fields, opts.address)
            except OutboundError as err:
                raise self._invalid(err) from err
            changes = await self._async_save_edit(cal, snapshot)
            ok = await self._async_request(
                uid, plan.request, plan.sequence, "Updated invitation: "
            )
            if plan.removed_attendees:
                try:
                    await self._async_send_invite(
                        "CANCEL",
                        plan.cancel,
                        plan.removed_attendees,
                        f"Cancelled: {plan.cancel[0].get('SUMMARY', '')}",
                        uid,
                    )
                except smtp.SmtpError as err:
                    _LOGGER.warning(
                        "[%s] cancellation for removed attendees of %s not sent: %s",
                        self.config_entry.title,
                        uid,
                        err,
                    )
            await self._state_store.async_save(self.state)
            self._after_edit(cal, changes)
            return {
                "uid": uid,
                "sequence": plan.sequence,
                "invited": events.get_attendee_emails(plan.request[0]),
                "removed": plan.removed_attendees,
                "pending": not ok,
            }

    async def async_cancel(
        self, uid: str, recurrence_id: str | None
    ) -> dict[str, object]:
        """Service cancel_event. The CANCEL goes out BEFORE the calendar
        changes: if it can't be sent, nothing changes and the call fails, so
        attendees are never left with an event that no longer exists here."""
        async with self.lock:
            opts = self.options
            cal, snapshot = await self._async_load_for_edit()
            try:
                if recurrence_id:
                    stubs = cancel_occurrence_own(cal, uid, recurrence_id, opts.address)
                else:
                    stubs = cancel_series(cal, uid, opts.address)
            except OutboundError as err:
                raise self._invalid(err) from err
            attendees = events.get_attendee_emails(stubs[0])
            if attendees:
                try:
                    await self._async_send_invite(
                        "CANCEL",
                        stubs,
                        attendees,
                        f"Cancelled: {stubs[0].get('SUMMARY', '')}",
                        uid,
                    )
                except smtp.SmtpError as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="send_failed",
                        translation_placeholders={"error": str(err)},
                    ) from err
            changes = await self._async_save_edit(cal, snapshot)
            if not recurrence_id:
                self.state.forget([uid])
            await self._state_store.async_save(self.state)
            self._after_edit(cal, changes)
            return {"uid": uid, "notified": attendees}
