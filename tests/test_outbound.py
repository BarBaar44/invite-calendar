"""create_event, update_event, cancel_event: events this calendar organizes."""

from __future__ import annotations

import datetime
import email
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from icalendar import Calendar
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar.const import DOMAIN
from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.ical.imip import apply_message
from custom_components.invite_calendar.mail import smtp

from .caldav_fake import FakeCalDav
from .conftest import FakeMailbox, Outbox
from .helpers import TZ, mail, vev

ENTITY = "calendar.tesla"
BART = "bart@example.com"
ANNE = "anne@example.com"


def local(days: int, hour: int = 9) -> datetime.datetime:
    return (dt_util.now() + datetime.timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


async def call(hass: HomeAssistant, service: str, **data: Any) -> dict[str, Any]:
    resp = await hass.services.async_call(
        DOMAIN,
        service,
        {"entity_id": ENTITY, **data},
        blocking=True,
        return_response=True,
    )
    return resp[ENTITY]


async def list_events(hass: HomeAssistant, days: int = 60) -> list[dict[str, Any]]:
    return (await call(hass, "list_events", duration={"days": days}))["events"]


def ics_of(msg) -> Calendar:
    """The attached .ics of a REQUEST/CANCEL; asserts there's no inline part."""
    parts = list(msg.walk())
    assert not any(p.get_content_type() == "text/calendar" for p in parts)
    (attachment,) = [p for p in parts if p.get_content_type() == "application/ics"]
    return Calendar.from_ical(attachment.get_payload(decode=True))


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    outbox: Outbox,
    setup_entry: MockConfigEntry,
) -> MockConfigEntry:
    return setup_entry


# ---- create --------------------------------------------------------------


async def test_create_sends_request_as_organizer(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    resp = await call(
        hass,
        "create_event",
        summary="Trip to Delft",
        start_date_time=local(2).isoformat(),
        location="Markt 87, Delft",
        description="TRIP_TYPE=work",
        attendees=[BART.upper()],
    )
    uid = resp["uid"]
    assert uid.endswith("@example.com")
    assert resp["invited"] == [BART] and not resp["pending"]

    (msg,) = outbox.sent
    assert msg["From"] == "Tesla Calendar <tesla@example.com>"
    assert msg["To"] == BART
    assert msg["Subject"].startswith("Invitation: Trip to Delft @ ")
    cal = ics_of(msg)
    assert str(cal["METHOD"]) == "REQUEST"
    (ev,) = cal.walk("VEVENT")
    assert events.get_organizer_email(ev) == "tesla@example.com"
    assert events.get_attendee_emails(ev) == [BART]
    assert int(ev["SEQUENCE"]) == 0 and ev.get("DTSTAMP") is not None
    assert ev["DTSTART"].params["TZID"] == "Europe/Amsterdam"
    assert ev["DTEND"].dt - ev["DTSTART"].dt == datetime.timedelta(hours=1)
    assert str(ev["DESCRIPTION"]) == "TRIP_TYPE=work"

    (listed,) = await list_events(hass)
    assert listed["uid"] == uid and listed["own"] and listed["managed"]
    assert not listed["accepted"]
    assert entry.runtime_data.state.sent[uid] == msg["Message-ID"]


async def test_attendee_client_reads_the_invite(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    """The REQUEST round trips through the same parser an attendee uses."""
    await call(
        hass,
        "create_event",
        summary="Weekly",
        start_date_time=local(2).isoformat(),
        rrule="FREQ=WEEKLY;COUNT=3",
        attendees=[BART],
    )
    (msg,) = outbox.sent
    theirs = events.new_calendar()
    result = apply_message(email.message_from_bytes(msg.as_bytes()), theirs)
    assert result.changed
    (ev,) = theirs.walk("VEVENT")
    assert str(ev["RRULE"].to_ical().decode()) == "FREQ=WEEKLY;COUNT=3"


async def test_create_without_attendees_sends_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    resp = await call(
        hass, "create_event", summary="Solo", start_date=str(local(3).date())
    )
    assert resp["invited"] == [] and not resp["pending"]
    assert outbox.sent == []
    (listed,) = await list_events(hass)
    assert listed["all_day"] and listed["start"] == str(local(3).date())


async def test_recurring_series_keeps_wall_clock_across_dst(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    start = datetime.datetime(2026, 10, 19, 9, 0)  # naive: local
    await call(
        hass,
        "create_event",
        summary="Standup",
        start_date_time=start.isoformat(),
        rrule="RRULE:FREQ=WEEKLY;COUNT=3",
    )
    got = (
        await call(
            hass,
            "list_events",
            start="2026-10-18T00:00:00",
            end="2026-11-10T00:00:00",
        )
    )["events"]
    starts = [dt_util.parse_datetime(e["start"]).astimezone(TZ) for e in got]
    assert [s.hour for s in starts] == [9, 9, 9]
    assert [s.day for s in starts] == [19, 26, 2]


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"rrule": "FREQ=SOMETIMES"}, "repeat rule"),
        ({"attendees": ["not an address"]}, "email address"),
        ({"end_date_time": "2020-01-01T00:00:00"}, "after the start"),
        ({"end_date": "2030-01-01"}, "both be dates"),
    ],
)
async def test_create_validation(
    hass: HomeAssistant, entry: MockConfigEntry, data: dict, match: str
) -> None:
    with pytest.raises(ServiceValidationError, match=match):
        await call(
            hass,
            "create_event",
            summary="x",
            start_date_time=local(2).isoformat(),
            **data,
        )


