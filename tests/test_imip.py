"""Inbound iMIP (ical/imip.py), the Tier 1 inbound cases."""

from __future__ import annotations

import datetime
import email

import pytest

from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.ical.imip import apply_message

from .helpers import BROKEN_MAIL, TZ, mail, vev

T0 = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
RID = T0 + datetime.timedelta(days=7)
WEEKLY = {"FREQ": "WEEKLY", "COUNT": 4}


def apply(raw: bytes, cal):
    return apply_message(email.message_from_bytes(raw), cal, "test")


def shape(cal) -> list[tuple[str, bool]]:
    return sorted(
        (str(c["UID"]), events.recurrence_key(c) is not None)
        for c in cal.walk("VEVENT")
    )


def test_single_request_records_organizer_and_invite() -> None:
    cal = events.new_calendar()
    result = apply(mail("REQUEST", [vev("a", T0)], "m1"), cal)
    assert result.changed
    assert shape(cal) == [("a", False)]
    assert result.organizers == {"a": "boss@ext.com"}
    assert [r.uid for r in result.received] == ["a"]
    assert result.received[0].location == "Utrecht"


def test_recurring_master_plus_override() -> None:
    cal = events.new_calendar()
    apply(
        mail(
            "REQUEST",
            [
                vev("r", T0, rrule=WEEKLY),
                vev("r", RID + datetime.timedelta(hours=2), rid=RID),
            ],
            "m1",
        ),
        cal,
    )
    assert shape(cal) == [("r", False), ("r", True)]


def test_request_with_overrides_only_keeps_master() -> None:
    cal = events.new_calendar()
    apply(mail("REQUEST", [vev("r", T0, rrule=WEEKLY)], "m1"), cal)
    result = apply(
        mail("REQUEST", [vev("r", RID + datetime.timedelta(hours=1), rid=RID)], "m2"),
        cal,
    )
    assert result.changed
    assert shape(cal) == [("r", False), ("r", True)]


def test_cancel_series() -> None:
    cal = events.new_calendar()
    apply(mail("REQUEST", [vev("r", T0, rrule=WEEKLY)], "m1"), cal)
    result = apply(mail("CANCEL", [vev("r", T0, rrule=WEEKLY, seq=1)], "m2"), cal)
    assert result.cancelled == ["r"]
    assert shape(cal) == []


def test_cancel_one_occurrence() -> None:
    cal = events.new_calendar()
    apply(
        mail(
            "REQUEST",
            [
                vev("r", T0, rrule=WEEKLY),
                vev("r", RID + datetime.timedelta(hours=2), rid=RID),
            ],
            "m1",
        ),
        cal,
    )
    result = apply(mail("CANCEL", [vev("r", RID, rid=RID, seq=1)], "m2"), cal)
    assert result.changed and result.cancelled == []
    assert shape(cal) == [("r", False)]
    assert events.find_event(cal, "r").get("EXDATE") is not None


def test_reply_is_ignored() -> None:
    cal = events.new_calendar()
    apply(mail("REQUEST", [vev("a", T0)], "m1"), cal)
    result = apply(mail("REPLY", [vev("a", T0, location=None, seq=5)], "m2"), cal)
    assert not result.changed
    assert str(events.find_event(cal, "a")["LOCATION"]) == "Utrecht"


def test_duplicate_parts_in_one_mail_apply_once() -> None:
    cal = events.new_calendar()
    result = apply(mail("REQUEST", [vev("a", T0)], "m1", twice=True), cal)
    assert len(result.received) == 1
    assert shape(cal) == [("a", False)]


def test_stale_sequence_ignored() -> None:
    cal = events.new_calendar()
    apply(mail("REQUEST", [vev("a", T0, seq=3, summary="new")], "m1"), cal)
    result = apply(mail("REQUEST", [vev("a", T0, seq=1, summary="old")], "m2"), cal)
    assert not result.changed
    assert str(events.find_event(cal, "a")["SUMMARY"]) == "new"


def test_broken_dtstart_raises() -> None:
    cal = events.new_calendar()
    with pytest.raises(ValueError, match="DTSTART"):
        apply(BROKEN_MAIL, cal)
    assert shape(cal) == []


def test_unparseable_part_is_skipped() -> None:
    cal = events.new_calendar()
    raw = (
        b"Message-ID: <x>\r\nContent-Type: text/calendar\r\n\r\n"
        b"this is not a calendar\r\n"
    )
    result = apply(raw, cal)
    assert not result.changed


def test_mail_without_calendar_part() -> None:
    cal = events.new_calendar()
    raw = b"Message-ID: <x>\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
    assert not apply(raw, cal).changed
