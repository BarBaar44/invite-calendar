"""Poll cycle end to end: mailbox in, calendar entity out."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.invite_calendar.const import (
    DOMAIN,
    EVENT_INVITE_RECEIVED,
    EVENT_UPDATED,
    MAX_MESSAGE_ATTEMPTS,
)
from custom_components.invite_calendar.coordinator import process_messages
from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.mail import imap
from custom_components.invite_calendar.state import EntryState
from custom_components.invite_calendar.store import StoreError
from custom_components.invite_calendar.store.ics_file import IcsFileStore

from .conftest import FakeMailbox
from .helpers import BROKEN_MAIL, TZ, mail, vev

ENTITY = "calendar.tesla"
WEEKLY = {"FREQ": "WEEKLY", "COUNT": 4}


def future(days: int) -> datetime.datetime:
    """Local 09:00, `days` from now."""
    return (dt_util.now() + datetime.timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


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


async def test_invite_shows_in_calendar(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    updated = async_capture_events(hass, EVENT_UPDATED)
    received = async_capture_events(hass, EVENT_INVITE_RECEIVED)
    uid = mailbox.add(
        mail("REQUEST", [vev("trip1", future(2), summary="Dentist")], "m1")
    )

    await poll(hass, setup_entry)

    got = await get_events(hass)
    assert [e["summary"] for e in got] == ["Dentist"]
    assert got[0]["location"] == "Utrecht"
    assert uid in mailbox.flagged
    assert updated[-1].data == {
        "entity_id": ENTITY,
        "added": ["trip1"],
        "updated": [],
        "removed": [],
    }
    assert [e.data["uid"] for e in received] == ["trip1"]
    assert received[0].data["organizer"] == "boss@ext.com"
    assert hass.states.get(ENTITY).attributes["message"] == "Dentist"


async def test_quiet_poll_fires_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, setup_entry)
    updated = async_capture_events(hass, EVENT_UPDATED)
    received = async_capture_events(hass, EVENT_INVITE_RECEIVED)
    await poll(hass, setup_entry)
    assert updated == [] and received == []


async def test_cancel_one_occurrence_then_series(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    start = future(2)
    second = start + datetime.timedelta(days=7)
    mailbox.add(mail("REQUEST", [vev("rec", start, rrule=WEEKLY)], "m1"))
    await poll(hass, setup_entry)
    assert len(await get_events(hass)) == 4

    mailbox.add(mail("CANCEL", [vev("rec", second, rid=second, seq=1)], "m2"))
    await poll(hass, setup_entry)
    starts = [dt_util.parse_datetime(e["start"]) for e in await get_events(hass)]
    assert len(starts) == 3 and second not in starts

    removed = async_capture_events(hass, EVENT_UPDATED)
    mailbox.add(mail("CANCEL", [vev("rec", start, rrule=WEEKLY, seq=2)], "m3"))
    await poll(hass, setup_entry)
    assert await get_events(hass) == []
    assert removed[-1].data["removed"] == ["rec"]
    assert "rec" not in setup_entry.runtime_data.state.organizer


async def test_override_moves_one_occurrence(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    start = future(2)
    second = start + datetime.timedelta(days=7)
    mailbox.add(
        mail(
            "REQUEST",
            [
                vev("rec", start, rrule=WEEKLY),
                vev(
                    "rec",
                    second + datetime.timedelta(hours=3),
                    rid=second,
                    summary="Moved",
                ),
            ],
            "m1",
        )
    )
    await poll(hass, setup_entry)
    got = await get_events(hass)
    assert len(got) == 4
    moved = [e for e in got if e["summary"] == "Moved"]
    assert dt_util.parse_datetime(moved[0]["start"]) == second + datetime.timedelta(
        hours=3
    )


async def test_store_error_leaves_mail_unflagged(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    uid = mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    with patch.object(IcsFileStore, "save", side_effect=StoreError("disk full")):
        await poll(hass, setup_entry)
    assert uid not in mailbox.flagged
    assert "a" not in setup_entry.runtime_data.state.organizer

    await poll(hass, setup_entry)
    assert uid in mailbox.flagged
    assert [e["summary"] for e in await get_events(hass)] == ["S a"]


async def test_flag_failure_reapplies_idempotently(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    mailbox.flag_error = imap.ImapConnectError("gone")
    await poll(hass, setup_entry)
    mailbox.flag_error = None
    await poll(hass, setup_entry)
    assert len(await get_events(hass)) == 1


async def test_broken_message_given_up_after_attempts(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    uid = mailbox.add(BROKEN_MAIL)
    for attempt in range(1, MAX_MESSAGE_ATTEMPTS):
        await poll(hass, setup_entry)
        assert uid not in mailbox.flagged
        assert setup_entry.runtime_data.state.failed == {"<broken>": attempt}

    with patch(
        "custom_components.invite_calendar.coordinator.persistent_notification.async_create"
    ) as notify:
        await poll(hass, setup_entry)
    assert uid in mailbox.flagged
    assert setup_entry.runtime_data.state.failed == {}
    assert notify.call_count == 1
    assert "<broken>" in notify.call_args.args[1]
    assert await get_events(hass) == []


async def test_imap_down_keeps_calendar_available(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, setup_entry)
    mailbox.fetch_error = imap.ImapConnectError("timeout")
    await poll(hass, setup_entry)
    assert setup_entry.runtime_data.last_update_success
    assert hass.states.get(ENTITY).state != "unavailable"
    assert len(await get_events(hass)) == 1


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    mailbox.fetch_error = imap.ImapAuthError("AUTHENTICATIONFAILED")
    await poll(hass, setup_entry)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_poll_service(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await hass.services.async_call(DOMAIN, "poll", {"entity_id": ENTITY}, blocking=True)
    assert len(await get_events(hass)) == 1

    with (
        patch.object(IcsFileStore, "load", side_effect=StoreError("unreadable")),
        pytest.raises(HomeAssistantError, match="unreadable"),
    ):
        await hass.services.async_call(
            DOMAIN, "poll", {"entity_id": ENTITY}, blocking=True
        )


async def test_state_persisted(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    mailbox: FakeMailbox,
    hass_storage: dict[str, Any],
) -> None:
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, setup_entry)
    key = f"{DOMAIN}.{setup_entry.entry_id}"
    assert hass_storage[key]["data"]["organizer"] == {"a": "boss@ext.com"}


def test_process_messages_retention_and_state() -> None:
    cal = events.new_calendar()
    old = datetime.datetime(2025, 1, 1, 9, tzinfo=TZ)
    soon = datetime.datetime(2026, 12, 1, 9, tzinfo=TZ)
    state = EntryState()
    outcome = process_messages(
        "t",
        [
            ("1", mail("REQUEST", [vev("old", old)], "m1")),
            ("2", mail("REQUEST", [vev("new", soon)], "m2")),
        ],
        cal,
        state,
        retention_cutoff=datetime.datetime(2026, 10, 1, tzinfo=TZ),
    )
    assert outcome.to_flag == ["1", "2"]
    assert outcome.pruned == ["old"]
    assert events.all_uids(cal) == {"new"}
    assert state.organizer == {"new": "boss@ext.com"}


async def test_pending_survives_restart(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    """A REQUEST that could not be sent before a restart is still resent."""
    from custom_components.invite_calendar.state import EntryState, StateStore

    store = StateStore(hass, mock_config_entry.entry_id)
    await store.async_save(EntryState(organizer={"x": "a@b"}, pending={"x": 2}))
    loaded = await StateStore(hass, mock_config_entry.entry_id).async_load()
    assert loaded.pending == {"x": 2}
