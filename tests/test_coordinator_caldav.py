"""Poll cycle into a shared CalDAV calendar."""

from __future__ import annotations

import datetime

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar.coordinator import process_messages
from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.state import EntryState

from .caldav_fake import FakeCalDav
from .conftest import FakeMailbox
from .helpers import TZ, mail, vev

WEEKLY = {"FREQ": "WEEKLY", "COUNT": 4}


def future(days: int) -> datetime.datetime:
    return (dt_util.now() + datetime.timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


def ical(*comps) -> str:
    cal = events.new_calendar()
    for c in comps:
        cal.add_component(c)
    return cal.to_ical().decode()


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_invite_lands_in_caldav_hand_made_event_untouched(
    hass: HomeAssistant,
    caldav_server: FakeCalDav,
    mailbox: FakeMailbox,
    caldav_entry: MockConfigEntry,
) -> None:
    dentist = caldav_server.put_raw(
        "by-hand.ics", ical(vev("dentist", future(3), organizer=None))
    )
    before = caldav_server.resources[dentist]
    caldav_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(caldav_entry.entry_id)
    await hass.async_block_till_done()

    uid = mailbox.add(
        mail("REQUEST", [vev("trip@ext.com", future(2), rrule=WEEKLY)], "m1")
    )
    await poll(hass, caldav_entry)

    assert uid in mailbox.flagged
    assert ("PUT", caldav_server.base + "trip%40ext.com.ics") in caldav_server.writes()
    assert caldav_server.resources[dentist] == before
    assert set(caldav_entry.runtime_data.state.organizer) == {"trip@ext.com"}
    # The entity shows both: the mailed invite and the hand made event.
    assert events.all_uids(caldav_entry.runtime_data.data) == {
        "trip@ext.com",
        "dentist",
    }

    second = future(2) + datetime.timedelta(days=7)
    mailbox.add(mail("CANCEL", [vev("trip@ext.com", second, rid=second, seq=1)], "m2"))
    await poll(hass, caldav_entry)
    master = events.find_event(caldav_entry.runtime_data.data, "trip@ext.com")
    assert master.get("EXDATE") is not None

    mailbox.add(
        mail("CANCEL", [vev("trip@ext.com", future(2), rrule=WEEKLY, seq=2)], "m3")
    )
    await poll(hass, caldav_entry)
    assert caldav_server.writes()[-1] == (
        "DELETE",
        caldav_server.base + "trip%40ext.com.ics",
    )
    assert caldav_server.resources[dentist] == before
    await hass.config_entries.async_unload(caldav_entry.entry_id)


async def test_conflict_leaves_mail_unflagged_then_succeeds(
    hass: HomeAssistant,
    setup_caldav_entry: MockConfigEntry,
    caldav_server: FakeCalDav,
    mailbox: FakeMailbox,
) -> None:
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, setup_caldav_entry)
    href = caldav_server.base + "a.ics"

    # Someone edits "a" in Nextcloud between our load and save.
    uid = mailbox.add(
        mail("REQUEST", [vev("a", future(2), seq=1, summary="moved")], "m2")
    )
    store = setup_caldav_entry.runtime_data.store
    real_load = store.async_load

    async def load_then_touch():
        result = await real_load()
        caldav_server.touch(href)
        return result

    store.async_load = load_then_touch
    await poll(hass, setup_caldav_entry)
    assert not setup_caldav_entry.runtime_data.last_update_success
    assert uid not in mailbox.flagged

    store.async_load = real_load
    await poll(hass, setup_caldav_entry)
    assert uid in mailbox.flagged
    assert "SUMMARY:moved" in caldav_server.resources[href][1]
    await hass.config_entries.async_unload(setup_caldav_entry.entry_id)


async def test_caldav_auth_failure_starts_reauth(
    hass: HomeAssistant,
    setup_caldav_entry: MockConfigEntry,
    caldav_server: FakeCalDav,
) -> None:
    caldav_server.password = "rotated"
    await poll(hass, setup_caldav_entry)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    await hass.config_entries.async_unload(setup_caldav_entry.entry_id)


async def test_caldav_down_skips_poll_without_flagging(
    hass: HomeAssistant,
    setup_caldav_entry: MockConfigEntry,
    caldav_server: FakeCalDav,
    mailbox: FakeMailbox,
) -> None:
    caldav_server.report_status = 503
    uid = mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, setup_caldav_entry)
    assert uid not in mailbox.flagged
    assert caldav_server.writes() == []
    await hass.config_entries.async_unload(setup_caldav_entry.entry_id)


def test_retention_on_shared_store_prunes_managed_only() -> None:
    cal = events.new_calendar()
    old = datetime.datetime(2025, 1, 1, 9, tzinfo=TZ)
    events.upsert_event(cal, vev("theirs", old, organizer=None))
    state = EntryState(organizer={"mine": "boss@ext.com"})
    outcome = process_messages(
        "t",
        [("1", mail("REQUEST", [vev("mine", old)], "m1"))],
        cal,
        state,
        retention_cutoff=datetime.datetime(2026, 10, 1, tzinfo=TZ),
        managed_only=True,
    )
    assert outcome.pruned == ["mine"]
    assert events.all_uids(cal) == {"theirs"}
