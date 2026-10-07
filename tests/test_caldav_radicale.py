"""CalDAV store against a real server: Radicale on localhost.

Catches what the in memory fake can't: real multistatus XML, real ETags,
real If-Match / If-None-Match handling. Skipped when radicale isn't
installed.
"""

from __future__ import annotations

import datetime
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.store import StoreAuthError
from custom_components.invite_calendar.store.caldav import (
    CalDavConflictError,
    CalDavNotCalendarError,
    CalDavSettings,
    CalDavStore,
    CalendarInfo,
    async_discover,
)

from .helpers import TZ, vev

pytest.importorskip("radicale")

T0 = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
USER, PASSWORD = "bart", "app-pw"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def radicale(tmp_path: Path, socket_enabled) -> Generator[str]:
    """Base URL of a running Radicale with one user."""
    port = _free_port()
    users = tmp_path / "users"
    users.write_text(f"{USER}:{PASSWORD}\n")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "radicale",
            "--server-hosts",
            f"127.0.0.1:{port}",
            "--storage-filesystem-folder",
            str(tmp_path / "storage"),
            "--auth-type",
            "htpasswd",
            "--auth-htpasswd-filename",
            str(users),
            "--auth-htpasswd-encryption",
            "plain",
            "--logging-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"radicale did not start: {proc.stderr.read().decode()}")
            time.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{port}/{USER}/"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
async def session(radicale: str) -> AsyncGenerator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
async def calendar_url(radicale: str, session: aiohttp.ClientSession) -> str:
    """A fresh calendar collection, created with MKCALENDAR."""
    url = radicale + "test/"
    async with session.request(
        "MKCALENDAR",
        url,
        headers={"Authorization": aiohttp.encode_basic_auth(USER, PASSWORD)},
    ) as resp:
        assert resp.status == 201, await resp.text()
    return url


def make_store(
    hass: HomeAssistant,
    url: str,
    session: aiohttp.ClientSession,
    password: str = PASSWORD,
) -> CalDavStore:
    return CalDavStore(hass, CalDavSettings(url, USER, password), session=session)


async def test_round_trip(
    hass: HomeAssistant, calendar_url: str, session: aiohttp.ClientSession
) -> None:
    store = make_store(hass, calendar_url, session)

    cal, snap = await store.async_load()
    assert events.all_uids(cal) == set()

    events.replace_series(
        cal,
        [
            vev("rec@ext.com", T0, rrule={"FREQ": "WEEKLY", "COUNT": 4}),
            vev(
                "rec@ext.com",
                T0 + datetime.timedelta(days=7, hours=2),
                rid=T0 + datetime.timedelta(days=7),
            ),
        ],
    )
    events.upsert_event(cal, vev("single", T0))
    cal.add_missing_timezones()
    changes = await store.async_save(cal, snap)
    assert changes.added == ["rec@ext.com", "single"]

    # Read back: master plus override stay one resource, ETags present.
    cal, snap = await store.async_load()
    assert events.all_uids(cal) == {"rec@ext.com", "single"}
    assert len([c for c in cal.walk("VEVENT") if str(c["UID"]) == "rec@ext.com"]) == 2
    assert snap.hrefs["rec@ext.com"].startswith(calendar_url)
    assert all(snap.etags.get(h) for h in snap.hrefs.values())

    # Update and delete with If-Match.
    events.bump_sequence(events.find_event(cal, "single"))
    events.remove_event(cal, "rec@ext.com")
    changes = await store.async_save(cal, snap)
    assert changes.updated == ["single"] and changes.removed == ["rec@ext.com"]

    cal, _ = await store.async_load()
    assert events.all_uids(cal) == {"single"}
    assert int(events.find_event(cal, "single")["SEQUENCE"]) == 1


async def test_concurrent_edit_conflicts(
    hass: HomeAssistant, calendar_url: str, session: aiohttp.ClientSession
) -> None:
    store = make_store(hass, calendar_url, session)
    cal, snap = await store.async_load()
    events.upsert_event(cal, vev("a", T0))
    await store.async_save(cal, snap)

    mine, my_snap = await store.async_load()
    theirs, their_snap = await store.async_load()
    events.bump_sequence(events.find_event(theirs, "a"))
    await store.async_save(theirs, their_snap)

    events.find_event(mine, "a")["SUMMARY"] = "mine"
    with pytest.raises(CalDavConflictError):
        await store.async_save(mine, my_snap)


async def test_untouched_events_not_written(
    hass: HomeAssistant, calendar_url: str, session: aiohttp.ClientSession
) -> None:
    store = make_store(hass, calendar_url, session)
    cal, snap = await store.async_load()
    events.upsert_event(cal, vev("theirs", T0, organizer=None))
    await store.async_save(cal, snap)

    cal, snap = await store.async_load()
    etag_before = snap.etags[snap.hrefs["theirs"]]
    events.upsert_event(cal, vev("mine", T0))
    await store.async_save(cal, snap)

    _, snap = await store.async_load()
    assert snap.etags[snap.hrefs["theirs"]] == etag_before


async def test_errors(
    hass: HomeAssistant,
    radicale: str,
    calendar_url: str,
    session: aiohttp.ClientSession,
) -> None:
    with pytest.raises(StoreAuthError):
        await make_store(hass, calendar_url, session, password="wrong").async_load()
    with pytest.raises(CalDavNotCalendarError):
        await make_store(hass, radicale + "missing/", session).async_load()


async def test_discovery(
    hass: HomeAssistant,
    radicale: str,
    calendar_url: str,
    session: aiohttp.ClientSession,
) -> None:
    """Server address in, the user's event calendars out: through
    .well-known (Radicale redirects it to /), the principal and the home."""
    auth = {"Authorization": aiohttp.encode_basic_auth(USER, PASSWORD)}
    # A task list: not offered (no VEVENT).
    async with session.request(
        "MKCALENDAR",
        radicale + "tasks/",
        headers={**auth, "Content-Type": "application/xml"},
        data=(
            b'<?xml version="1.0"?><c:mkcalendar xmlns:d="DAV:" '
            b'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            b"<d:displayname>Tasks</d:displayname>"
            b'<c:supported-calendar-component-set><c:comp name="VTODO"/>'
            b"</c:supported-calendar-component-set></d:prop></d:set>"
            b"</c:mkcalendar>"
        ),
    ) as resp:
        assert resp.status == 201, await resp.text()

    host = radicale.split(f"/{USER}/")[0]
    found = await async_discover(hass, host, USER, PASSWORD, session=session)
    # Radicale names a calendar without a displayname after its path.
    assert found == [CalendarInfo(calendar_url, "bart/test")]

    # A calendar's own URL: just that one.
    found = await async_discover(hass, calendar_url, USER, PASSWORD, session=session)
    assert [c.url for c in found] == [calendar_url]

    with pytest.raises(StoreAuthError):
        await async_discover(hass, host, USER, "wrong", session=session)
