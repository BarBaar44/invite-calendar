"""RSVPs, missing location replies, accept_event and list_events."""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar.const import (
    CONF_ACCEPT_POLICY,
    CONF_MISSING_LOCATION_REPLY,
    CONF_MISSING_LOCATION_TEXT,
    DOMAIN,
)
from custom_components.invite_calendar.mail import smtp
from custom_components.invite_calendar.store import StoreError
from custom_components.invite_calendar.store.ics_file import IcsFileStore

from .conftest import FakeMailbox, Outbox
from .helpers import mail, vev

ENTITY = "calendar.tesla"
WEEKLY = {"FREQ": "WEEKLY", "COUNT": 4}


def future(days: int) -> datetime.datetime:
    return (dt_util.now() + datetime.timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


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


# ---- accept policies ------------------------------------------------------


async def test_never_sends_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry)
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, entry)
    assert outbox.sent == []


async def test_always_accepts_once_per_series_and_sequence(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    rid = future(9)
    mailbox.add(
        mail(
            "REQUEST",
            [
                vev("r", future(2), rrule=WEEKLY),
                vev("r", rid, rid=rid, summary="moved"),
            ],
            "m1",
        )
    )
    await poll(hass, entry)
    assert outbox.accepted() == [("r", 0)]  # master only, not the override

    await poll(hass, entry)
    assert outbox.accepted() == [("r", 0)]  # quiet poll: no duplicate

    mailbox.add(mail("REQUEST", [vev("r", future(2), rrule=WEEKLY, seq=1)], "m2"))
    await poll(hass, entry)
    assert outbox.accepted() == [("r", 0), ("r", 1)]  # re-accept after bump
    assert entry.runtime_data.state.accepted == {"r": 1}


async def test_if_location_waits_for_a_location(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "if_location"})
    mailbox.add(mail("REQUEST", [vev("a", future(2), location=None)], "m1"))
    await poll(hass, entry)
    assert outbox.accepted() == []

    mailbox.add(mail("REQUEST", [vev("a", future(2), seq=1, location="Delft")], "m2"))
    await poll(hass, entry)
    assert outbox.accepted() == [("a", 1)]


