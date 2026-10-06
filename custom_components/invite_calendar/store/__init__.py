"""Calendar stores: the backend protocol and the per UID diff.

async_load() fingerprints every UID; async_save() writes only the UIDs whose
components changed and removes the ones that disappeared.

* A local .ics file (ics_file.py) is DEDICATED to this entry. The diff is
  re-applied to a fresh read of the file, which also protects against
  another writer during migration (the pyscript app).
* A CalDAV calendar (caldav.py) is SHARED: people add events in Nextcloud
  directly. Untouched events are never written, and retention, RSVPs and
  state only ever act on MANAGED events (those that came in by mail).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from icalendar import Calendar, Event


@dataclass(slots=True)
class Snapshot:
    """What a load saw: per UID fingerprints, plus CalDAV resource data."""

    fingerprints: dict[str, bytes]
    hrefs: dict[str, str] = field(default_factory=dict)  # uid -> href
    etags: dict[str, str] = field(default_factory=dict)  # href -> etag


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
    """A calendar store."""

    # True when other people write the same calendar (CalDAV): retention
    # then only touches managed events.
    shared: bool

    async def async_load(self) -> tuple[Calendar, Snapshot]:
        """Read the calendar. Raises StoreError when it can't be read."""

    async def async_save(self, cal: Calendar, snapshot: Snapshot) -> Diff:
        """Write the per UID diff since the load. Raises StoreError."""

    def describe(self) -> str:
        """Short human readable name, for logs."""


class StoreError(Exception):
    """A store could not be read or written."""


class StoreAuthError(StoreError):
    """The store rejected the credentials."""


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
