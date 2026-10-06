"""Coordinator: one poll cycle per entry.

Same order as the pyscript app (reference/calendar_mailbox.py _poll_one):

1. fetch unflagged messages (IMAP, executor)
2. load the store, snapshot
3. apply each message (REQUEST/CANCEL, validated first); a message that
   raises is retried up to MAX_MESSAGE_ATTEMPTS, then given up and flagged
4. retention prune
5. per UID diff save; on a store error nothing is flagged and nothing is
   committed, so the next poll retries
6. flag processed messages
7. commit state (organizer, failed), forget cancelled and pruned UIDs
8. missing location replies for new or changed invites (option)
9. RSVP scan per accept policy; "accepted" written after a confirmed send
10. fire invite_calendar_updated / invite_calendar_invite_received

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
from icalendar import Calendar

from .const import (
    DOMAIN,
    EVENT_INVITE_RECEIVED,
    EVENT_UPDATED,
    MAX_MESSAGE_ATTEMPTS,
)
from .ical import events
from .ical.imip import ReceivedInvite, apply_message
from .mail import imap, smtp
from .options import EntryOptions
from .replies import rsvp_candidates
from .state import EntryState, StateStore
from .store import Diff, StoreAuthError, StoreBackend, StoreError

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
            result = apply_message(msg, cal, name)
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
        self._imap_failing = False

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
            if not self._imap_failing:
                _LOGGER.warning(
                    "[%s] IMAP fetch failed, retrying every poll: %s",
                    self.config_entry.title,
                    err,
                )
            self._imap_failing = True
            return []
        if self._imap_failing:
            _LOGGER.info("[%s] IMAP reachable again", self.config_entry.title)
        self._imap_failing = False
        return messages

    async def _async_poll(self) -> Calendar:
        name = self.config_entry.title
        messages = await self._async_fetch()

        try:
            cal, snapshot = await self.store.async_load()
        except StoreError as err:
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
        )

        try:
            changes: Diff = await self.store.async_save(cal, snapshot)
        except StoreError as err:
            # Nothing flagged, nothing committed: the next poll applies the
            # same messages again, which is idempotent.
            raise self._store_failed(err, "store_write_failed") from err

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

        await self._async_missing_location_replies(changes, outcome)
        await self._async_rsvp_scan(cal)
        self._fire_events(changes, outcome)
        return cal

    # ---------------------------------------------------------------- replies

    async def _async_send(self, build, *args) -> None:
        """Build and send one message in the executor."""

        def job() -> None:
            smtp.send(self.options.smtp, build(self.options.smtp, *args))

        await self.hass.async_add_executor_job(job)

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

    async def _async_send_accept(self, component) -> None:
        opts = self.options
        await self._async_send(
            smtp.build_accept_reply,
            opts.from_name,
            opts.address,
            opts.attendee_cn,
            events.get_organizer_email(component),
            component,
        )

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

    async def async_accept(self, uid: str) -> dict[str, object]:
        """Service accept_event: RSVP ACCEPTED for one managed event, under
        any policy. Already accepted at this SEQUENCE: nothing is sent."""
        async with self.lock:
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
            seq = int(component.get("SEQUENCE", 0))
            if self.state.accepted.get(uid) == seq:
                return {"uid": uid, "sequence": seq, "sent": False}
            try:
                await self._async_send_accept(component)
            except smtp.SmtpError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="send_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
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
