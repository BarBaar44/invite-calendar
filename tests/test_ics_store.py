"""Local .ics store (store/ics_file.py)."""

from __future__ import annotations

import datetime
from pathlib import Path

from icalendar import Calendar

from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.store.ics_file import IcsFileStore

from .helpers import TZ, vev

T0 = datetime.datetime(2026, 10, 10, 9, 0, tzinfo=TZ)


def test_file_created_on_first_run(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "cal.ics"
    store = IcsFileStore(None, str(path))
    cal, snap = store.load()
    assert not store.save(cal, snap)
    assert path.exists()


def test_diff_reports_added_updated_removed(tmp_path: Path) -> None:
    store = IcsFileStore(None, str(tmp_path / "cal.ics"))
    cal, snap = store.load()
    events.upsert_event(cal, vev("a", T0))
    events.upsert_event(cal, vev("b", T0))
    d = store.save(cal, snap)
    assert d.added == ["a", "b"] and not d.updated and not d.removed

    cal, snap = store.load()
    events.remove_event(cal, "a")
    events.bump_sequence(events.find_event(cal, "b"))
    d = store.save(cal, snap)
    assert d.updated == ["b"] and d.removed == ["a"] and not d.added


def test_concurrent_writers_do_not_lose_changes(tmp_path: Path) -> None:
    store = IcsFileStore(None, str(tmp_path / "cal.ics"))
    cal, snap = store.load()
    events.upsert_event(cal, vev("a", T0))
    events.upsert_event(cal, vev("b", T0))
    store.save(cal, snap)

    c1, s1 = store.load()
    c2, s2 = store.load()
    events.remove_event(c1, "a")
    events.upsert_event(c2, vev("c", T0))
    events.bump_sequence(events.find_event(c2, "b"))
    store.save(c1, s1)
    store.save(c2, s2)

    final, _ = store.load()
    assert events.all_uids(final) == {"b", "c"}
    assert int(events.find_event(final, "b")["SEQUENCE"]) == 1


def test_no_change_does_not_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "cal.ics"
    store = IcsFileStore(None, str(path))
    cal, snap = store.load()
    events.upsert_event(cal, vev("a", T0))
    store.save(cal, snap)
    mtime = path.stat().st_mtime_ns
    cal, snap = store.load()
    assert not store.save(cal, snap)
    assert path.stat().st_mtime_ns == mtime


def test_corrupt_file_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "cal.ics"
    path.write_bytes(b"BEGIN:VCALENDAR\r\nthis is broken")
    store = IcsFileStore(None, str(path))
    cal, _ = store.load()
    assert events.all_uids(cal) == set()
    assert (tmp_path / "cal.ics.corrupt").read_bytes().startswith(b"BEGIN:VCALENDAR")
    assert not path.exists()


def test_new_vtimezone_is_written(tmp_path: Path) -> None:
    store = IcsFileStore(None, str(tmp_path / "cal.ics"))
    cal, snap = store.load()
    events.upsert_event(cal, vev("a", T0))
    cal.add_missing_timezones()
    store.save(cal, snap)
    saved = Calendar.from_ical((tmp_path / "cal.ics").read_bytes())
    assert [str(c["TZID"]) for c in saved.walk("VTIMEZONE")] == ["Europe/Amsterdam"]
