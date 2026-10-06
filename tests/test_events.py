"""VEVENT helpers (ical/events.py)."""

from __future__ import annotations

import datetime

from custom_components.invite_calendar.ical import events

from .helpers import TZ, vev

T0 = datetime.datetime(2026, 10, 10, 9, 0, tzinfo=TZ)


def test_upsert_ignores_stale_sequence() -> None:
    cal = events.new_calendar()
    assert events.upsert_event(cal, vev("a", T0, seq=2, summary="new"))
    assert not events.upsert_event(cal, vev("a", T0, seq=1, summary="old"))
    assert str(events.find_event(cal, "a")["SUMMARY"]) == "new"


def test_override_sits_next_to_master() -> None:
    cal = events.new_calendar()
    rid = T0 + datetime.timedelta(days=7)
    events.replace_series(cal, [vev("r", T0, rrule={"FREQ": "WEEKLY", "COUNT": 4})])
    events.replace_series(cal, [vev("r", rid + datetime.timedelta(hours=2), rid=rid)])
    comps = [c for c in cal.walk("VEVENT")]
    assert len(comps) == 2
    assert events.recurrence_key(events.find_event(cal, "r")) is None


def test_request_with_master_replaces_series() -> None:
    cal = events.new_calendar()
    rid = T0 + datetime.timedelta(days=7)
    events.replace_series(
        cal,
        [
            vev("r", T0, rrule={"FREQ": "WEEKLY", "COUNT": 4}),
            vev("r", rid + datetime.timedelta(hours=2), rid=rid),
        ],
    )
    events.replace_series(
        cal, [vev("r", T0, rrule={"FREQ": "WEEKLY", "COUNT": 2}, seq=1)]
    )
    assert len(list(cal.walk("VEVENT"))) == 1


def test_stale_master_changes_nothing() -> None:
    cal = events.new_calendar()
    events.replace_series(cal, [vev("r", T0, seq=3)])
    assert not events.replace_series(cal, [vev("r", T0, seq=2, summary="old")])


def test_cancel_occurrence_adds_exdate_and_drops_override() -> None:
    cal = events.new_calendar()
    rid = T0 + datetime.timedelta(days=7)
    events.replace_series(
        cal,
        [
            vev("r", T0, rrule={"FREQ": "WEEKLY", "COUNT": 4}),
            vev("r", rid + datetime.timedelta(hours=2), rid=rid),
        ],
    )
    cancel = vev("r", rid, rid=rid, seq=1)
    assert events.cancel_occurrence(cal, "r", cancel["RECURRENCE-ID"])
    assert len(list(cal.walk("VEVENT"))) == 1
    assert events.find_event(cal, "r").get("EXDATE") is not None
    # Idempotent: the same CANCEL again changes nothing.
    assert not events.cancel_occurrence(cal, "r", cancel["RECURRENCE-ID"])


def test_series_end_uses_last_occurrence() -> None:
    master = vev("r", T0, rrule={"FREQ": "DAILY", "COUNT": 3})
    assert events.series_end(master) == T0 + datetime.timedelta(days=2, hours=1)
    unbounded = vev("u", T0, rrule={"FREQ": "WEEKLY"})
    assert events.series_end(unbounded) is None


def test_series_end_keeps_wall_clock_over_dst() -> None:
    # 09:00 local on 20 Oct (CEST) and 3 Nov (CET).
    start = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    master = vev("d", start, rrule={"FREQ": "WEEKLY", "COUNT": 3})
    end = events.series_end(master)
    assert end.astimezone(TZ).hour == 10
    assert end.astimezone(TZ).date() == datetime.date(2026, 11, 3)


def test_prune_keeps_unbounded_and_recent() -> None:
    cal = events.new_calendar()
    old = T0 - datetime.timedelta(days=400)
    events.upsert_event(cal, vev("old", old))
    events.upsert_event(cal, vev("forever", old, rrule={"FREQ": "WEEKLY"}))
    events.upsert_event(cal, vev("series", old, rrule={"FREQ": "DAILY", "COUNT": 2}))
    events.upsert_event(cal, vev("new", T0))
    gone = events.prune_past_events(cal, T0 - datetime.timedelta(days=30))
    assert gone == ["old", "series"]
    assert events.all_uids(cal) == {"forever", "new"}


def test_prune_scoped_to_managed() -> None:
    cal = events.new_calendar()
    old = T0 - datetime.timedelta(days=400)
    events.upsert_event(cal, vev("mine", old))
    events.upsert_event(cal, vev("theirs", old))
    assert events.prune_past_events(cal, T0, only_uids={"mine"}) == ["mine"]
    assert events.all_uids(cal) == {"theirs"}


def test_prune_never_crashes_on_bad_dates() -> None:
    cal = events.new_calendar()
    bad = vev("bad", T0)
    bad.pop("DTSTART")
    bad.pop("DTEND")
    cal.add_component(bad)
    assert events.prune_past_events(cal, T0) == []


def test_organizer_and_attendees() -> None:
    e = vev("a", T0)
    e.add("attendee", "mailto:Tesla@Example.com")
    assert events.get_organizer_email(e) == "boss@ext.com"
    assert events.get_attendee_emails(e) == ["tesla@example.com"]
