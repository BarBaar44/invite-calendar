"""Per entry state in HA's .storage (replaces the pyscript side car files).

    {"organizer": {uid: email},        managed events and who sent them
     "accepted":  {uid: sequence},     RSVPs sent (milestone 3)
     "failed":    {message_id: n},     processing attempts per message
     "sent":      {uid: message_id}}   our own outbound invites (milestone 3)

"Managed" means the UID is in `organizer`: it arrived through the mailbox
(or, later, was created through the services).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STATE_STORAGE_VERSION


@dataclass(slots=True)
class EntryState:
    """Mutable state for one entry."""

    organizer: dict[str, str] = field(default_factory=dict)
    accepted: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    sent: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON document."""
        return {
            "organizer": self.organizer,
            "accepted": self.accepted,
            "failed": self.failed,
            "sent": self.sent,
        }

    def forget(self, uids: list[str] | set[str]) -> bool:
        """Drop every per UID entry for these UIDs. True if anything went."""
        changed = False
        for uid in uids:
            for mapping in (self.organizer, self.accepted, self.sent):
                if mapping.pop(uid, None) is not None:
                    changed = True
        return changed

    def prune_to(self, live: set[str]) -> bool:
        """Drop per UID entries whose UID is no longer in the calendar."""
        stale = {
            uid
            for mapping in (self.organizer, self.accepted, self.sent)
            for uid in mapping
            if uid not in live
        }
        return self.forget(stale)


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
        )

    async def async_save(self, state: EntryState) -> None:
        """Write now."""
        await self._store.async_save(state.as_dict())

    async def async_remove(self) -> None:
        """Delete the file (entry removed)."""
        await self._store.async_remove()
