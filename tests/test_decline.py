"""decline_event: decline an invitation or one occurrence, on request."""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from icalendar import Calendar
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.invite_calendar.const import (
    CONF_ACCEPT_POLICY,
    DOMAIN,
    EVENT_UPDATED,
)
from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.mail import smtp

from .conftest import FakeMailbox, Outbox
from .helpers import mail, vev

ENTITY = "calendar.tesla"
WEEKLY = {"FREQ": "WEEKLY", "COUNT": 4}


def future(days: int) -> datetime.datetime:
    return (dt_util.now() + datetime.timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


def rid_text(value: datetime.datetime | datetime.date) -> str:
    """recurrence_id as list_events returns it."""
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    outbox: Outbox,
    mock_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    mock_config_entry.add_to_hass(hass)
    return mock_config_entry


async def start(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def decline(
    hass: HomeAssistant, uid: str, recurrence_id: str | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {"entity_id": ENTITY, "uid": uid}
    if recurrence_id is not None:
        data["recurrence_id"] = recurrence_id
    resp = await hass.services.async_call(
        DOMAIN, "decline_event", data, blocking=True, return_response=True
    )
    return resp[ENTITY]


async def accept(hass: HomeAssistant, uid: str) -> dict[str, Any]:
    resp = await hass.services.async_call(
        DOMAIN,
        "accept_event",
        {"entity_id": ENTITY, "uid": uid},
        blocking=True,
        return_response=True,
    )
    return resp[ENTITY]


async def get_events(hass: HomeAssistant, days: int = 60) -> list[dict[str, Any]]:
    now = dt_util.now()
    resp = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": ENTITY,
            "start_date_time": now,
            "end_date_time": now + datetime.timedelta(days=days),
        },
        blocking=True,
        return_response=True,
    )
    return resp[ENTITY]["events"]


def replies(outbox: Outbox) -> list[tuple[str, str, str | None]]:
    """(UID, PARTSTAT, RECURRENCE-ID local date or None) of every REPLY."""
    out = []
    for msg in outbox.sent:
        for part in msg.walk():
            if part.get_content_type() != "text/calendar":
                continue
            cal = Calendar.from_ical(part.get_payload(decode=True))
            for ev in cal.walk("VEVENT"):
                rid = ev.get("RECURRENCE-ID")
                out.append(
                    (
                        str(ev["UID"]),
                        str(ev["ATTENDEE"].params["PARTSTAT"]),
                        dt_util.as_local(events.aware(rid.dt)).date().isoformat()
                        if rid is not None
                        else None,
                    )
                )
    return out


# ---- whole event -----------------------------------------------------------


async def test_decline_whole_event(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    mailbox.add(mail("REQUEST", [vev("trip", future(2))], "m1"))
    await poll(hass, entry)
    updated = async_capture_events(hass, EVENT_UPDATED)

    assert await decline(hass, "trip") == {"uid": "trip", "sequence": 0, "sent": True}

    assert replies(outbox) == [("trip", "DECLINED", None)]
    assert await get_events(hass) == []
    assert updated[-1].data["removed"] == ["trip"]
    assert entry.runtime_data.state.declined["trip"]["whole"] is True

    # Idempotent, even though the event is gone from the calendar now.
    assert await decline(hass, "trip") == {"uid": "trip", "sequence": 0, "sent": False}
    assert len(outbox.sent) == 1


async def test_declined_event_stays_out_until_a_new_version(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    mailbox.add(mail("REQUEST", [vev("trip", future(2))], "m1"))
    await poll(hass, entry)
    await decline(hass, "trip")

    # The organizer resends the same version: still left out.
    mailbox.add(mail("REQUEST", [vev("trip", future(2))], "m2"))
    await poll(hass, entry)
    assert await get_events(hass) == []

    # A new version is decided again: back in the calendar, unanswered.
    mailbox.add(mail("REQUEST", [vev("trip", future(3), seq=1)], "m3"))
    await poll(hass, entry)
    assert [e["summary"] for e in await get_events(hass)] == ["S trip"]
    assert len(outbox.sent) == 1


async def test_decline_after_accept(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    """A deliberate decline overrides an earlier acceptance."""
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    mailbox.add(mail("REQUEST", [vev("trip", future(2))], "m1"))
    await poll(hass, entry)
    assert entry.runtime_data.state.accepted == {"trip": 0}

    assert (await decline(hass, "trip"))["sent"] is True
    assert replies(outbox) == [("trip", "ACCEPTED", None), ("trip", "DECLINED", None)]
    assert entry.runtime_data.state.accepted == {}

    await poll(hass, entry)  # the scan never accepts it again
    assert len(outbox.sent) == 2
    assert await get_events(hass) == []


async def test_decline_send_failure_changes_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    mailbox.add(mail("REQUEST", [vev("trip", future(2))], "m1"))
    await poll(hass, entry)
    outbox.error = smtp.SmtpError("down")

    with pytest.raises(HomeAssistantError, match="down"):
        await decline(hass, "trip")
    assert entry.runtime_data.state.declined == {}
    assert len(await get_events(hass)) == 1


# ---- one occurrence ---------------------------------------------------------


async def test_decline_one_occurrence(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    first = future(2)
    mailbox.add(mail("REQUEST", [vev("weekly", first, rrule=WEEKLY)], "m1"))
    await poll(hass, entry)
    second = first + datetime.timedelta(days=7)
    third = first + datetime.timedelta(days=14)

    got = await decline(hass, "weekly", rid_text(second))
    assert got == {
        "uid": "weekly",
        "sequence": 0,
        "recurrence_id": rid_text(second),
        "sent": True,
    }
    assert replies(outbox) == [("weekly", "DECLINED", second.date().isoformat())]
    starts = [e["start"] for e in await get_events(hass)]
    assert second.isoformat() not in starts
    assert len(starts) == 3

    # Same occurrence again: nothing sent. Another one: added to the record.
    assert (await decline(hass, "weekly", rid_text(second)))["sent"] is False
    assert (await decline(hass, "weekly", rid_text(third)))["sent"] is True
    assert len(await get_events(hass)) == 2
    record = entry.runtime_data.state.declined["weekly"]
    assert record["whole"] is False and len(record["occurrences"]) == 2

    # The series itself can still be accepted, and the declines stay.
    assert (await accept(hass, "weekly"))["sent"] is True
    await poll(hass, entry)
    assert len(await get_events(hass)) == 2


async def test_decline_occurrence_of_all_day_series(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    """An all day series keeps a DATE EXDATE."""
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    day = future(2).date()
    mailbox.add(mail("REQUEST", [vev("camp", day, rrule=WEEKLY)], "m1"))
    await poll(hass, entry)
    second = day + datetime.timedelta(days=7)

    assert (await decline(hass, "camp", rid_text(second)))["sent"] is True
    assert replies(outbox) == [("camp", "DECLINED", second.isoformat())]
    starts = [e["start"] for e in await get_events(hass)]
    assert second.isoformat() not in starts and len(starts) == 3

    master = events.find_event(entry.runtime_data.data, "camp")
    exdate = master["EXDATE"]
    exdates = exdate if isinstance(exdate, list) else [exdate]
    values = [d.dt for group in exdates for d in group.dts]
    assert values == [second]


# ---- refused calls -----------------------------------------------------------


async def test_decline_errors(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    with pytest.raises(ServiceValidationError, match="No event"):
        await decline(hass, "nope")

    mailbox.add(mail("REQUEST", [vev("single", future(2))], "m1"))
    mailbox.add(mail("REQUEST", [vev("weekly", future(3), rrule=WEEKLY)], "m2"))
    await poll(hass, entry)

    with pytest.raises(ServiceValidationError, match="does not repeat"):
        await decline(hass, "single", rid_text(future(2)))
    with pytest.raises(ServiceValidationError, match="no occurrence"):
        await decline(hass, "weekly", rid_text(future(4)))
    with pytest.raises(ServiceValidationError, match="no occurrence"):
        await decline(hass, "weekly", "garbage")
    assert outbox.sent == []


async def test_decline_not_managed_or_own(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    store = entry.runtime_data.store
    cal, snap = await store.async_load()
    events.upsert_event(cal, vev("byhand", future(2)))
    await store.async_save(cal, snap)
    await poll(hass, entry)
    with pytest.raises(ServiceValidationError, match="did not arrive by mail"):
        await decline(hass, "byhand")

    created = await hass.services.async_call(
        DOMAIN,
        "create_event",
        {"entity_id": ENTITY, "summary": "Own", "start_date_time": future(3)},
        blocking=True,
        return_response=True,
    )
    with pytest.raises(ServiceValidationError, match="organizes it itself"):
        await decline(hass, created[ENTITY]["uid"])
    assert outbox.sent == []
