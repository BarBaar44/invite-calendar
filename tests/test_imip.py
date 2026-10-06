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


OWN = "cal@example.com"


def apply(raw: bytes, cal, managed: dict[str, str] | None = None):
    """Apply like the coordinator does: `managed` is the organizer map, kept
    up to date across calls (one per calendar, attached to it here)."""
    if managed is None:
        if not hasattr(cal, "test_managed"):
            cal.test_managed = {}
        managed = cal.test_managed
    result = apply_message(email.message_from_bytes(raw), cal, "test", OWN, managed)
    managed.update(result.organizers)
    for uid in result.cancelled:
        managed.pop(uid, None)
    return result


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


# ------------------------------------------------ who may change what


def test_cancel_from_other_organizer_is_ignored() -> None:
    cal = events.new_calendar()
    apply(mail("REQUEST", [vev("a", T0)], "m1"), cal)
    result = apply(
        mail("CANCEL", [vev("a", T0, seq=1, organizer="evil@ext.com")], "m2"), cal
    )
    assert not result.changed and result.refused == ["a"]
    assert result.cancelled == []
    assert shape(cal) == [("a", False)]


def test_request_from_other_organizer_is_ignored() -> None:
    cal = events.new_calendar()
    apply(mail("REQUEST", [vev("a", T0)], "m1"), cal)
    result = apply(
        mail(
            "REQUEST",
            [vev("a", T0, seq=2, summary="hijack", organizer="evil@ext.com")],
            "m2",
        ),
        cal,
    )
    assert not result.changed and result.refused == ["a"]
    assert result.organizers == {}
    assert str(events.find_event(cal, "a")["SUMMARY"]) == "S a"


def test_organizer_match_is_case_insensitive() -> None:
    cal = events.new_calendar()
    apply(mail("REQUEST", [vev("a", T0)], "m1"), cal)
    result = apply(
        mail("CANCEL", [vev("a", T0, seq=1, organizer="Boss@EXT.com")], "m2"), cal
    )
    assert result.cancelled == ["a"] and shape(cal) == []


def test_unmanaged_event_is_never_cancelled() -> None:
    """Made by hand in Nextcloud: same UID, never arrived by mail."""
    cal = events.new_calendar()
    events.upsert_event(cal, vev("dentist", T0, organizer="boss@ext.com"))
    result = apply(mail("CANCEL", [vev("dentist", T0, seq=1)], "m1"), cal, {})
    assert not result.changed and result.refused == ["dentist"]
    assert shape(cal) == [("dentist", False)]


def test_unmanaged_event_is_never_overwritten() -> None:
    cal = events.new_calendar()
    events.upsert_event(cal, vev("dentist", T0, organizer=None))
    result = apply(
        mail("REQUEST", [vev("dentist", T0, seq=5, summary="mine now")], "m1"),
        cal,
        {},
    )
    assert not result.changed and result.refused == ["dentist"]
    assert result.organizers == {}
    assert str(events.find_event(cal, "dentist")["SUMMARY"]) == "S dentist"


def test_unmanaged_occurrence_is_never_cancelled() -> None:
    cal = events.new_calendar()
    events.upsert_event(cal, vev("r", T0, rrule=WEEKLY))
    result = apply(mail("CANCEL", [vev("r", RID, rid=RID, seq=1)], "m1"), cal, {})
    assert not result.changed
    assert events.find_event(cal, "r").get("EXDATE") is None


def test_request_without_organizer_is_not_imported() -> None:
    cal = events.new_calendar()
    result = apply(mail("REQUEST", [vev("a", T0, organizer=None)], "m1"), cal)
    assert not result.changed and result.refused == ["a"]
    assert shape(cal) == []


def test_own_events_are_never_changed_by_mail() -> None:
    cal = events.new_calendar()
    events.upsert_event(cal, vev("mine", T0, organizer=OWN))
    managed = {"mine": OWN}
    for method, seq in (("REQUEST", 3), ("CANCEL", 4)):
        result = apply(
            mail(method, [vev("mine", T0, seq=seq, organizer=OWN)], "m"),
            cal,
            managed,
        )
        assert not result.changed and result.refused == ["mine"]
    assert shape(cal) == [("mine", False)]


def test_own_invitation_coming_back_is_not_imported() -> None:
    cal = events.new_calendar()
    result = apply(mail("REQUEST", [vev("x", T0, organizer=OWN)], "m1"), cal)
    assert not result.changed and shape(cal) == []


def test_cancel_of_unknown_uid_changes_nothing() -> None:
    cal = events.new_calendar()
    result = apply(mail("CANCEL", [vev("ghost", T0, seq=1)], "m1"), cal)
    assert not result.changed and result.refused == []
