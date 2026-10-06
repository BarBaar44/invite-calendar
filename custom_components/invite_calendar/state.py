"""Per entry state in HA's .storage (replaces the pyscript side car files).

    {"organizer": {uid: email},        managed events and who sent them
     "accepted":  {uid: sequence},     RSVPs sent (milestone 3)
     "failed":    {message_id: n},     processing attempts per message
     "sent":      {uid: message_id},   our own outbound invites (milestone 3b)
     "rsvp_failed": {uid: sequence},   RSVP permanently refused for this
                                       sequence; tried again after an update
     "pending":   {uid: sequence},     own event saved but its REQUEST not
                                       sent yet; resent on the next poll
     "declined":  {uid: {...}}}        policy if_free: what was declined
                                       (see DeclineRecord)

"Managed" means the UID is in `organizer`: it arrived through the mailbox
(or, later, was created through the services).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STATE_STORAGE_VERSION


class DeclineRecord(TypedDict, total=False):
    """One declined invitation, for the SEQUENCE it was decided at.

    whole:       the event was declined and left out of the calendar
    occurrences: series: declined occurrence starts (ISO), each an EXDATE
    unsent:      occurrences whose DECLINED reply still has to go out
    until:       end of the declined event (whole), unix seconds; the record
                 is kept that long so a resent copy is left out again
    """

    sequence: int
    whole: bool
    occurrences: list[str]
    unsent: list[str]
    until: float | None


@dataclass(slots=True)
class EntryState:
    """Mutable state for one entry."""

    organizer: dict[str, str] = field(default_factory=dict)
    accepted: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    sent: dict[str, str] = field(default_factory=dict)
    rsvp_failed: dict[str, int] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)
    declined: dict[str, DeclineRecord] = field(default_factory=dict)

    def declined_sequence(self, uid: str) -> int | None:
        """SEQUENCE the invitation was declined at (wholly or in part)."""
        record = self.declined.get(uid)
        return int(record["sequence"]) if record else None

    def _per_uid(self) -> tuple[dict, ...]:
        return (
            self.organizer,
            self.accepted,
            self.sent,
            self.rsvp_failed,
            self.pending,
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON document."""
        return {
            "organizer": self.organizer,
            "accepted": self.accepted,
            "failed": self.failed,
            "sent": self.sent,
            "rsvp_failed": self.rsvp_failed,
            "pending": self.pending,
            "declined": self.declined,
        }

    def forget(self, uids: list[str] | set[str]) -> bool:
        """Drop every per UID entry for these UIDs (cancelled or deleted).
        True if anything went."""
        changed = False
        for uid in uids:
            for mapping in (*self._per_uid(), self.declined):
                if mapping.pop(uid, None) is not None:
                    changed = True
        return changed

    def prune_to(self, live: set[str], now: datetime.datetime | None = None) -> bool:
        """Drop per UID entries whose UID is no longer in the calendar.

        A wholly declined event is not in the calendar on purpose: its record
        stays until the event is over, so a resent copy of the same version
        is left out again instead of being decided twice."""
        stale = {
            uid for mapping in self._per_uid() for uid in mapping if uid not in live
        }
        changed = self.forget(stale)
        ts = (now or datetime.datetime.now(datetime.UTC)).timestamp()
        for uid, record in list(self.declined.items()):
            if uid in live:
                continue
            until = record.get("until")
            if not record.get("whole") or (until is not None and until < ts):
                del self.declined[uid]
                changed = True
        return changed


class StateStore:
    """Loads and saves EntryState for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Store under .storage/invite_calendar.<entry_id>."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STATE_STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )

    async def async_load(self) -> EntryState:
        """Read, or start empty."""
        data = await self._store.async_load() or {}
        return EntryState(
            organizer=dict(data.get("organizer") or {}),
            accepted={k: int(v) for k, v in (data.get("accepted") or {}).items()},
            failed={k: int(v) for k, v in (data.get("failed") or {}).items()},
            sent=dict(data.get("sent") or {}),
            rsvp_failed={k: int(v) for k, v in (data.get("rsvp_failed") or {}).items()},
            pending={k: int(v) for k, v in (data.get("pending") or {}).items()},
            declined=dict(data.get("declined") or {}),
        )

    async def async_save(self, state: EntryState) -> None:
        """Write now."""
        await self._store.async_save(state.as_dict())

    async def async_remove(self) -> None:
        """Delete the file (entry removed)."""
        await self._store.async_remove()
