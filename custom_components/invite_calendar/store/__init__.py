"""Calendar stores: the backend protocol and the per UID diff.

load() fingerprints every UID; save() writes only the UIDs whose components
changed and removes the ones that disappeared. For a shared CalDAV calendar
(milestone 2) this is what keeps events people add by hand untouched. For
an .ics file the diff is re-applied to a fresh read of the file, which also
protects against another writer during migration (the pyscript app).

Backends are BLOCKING: the coordinator calls them in the executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from icalendar import Calendar, Event


@dataclass(slots=True)
class Snapshot:
    """What a load() saw: per UID fingerprints, plus backend data (CalDAV
    hrefs)."""

    fingerprints: dict[str, bytes]
    hrefs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Diff:
    """Per UID changes between a snapshot and a calendar."""

    added: list[str]
    updated: list[str]
    removed: list[str]

    @property
    def changed(self) -> list[str]:
        """Added plus updated."""
        return self.added + self.updated

    def __bool__(self) -> bool:
        return bool(self.added or self.updated or self.removed)


class StoreBackend(Protocol):
    """A calendar store. All methods block."""

    def load(self) -> tuple[Calendar, Snapshot]:
        """Read the calendar. Raises StoreError when it can't be read."""

    def save(self, cal: Calendar, snapshot: Snapshot) -> Diff:
        """Write the per UID diff since load(). Raises StoreError."""

    def describe(self) -> str:
        """Short human readable name, for logs."""


class StoreError(Exception):
    """A store could not be read or written."""


def group(cal: Calendar) -> dict[str, list[Event]]:
    """{uid: [VEVENT components in file order]}"""
    groups: dict[str, list[Event]] = {}
    for c in cal.subcomponents:
        if c.name != "VEVENT" or c.get("UID") is None:
            continue
        groups.setdefault(str(c.get("UID")), []).append(c)
    return groups


def fingerprints(cal: Calendar) -> dict[str, bytes]:
    """{uid: serialized components}, the unit of change for a save."""
    return {
        uid: b"".join(c.to_ical() for c in comps) for uid, comps in group(cal).items()
    }


def diff(cal: Calendar, snapshot: Snapshot) -> Diff:
    """Per UID changes in `cal` since `snapshot`."""
    now = fingerprints(cal)
    old = snapshot.fingerprints
    return Diff(
        added=sorted(u for u in now if u not in old),
        updated=sorted(u for u in now if u in old and old[u] != now[u]),
        removed=sorted(u for u in old if u not in now),
    )
