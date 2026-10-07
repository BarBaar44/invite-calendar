"""Calendar entity expansion (calendar.py)."""

from __future__ import annotations

import datetime

from homeassistant.core import HomeAssistant

from custom_components.invite_calendar.calendar import expand
from custom_components.invite_calendar.ical import events

from .helpers import TZ, vev

WINDOW_START = datetime.datetime(2026, 10, 1, tzinfo=TZ)
WINDOW_END = datetime.datetime(2026, 12, 1, tzinfo=TZ)


def cal_of(*comps):
    cal = events.new_calendar()
    for c in comps:
        cal.add_component(c)
    return cal


async def test_weekly_series_keeps_wall_clock_across_dst(hass: HomeAssistant) -> None:
    start = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    got = expand(
        cal_of(vev("r", start, rrule={"FREQ": "WEEKLY", "COUNT": 3})),
        WINDOW_START,
        WINDOW_END,
    )
    assert [e.start.astimezone(TZ).hour for e in got] == [9, 9, 9]
    assert [e.start.utcoffset() for e in got][0] != [e.start.utcoffset() for e in got][
        -1
    ]
    assert {e.uid for e in got} == {"r"}
    assert got[0].recurrence_id == "20261020T090000"


async def test_recurrence_id_only_for_series(hass: HomeAssistant) -> None:
    """recurring-ical-events stamps RECURRENCE-ID on single events too
    (#10); only occurrences of a series may carry one."""
    start = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    rid = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=TZ)
    got = expand(
        cal_of(
            vev("single", start + datetime.timedelta(hours=3)),
            vev("r", start, rrule={"FREQ": "WEEKLY", "COUNT": 2}),
            # Invite to one occurrence of someone else's series: no master.
            vev("lone", rid + datetime.timedelta(hours=1), rid=rid),
        ),
        WINDOW_START,
        WINDOW_END,
    )
    by_uid: dict[str, list] = {}
    for e in got:
        by_uid.setdefault(e.uid, []).append(e.recurrence_id)
    assert by_uid["single"] == [None]
    assert by_uid["r"] == ["20261020T090000", "20261027T090000"]
    assert by_uid["lone"] == ["20261102T090000"]


async def test_exdate_honoured(hass: HomeAssistant) -> None:
    start = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    master = vev("r", start, rrule={"FREQ": "WEEKLY", "COUNT": 3})
    master.add("exdate", start + datetime.timedelta(days=7))
    got = expand(cal_of(master), WINDOW_START, WINDOW_END)
    assert [e.start.date() for e in got] == [
        datetime.date(2026, 10, 20),
        datetime.date(2026, 11, 3),
    ]


async def test_all_day_event(hass: HomeAssistant) -> None:
    got = expand(
        cal_of(vev("d", datetime.date(2026, 10, 15))), WINDOW_START, WINDOW_END
    )
    assert got[0].all_day
    assert got[0].end == datetime.date(2026, 10, 16)


async def test_all_day_without_dtend(hass: HomeAssistant) -> None:
    e = vev("d", datetime.date(2026, 10, 15))
    e.pop("DTEND")
    got = expand(cal_of(e), WINDOW_START, WINDOW_END)
    assert got[0].end == datetime.date(2026, 10, 16)


async def test_floating_time_is_local(hass: HomeAssistant) -> None:
    got = expand(
        cal_of(vev("f", datetime.datetime(2026, 10, 15, 14, 0))),
        WINDOW_START,
        WINDOW_END,
    )
    assert got[0].start == datetime.datetime(2026, 10, 15, 14, 0, tzinfo=TZ)


async def test_cancelled_status_hidden(hass: HomeAssistant) -> None:
    start = datetime.datetime(2026, 10, 15, 9, tzinfo=TZ)
    got = expand(
        cal_of(vev("c", start, status="CANCELLED"), vev("k", start)),
        WINDOW_START,
        WINDOW_END,
    )
    assert [e.uid for e in got] == ["k"]


async def test_zero_duration_event(hass: HomeAssistant) -> None:
    start = datetime.datetime(2026, 10, 15, 9, tzinfo=TZ)
    e = vev("z", start, end=start)
    got = expand(cal_of(e), WINDOW_START, WINDOW_END)
    assert got[0].start == got[0].end == start


async def test_sorted_by_start(hass: HomeAssistant) -> None:
    a = vev("late", datetime.datetime(2026, 10, 20, 9, tzinfo=TZ))
    b = vev("early", datetime.date(2026, 10, 10))
    got = expand(cal_of(a, b), WINDOW_START, WINDOW_END)
    assert [e.uid for e in got] == ["early", "late"]