async def test_failed_invite_is_pending_and_resent(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    outbox.error = smtp.SmtpError("down")
    resp = await call(
        hass,
        "create_event",
        summary="x",
        start_date_time=local(2).isoformat(),
        attendees=[BART],
    )
    assert resp["pending"]
    assert len(await list_events(hass)) == 1  # saved anyway: HA is authoritative
    assert entry.runtime_data.state.pending == {resp["uid"]: 0}

    outbox.error = None
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(outbox.sent) == 1
    assert entry.runtime_data.state.pending == {}


# ---- update --------------------------------------------------------------


async def test_update_series_moves_and_threads(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    uid = (
        await call(
            hass,
            "create_event",
            summary="Trip",
            start_date_time=local(2).isoformat(),
            end_date_time=local(2, 11).isoformat(),
            attendees=[BART, ANNE],
        )
    )["uid"]
    first_id = outbox.sent[0]["Message-ID"]

    resp = await call(
        hass,
        "update_event",
        uid=uid,
        start_date_time=local(3, 14).isoformat(),
        attendees=[BART],
    )
    assert resp["sequence"] == 1 and resp["removed"] == [ANNE]

    request, cancel = outbox.sent[1:]
    assert request["Subject"].startswith("Updated invitation: Trip")
    assert request["In-Reply-To"] == first_id
    (ev,) = ics_of(request).walk("VEVENT")
    assert int(ev["SEQUENCE"]) == 1
    assert ev["DTSTART"].dt == local(3, 14)
    assert ev["DTEND"].dt == local(3, 16)  # duration kept

    assert cancel["To"] == ANNE
    cal = ics_of(cancel)
    assert str(cal["METHOD"]) == "CANCEL"
    (stub,) = cal.walk("VEVENT")
    assert events.get_attendee_emails(stub) == [ANNE]


async def test_update_one_occurrence(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    uid = (
        await call(
            hass,
            "create_event",
            summary="Weekly",
            start_date_time=local(2).isoformat(),
            rrule="FREQ=WEEKLY;COUNT=3",
            attendees=[BART],
        )
    )["uid"]
    second = (await list_events(hass))[1]
    await call(
        hass,
        "update_event",
        uid=uid,
        recurrence_id=second["recurrence_id"],
        start_date_time=local(10, 15).isoformat(),
        location="Elsewhere",
    )
    got = await list_events(hass)
    assert len(got) == 3
    moved = [e for e in got if e["location"] == "Elsewhere"]
    assert dt_util.parse_datetime(moved[0]["start"]) == local(10, 15)

    # The REQUEST carries the whole series: master plus the override.
    comps = list(ics_of(outbox.sent[-1]).walk("VEVENT"))
    assert len(comps) == 2
    assert {int(c["SEQUENCE"]) for c in comps} == {1}
    assert sum(c.get("RECURRENCE-ID") is not None for c in comps) == 1


async def test_update_errors(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    mailbox.add(mail("REQUEST", [vev("theirs", local(2))], "m1"))
    await entry.runtime_data.async_refresh()
    with pytest.raises(ServiceValidationError, match="organized by someone else"):
        await call(hass, "update_event", uid="theirs", summary="mine now")
    with pytest.raises(ServiceValidationError, match="No event"):
        await call(hass, "update_event", uid="nope", summary="x")

    uid = (
        await call(
            hass, "create_event", summary="x", start_date_time=local(2).isoformat()
        )
    )["uid"]
    with pytest.raises(ServiceValidationError, match="does not repeat"):
        await call(hass, "update_event", uid=uid, recurrence_id="20261020T090000")

    series = (
        await call(
            hass,
            "create_event",
            summary="s",
            start_date_time=local(2).isoformat(),
            rrule="FREQ=DAILY;COUNT=3",
        )
    )["uid"]
    with pytest.raises(ServiceValidationError, match="no occurrence"):
        await call(
            hass,
            "update_event",
            uid=series,
            recurrence_id="19990101T090000",
            summary="x",
        )
    rid = [e for e in await list_events(hass) if e["uid"] == series][0]["recurrence_id"]
    with pytest.raises(ServiceValidationError, match="whole series"):
        await call(
            hass, "update_event", uid=series, recurrence_id=rid, rrule="FREQ=WEEKLY"
        )


async def test_stop_repeating_drops_overrides(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    uid = (
        await call(
            hass,
            "create_event",
            summary="w",
            start_date_time=local(2).isoformat(),
            rrule="FREQ=WEEKLY;COUNT=3",
        )
    )["uid"]
    rid = (await list_events(hass))[1]["recurrence_id"]
    await call(hass, "update_event", uid=uid, recurrence_id=rid, summary="moved")
    await call(hass, "update_event", uid=uid, rrule="")
    got = await list_events(hass)
    assert [e["summary"] for e in got] == ["w"]


# ---- cancel --------------------------------------------------------------


async def test_cancel_one_occurrence(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    uid = (
        await call(
            hass,
            "create_event",
            summary="Weekly",
            start_date_time=local(2).isoformat(),
            rrule="FREQ=WEEKLY;COUNT=3",
            attendees=[BART],
        )
    )["uid"]
    second = (await list_events(hass))[1]
    resp = await call(
        hass, "cancel_event", uid=uid, recurrence_id=second["recurrence_id"]
    )
    assert resp["notified"] == [BART]

    got = await list_events(hass)
    assert len(got) == 2 and second["start"] not in [e["start"] for e in got]

    cal = ics_of(outbox.sent[-1])
    assert str(cal["METHOD"]) == "CANCEL"
    (stub,) = cal.walk("VEVENT")
    assert stub.get("RECURRENCE-ID") is not None and int(stub["SEQUENCE"]) == 1

    # An attendee applying it loses exactly that occurrence.
    theirs = events.new_calendar()
    first = apply_message(email.message_from_bytes(outbox.sent[0].as_bytes()), theirs)
    apply_message(
        email.message_from_bytes(outbox.sent[-1].as_bytes()),
        theirs,
        managed=first.organizers,
    )
    assert events.find_event(theirs, uid).get("EXDATE") is not None


async def test_cancel_series(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    uid = (
        await call(
            hass,
            "create_event",
            summary="x",
            start_date_time=local(2).isoformat(),
            attendees=[BART],
        )
    )["uid"]
    await call(hass, "cancel_event", uid=uid)
    assert await list_events(hass) == []
    (stub,) = ics_of(outbox.sent[-1]).walk("VEVENT")
    assert str(stub["STATUS"]) == "CANCELLED" and int(stub["SEQUENCE"]) == 1
    assert uid not in entry.runtime_data.state.organizer
    assert uid not in entry.runtime_data.state.sent


async def test_cancel_not_sent_changes_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, outbox: Outbox
) -> None:
    uid = (
        await call(
            hass,
            "create_event",
            summary="x",
            start_date_time=local(2).isoformat(),
            attendees=[BART],
        )
    )["uid"]
    outbox.error = smtp.SmtpError("down")
    with pytest.raises(HomeAssistantError, match="down"):
        await call(hass, "cancel_event", uid=uid)
    assert [e["uid"] for e in await list_events(hass)] == [uid]


# ---- inbound guard -------------------------------------------------------


async def test_inbound_mail_never_changes_own_events(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    uid = (
        await call(
            hass, "create_event", summary="mine", start_date_time=local(2).isoformat()
        )
    )["uid"]
    forged = vev(
        uid, local(5), summary="hijacked", seq=9, organizer="tesla@example.com"
    )
    mailbox.add(mail("REQUEST", [forged], "m1"))
    mailbox.add(mail("CANCEL", [vev(uid, local(2), seq=10, organizer="x@y.z")], "m2"))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    (listed,) = await list_events(hass)
    assert listed["summary"] == "mine"


# ---- CalDAV --------------------------------------------------------------


async def test_create_and_cancel_on_caldav(
    hass: HomeAssistant,
    caldav_server: FakeCalDav,
    setup_caldav_entry: MockConfigEntry,
    outbox: Outbox,
) -> None:
    resp = await hass.services.async_call(
        DOMAIN,
        "create_event",
        {
            "entity_id": "calendar.vakantie",
            "summary": "Holiday",
            "start_date": str(local(10).date()),
            "end_date": str(local(17).date()),
        },
        blocking=True,
        return_response=True,
    )
    uid = resp["calendar.vakantie"]["uid"]
    puts = [u for m, u in caldav_server.writes() if m == "PUT"]
    assert len(puts) == 1 and uid.split("@")[0] in puts[0]

    await hass.services.async_call(
        DOMAIN,
        "cancel_event",
        {"entity_id": "calendar.vakantie", "uid": uid},
        blocking=True,
        return_response=True,
    )
    assert caldav_server.writes()[-1][0] == "DELETE"
    await hass.config_entries.async_unload(setup_caldav_entry.entry_id)
