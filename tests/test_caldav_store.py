"""CalDAV store against an in memory server (store/caldav.py)."""

from __future__ import annotations

import datetime

import pytest
from homeassistant.core import HomeAssistant
from icalendar import Calendar

from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.store import StoreAuthError, StoreError
from custom_components.invite_calendar.store.caldav import (
    CalDavConflictError,
    CalDavNotCalendarError,
    CalDavSettings,
    CalDavStore,
    normalize_url,
)

from .caldav_fake import FakeCalDav
from .helpers import TZ, vev

T0 = datetime.datetime(2026, 10, 10, 9, 0, tzinfo=TZ)


def ical(*comps) -> str:
    cal = events.new_calendar()
    for c in comps:
        cal.add_component(c)
    cal.add_missing_timezones()
    return cal.to_ical().decode()


@pytest.fixture
def server() -> FakeCalDav:
    srv = FakeCalDav()
    srv.put_raw("dentist-by-hand.ics", ical(vev("dentist", T0, organizer=None)))
    srv.put_raw("invite1-weird-name.ics", ical(vev("invite1", T0)))
    return srv


def store_for(
    hass: HomeAssistant, srv: FakeCalDav, password: str = "app-pw"
) -> CalDavStore:
    return CalDavStore(
        hass,
        CalDavSettings(srv.base.rstrip("/"), "bart", password),
        session=srv,  # type: ignore[arg-type]
    )


async def test_load_reads_all_events(hass: HomeAssistant, server: FakeCalDav) -> None:
    cal, snap = await store_for(hass, server).async_load()
    assert events.all_uids(cal) == {"dentist", "invite1"}
    assert snap.hrefs["invite1"] == server.base + "invite1-weird-name.ics"
    assert snap.etags[snap.hrefs["invite1"]].startswith('"e')
    assert [str(c["TZID"]) for c in cal.walk("VTIMEZONE")] == ["Europe/Amsterdam"]


async def test_save_writes_only_the_diff(
    hass: HomeAssistant, server: FakeCalDav
) -> None:
    store = store_for(hass, server)
    before_dentist = server.resources[server.base + "dentist-by-hand.ics"]
    cal, snap = await store.async_load()
    events.upsert_event(cal, vev("new@ext.com", T0, summary="New"))
    events.bump_sequence(events.find_event(cal, "invite1"))

    changes = await store.async_save(cal, snap)
    assert changes.added == ["new@ext.com"] and changes.updated == ["invite1"]
    writes = server.writes()
    assert ("PUT", server.base + "invite1-weird-name.ics") in writes
    assert ("PUT", server.base + "new%40ext.com.ics") in writes
    assert not any("dentist" in u for _, u in writes)
    assert server.resources[server.base + "dentist-by-hand.ics"] == before_dentist

    # New resources are created with If-None-Match, existing with If-Match.
    put_headers = {u: h for m, u, h in server.log if m == "PUT"}
    assert put_headers[server.base + "new%40ext.com.ics"]["If-None-Match"] == "*"
    assert (
        put_headers[server.base + "invite1-weird-name.ics"]["If-Match"]
        == snap.etags[server.base + "invite1-weird-name.ics"]
    )

    # Each PUT body is a valid single UID VCALENDAR with its timezone.
    body = Calendar.from_ical(server.resources[server.base + "new%40ext.com.ics"][1])
    assert [str(c["UID"]) for c in body.walk("VEVENT")] == ["new@ext.com"]
    assert [str(c["TZID"]) for c in body.walk("VTIMEZONE")] == ["Europe/Amsterdam"]


async def test_delete_uses_loaded_href(hass: HomeAssistant, server: FakeCalDav) -> None:
    store = store_for(hass, server)
    cal, snap = await store.async_load()
    events.remove_event(cal, "invite1")
    changes = await store.async_save(cal, snap)
    assert changes.removed == ["invite1"]
    assert server.writes() == [("DELETE", server.base + "invite1-weird-name.ics")]


async def test_no_change_writes_nothing(
    hass: HomeAssistant, server: FakeCalDav
) -> None:
    store = store_for(hass, server)
    cal, snap = await store.async_load()
    assert not await store.async_save(cal, snap)
    assert server.writes() == []


async def test_concurrent_edit_is_a_conflict(
    hass: HomeAssistant, server: FakeCalDav
) -> None:
    """Someone edits the event in Nextcloud between our load and save."""
    store = store_for(hass, server)
    cal, snap = await store.async_load()
    events.bump_sequence(events.find_event(cal, "invite1"))
    server.touch(server.base + "invite1-weird-name.ics")
    with pytest.raises(CalDavConflictError):
        await store.async_save(cal, snap)


async def test_put_error_raises_after_trying_all(
    hass: HomeAssistant, server: FakeCalDav
) -> None:
    store = store_for(hass, server)
    cal, snap = await store.async_load()
    events.upsert_event(cal, vev("a", T0))
    events.upsert_event(cal, vev("b", T0))
    server.fail_put = 507
    with pytest.raises(StoreError, match="HTTP 507"):
        await store.async_save(cal, snap)
    assert len([w for w in server.writes() if w[0] == "PUT"]) == 2


async def test_auth_and_not_calendar(hass: HomeAssistant, server: FakeCalDav) -> None:
    with pytest.raises(StoreAuthError):
        await store_for(hass, server, password="wrong").async_load()
    other = CalDavStore(
        hass,
        CalDavSettings(server.base + "nope/", "bart", "app-pw"),
        session=server,  # type: ignore[arg-type]
    )
    with pytest.raises(CalDavNotCalendarError):
        await other.async_load()
    server.report_status = 500
    with pytest.raises(StoreError):
        await store_for(hass, server).async_load()


async def test_unparseable_resource_is_skipped(
    hass: HomeAssistant, server: FakeCalDav
) -> None:
    server.put_raw("broken.ics", "BEGIN:VCALENDAR\r\nthis is broken")
    store = store_for(hass, server)
    cal, snap = await store.async_load()
    assert events.all_uids(cal) == {"dentist", "invite1"}
    assert not await store.async_save(cal, snap)
    assert server.writes() == []


def test_normalize_url() -> None:
    assert normalize_url(" https://h/cal ") == "https://h/cal/"
    assert normalize_url("https://h/cal//") == "https://h/cal/"
