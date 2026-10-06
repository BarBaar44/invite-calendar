"""Local .ics file store. BLOCKING: run in the executor.

Writes are atomic (temp file in the same directory, fsync, os.replace), so
a restart or full disk halfway through never leaves a truncated file. A file
that exists but does not parse is moved aside to <path>.corrupt and a clean
calendar is used, so one bad write can't wedge every later poll.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from icalendar import Calendar

from ..ical import events
from . import Diff, Snapshot, StoreError, diff, fingerprints, group

_LOGGER = logging.getLogger(__name__)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class IcsFileStore:
    """A calendar kept in one local .ics file, dedicated to this entry."""

    def __init__(self, path: str, prodid: str = events.PRODID) -> None:
        """Store at `path`."""
        self.path = Path(path)
        self.prodid = prodid

    def describe(self) -> str:
        """The file path."""
        return str(self.path)

    def _read(self, quarantine: bool) -> Calendar:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return events.new_calendar(prodid=self.prodid)
        except OSError as err:
            raise StoreError(f"read {self.path}: {err}") from err
        try:
            return Calendar.from_ical(raw)
        except Exception as err:  # noqa: BLE001 - any parse failure
            if quarantine:
                corrupt = self.path.with_name(self.path.name + ".corrupt")
                _LOGGER.error(
                    "%s does not parse (%s); moved aside to %s, starting empty",
                    self.path,
                    err,
                    corrupt,
                )
                try:
                    os.replace(self.path, corrupt)
                except OSError as move_err:
                    raise StoreError(
                        f"{self.path} is corrupt and can't be moved aside: {move_err}"
                    ) from move_err
            return events.new_calendar(prodid=self.prodid)

    def load(self) -> tuple[Calendar, Snapshot]:
        """Read the file (missing: empty calendar)."""
        cal = self._read(quarantine=True)
        return cal, Snapshot(fingerprints=fingerprints(cal))

    def save(self, cal: Calendar, snapshot: Snapshot) -> Diff:
        """Apply the per UID diff to a fresh read of the file and write it.
        Creates the file on the very first save even when nothing changed."""
        changes = diff(cal, snapshot)
        try:
            if not changes:
                if not self.path.exists():
                    _write_atomic(self.path, cal.to_ical())
                return changes

            current = self._read(quarantine=True)
            drop = set(changes.changed + changes.removed)
            current.subcomponents = [
                c
                for c in current.subcomponents
                if not (c.name == "VEVENT" and str(c.get("UID")) in drop)
            ]
            # Timezone definitions that new events may reference.
            have_tz = {
                str(c.get("TZID"))
                for c in current.subcomponents
                if c.name == "VTIMEZONE"
            }
            for c in cal.subcomponents:
                if c.name == "VTIMEZONE" and str(c.get("TZID")) not in have_tz:
                    have_tz.add(str(c.get("TZID")))
                    current.add_component(c)
            groups = group(cal)
            for uid in changes.changed:
                for c in groups[uid]:
                    current.add_component(c)
            _write_atomic(self.path, current.to_ical())
        except OSError as err:
            raise StoreError(f"write {self.path}: {err}") from err
        return changes