async def test_lone_override_is_accepted(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    """An invite to one instance of someone else's series has no master."""
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    rid = future(3)
    mailbox.add(mail("REQUEST", [vev("x", rid, rid=rid)], "m1"))
    await poll(hass, entry)
    assert outbox.accepted() == [("x", 0)]


async def test_past_events_not_accepted(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    mailbox.add(mail("REQUEST", [vev("old", future(-3))], "m1"))
    await poll(hass, entry)
    assert outbox.accepted() == []


async def test_failed_send_is_retried_next_poll(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    outbox.error = smtp.SmtpError("server down")
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, entry)
    assert entry.runtime_data.state.accepted == {}
    assert entry.runtime_data.last_update_success  # the calendar itself is fine

    outbox.error = None
    await poll(hass, entry)
    assert outbox.accepted() == [("a", 0)]
    assert entry.runtime_data.state.accepted == {"a": 0}


async def test_refused_rsvp_not_retried_until_update(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    outbox.error = smtp.SmtpRefusedError("recipient refused")
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, entry)
    assert entry.runtime_data.state.rsvp_failed == {"a": 0}

    outbox.error = None
    await poll(hass, entry)
    assert outbox.accepted() == []

    mailbox.add(mail("REQUEST", [vev("a", future(2), seq=1)], "m2"))
    await poll(hass, entry)
    assert outbox.accepted() == [("a", 1)]
    assert entry.runtime_data.state.rsvp_failed == {}


# ---- accept_event --------------------------------------------------------


async def accept(hass: HomeAssistant, uid: str) -> dict[str, Any]:
    resp = await hass.services.async_call(
        DOMAIN,
        "accept_event",
        {"entity_id": ENTITY, "uid": uid},
        blocking=True,
        return_response=True,
    )
    return resp[ENTITY]


async def test_manual_policy_only_accepts_on_request(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    mailbox.add(mail("REQUEST", [vev("trip", future(2))], "m1"))
    await poll(hass, entry)
    assert outbox.accepted() == []

    assert await accept(hass, "trip") == {"uid": "trip", "sequence": 0, "sent": True}
    assert await accept(hass, "trip") == {"uid": "trip", "sequence": 0, "sent": False}
    assert outbox.accepted() == [("trip", 0)]

    await poll(hass, entry)  # the scan never sends under manual
    assert outbox.accepted() == [("trip", 0)]


async def test_accept_event_errors(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    with pytest.raises(ServiceValidationError, match="No event"):
        await accept(hass, "nope")

    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, entry)
    outbox.error = smtp.SmtpError("down")
    with pytest.raises(HomeAssistantError, match="down"):
        await accept(hass, "a")
    assert entry.runtime_data.state.accepted == {}


async def test_accept_event_not_managed(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    """An event put in the calendar by hand is never answered."""
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    store: IcsFileStore = entry.runtime_data.store
    cal, snap = await store.async_load()
    from custom_components.invite_calendar.ical import events

    events.upsert_event(cal, vev("byhand", future(2)))
    await store.async_save(cal, snap)
    await poll(hass, entry)
    with pytest.raises(ServiceValidationError, match="did not arrive by mail"):
        await accept(hass, "byhand")


# ---- missing location reply ---------------------------------------------


async def test_missing_location_reply_once(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(
        hass,
        entry,
        **{
            CONF_MISSING_LOCATION_REPLY: True,
            CONF_MISSING_LOCATION_TEXT: "The car needs to know where.",
        },
    )
    mailbox.add(mail("REQUEST", [vev("a", future(2), location=None)], "m1"))
    mailbox.add(mail("REQUEST", [vev("b", future(2))], "m2"))
    await poll(hass, entry)
    (reply,) = outbox.plain()
    assert reply["To"] == "boss@ext.com"
    assert reply["In-Reply-To"] == "<m1>"
    assert "The car needs to know where." in reply.get_payload(decode=True).decode()

    await poll(hass, entry)
    assert len(outbox.plain()) == 1  # quiet poll: no second ask


async def test_no_reply_when_save_fails(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    """A retried message must not ask twice."""
    from unittest.mock import patch

    await start(hass, entry, **{CONF_MISSING_LOCATION_REPLY: True})
    mailbox.add(mail("REQUEST", [vev("a", future(2), location=None)], "m1"))
    with patch.object(IcsFileStore, "save", side_effect=StoreError("disk full")):
        await poll(hass, entry)
    assert outbox.sent == []
    await poll(hass, entry)
    assert len(outbox.plain()) == 1


async def test_no_reply_to_own_address(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(
        hass, entry, **{CONF_MISSING_LOCATION_REPLY: True, CONF_ACCEPT_POLICY: "always"}
    )
    own = vev("mine", future(2), location=None, organizer="tesla@example.com")
    mailbox.add(mail("REQUEST", [own], "m1"))
    await poll(hass, entry)
    assert outbox.sent == []


# ---- list_events ---------------------------------------------------------


async def list_events(hass: HomeAssistant, **data: Any) -> list[dict[str, Any]]:
    resp = await hass.services.async_call(
        DOMAIN,
        "list_events",
        {"entity_id": ENTITY, **data},
        blocking=True,
        return_response=True,
    )
    return resp[ENTITY]["events"]


async def test_list_events(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    ev = vev("r", future(2), rrule=WEEKLY, summary="Trip")
    ev.add("attendee", "mailto:Bart@example.com")
    mailbox.add(mail("REQUEST", [ev], "m1"))
    await poll(hass, entry)

    got = await list_events(hass, duration={"days": 10})
    assert [e["uid"] for e in got] == ["r", "r"]
    first = got[0]
    assert first["organizer"] == "boss@ext.com"
    assert first["attendees"] == ["bart@example.com"]
    assert first["managed"] and first["accepted"]
    assert first["recurrence_id"] is not None
    assert first["location"] == "Utrecht" and first["summary"] == "Trip"
    assert dt_util.parse_datetime(first["start"]) == future(2)

    # Default window: 7 days from now.
    assert len(await list_events(hass)) == 1


async def test_list_events_recurrence_id_only_for_series(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    """A single event has recurrence_id None (#10); every occurrence of a
    series has one, which update/cancel/decline accept."""
    await start(hass, entry)
    mailbox.add(mail("REQUEST", [vev("single", future(1))], "m1"))
    mailbox.add(mail("REQUEST", [vev("r", future(2), rrule=WEEKLY)], "m2"))
    await poll(hass, entry)

    got = await list_events(hass, duration={"days": 10})
    single = [e for e in got if e["uid"] == "single"]
    series = [e for e in got if e["uid"] == "r"]
    assert len(single) == 1 and single[0]["recurrence_id"] is None
    assert len(series) == 2
    assert all(e["recurrence_id"] for e in series)
    assert len({e["recurrence_id"] for e in series}) == 2


async def test_list_events_naive_times_are_local(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry)
    when = future(2)
    mailbox.add(mail("REQUEST", [vev("a", when)], "m1"))
    await poll(hass, entry)
    naive = when.replace(tzinfo=None)
    # A window ending one minute after the local start includes the event;
    # read as UTC it would end before it (Amsterdam is ahead of UTC).
    got = await list_events(
        hass,
        start=(naive - datetime.timedelta(minutes=30)).isoformat(),
        end=(naive + datetime.timedelta(minutes=1)).isoformat(),
    )
    assert [e["uid"] for e in got] == ["a"]

    with pytest.raises(ServiceValidationError):
        await list_events(hass, start=naive.isoformat(), end=naive.isoformat())
