"""CalDAV requests never carry cookies (1.1.1).

Nextcloud answers Basic auth with session cookies, and a request that sends
them back is served as the session's user whatever its Authorization header
says. Through HA's shared session (which keeps cookies) a wrong username
stuck: after one request as bb, bart's app password still got bb's 404.

The server here behaves like that, on localhost, so the real HA session
code is exercised: aiohttp's cookie handling, not a stand in.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_CLOSE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.invite_calendar.const import (
    CONF_CALDAV_PASSWORD,
    CONF_CALDAV_URL,
    CONF_CALDAV_USERNAME,
    CONF_FOLDER,
    CONF_PROCESSED_KEYWORD,
    DOMAIN,
    STORE_CALDAV,
)
from custom_components.invite_calendar.store.caldav import (
    CalDavNotCalendarError,
    CalDavSettings,
    CalDavStore,
    async_get_caldav_session,
    async_validate,
)

from .conftest import FakeMailbox

PATH = "/remote.php/dav/calendars/bart/vakantie-planning/"
USERS = {"bart": "bart-app-pw", "bb": "bb-pw"}
MULTISTATUS = (
    '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
    'xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:response>'
    f"<d:href>{PATH}trip.ics</d:href><d:propstat><d:prop>"
    '<d:getetag>"e1"</d:getetag><cal:calendar-data>BEGIN:VCALENDAR\r\n'
    "VERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:VEVENT\r\nUID:trip\r\n"
    "DTSTAMP:20261006T120000Z\r\nDTSTART:20261020T090000Z\r\n"
    "SUMMARY:Trip\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n</cal:calendar-data>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    "</d:response></d:multistatus>"
)


@dataclass
class FakeNextcloud:
    """Session cookies that override Basic auth, like Nextcloud."""

    sessions: dict[str, str] = field(default_factory=dict)  # session id: user
    log: list[dict[str, str]] = field(default_factory=list)  # request headers
    server: TestServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        # A host name, not an IP: aiohttp's default cookie jar ignores
        # cookies from IP hosts, which would hide the bug.
        return f"http://localhost:{self.server.port}{PATH}"

    def cookies_sent(self) -> list[str]:
        return [h["Cookie"] for h in self.log if "Cookie" in h]

    async def handle(self, request: web.Request) -> web.Response:
        self.log.append(dict(request.headers))
        user = self.sessions.get(request.cookies.get("nc_session_id", ""))
        if user is None:
            user = self._basic_user(request.headers.get("Authorization", ""))
        if user is None:
            return web.Response(status=401)
        session_id = f"s{len(self.sessions) + 1}"
        self.sessions[session_id] = user
        if request.method != "REPORT" or request.path != PATH or user != "bart":
            resp = web.Response(status=404)
        else:
            resp = web.Response(
                status=207, text=MULTISTATUS, content_type="application/xml"
            )
        resp.set_cookie("nc_session_id", session_id, path="/")
        return resp

    @staticmethod
    def _basic_user(header: str) -> str | None:
        if not header.startswith("Basic "):
            return None
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return user if USERS.get(user) == password else None


@pytest.fixture
async def nextcloud(socket_enabled) -> AsyncGenerator[FakeNextcloud]:
    """The server on localhost. HA's connector gets a plain threaded
    resolver: the test harness's DNS resolver can't look up "localhost"."""
    fake = FakeNextcloud()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    fake.server = TestServer(app, host="127.0.0.1")
    await fake.server.start_server()
    resolver = aiohttp.ThreadedResolver()
    resolver.real_close = resolver.close  # what the harness's teardown calls
    try:
        with patch(
            "homeassistant.helpers.aiohttp_client._async_make_resolver",
            return_value=resolver,
        ):
            yield fake
    finally:
        await fake.server.close()


async def test_fake_reproduces_bug_with_cookie_session(
    hass: HomeAssistant, nextcloud: FakeNextcloud
) -> None:
    """Control: through a session with a cookie jar, the second login
    stays bb. This is the 1.1.0 behaviour."""
    async with aiohttp.ClientSession() as session:
        bb = CalDavStore(
            hass, CalDavSettings(nextcloud.url, "bb", USERS["bb"]), session=session
        )
        bart = CalDavStore(
            hass,
            CalDavSettings(nextcloud.url, "bart", USERS["bart"]),
            session=session,
        )
        with pytest.raises(CalDavNotCalendarError, match="404"):
            await bb.async_fetch()
        with pytest.raises(CalDavNotCalendarError, match="404"):
            await bart.async_fetch()
    assert nextcloud.cookies_sent()


async def test_store_sends_no_cookies(
    hass: HomeAssistant, nextcloud: FakeNextcloud
) -> None:
    bb = CalDavStore(hass, CalDavSettings(nextcloud.url, "bb", USERS["bb"]))
    with pytest.raises(CalDavNotCalendarError, match="404"):
        await bb.async_fetch()
    bart = CalDavStore(hass, CalDavSettings(nextcloud.url, "bart", USERS["bart"]))
    resources = await bart.async_fetch()
    assert [r.href.rsplit("/", 1)[1] for r in resources] == ["trip.ics"]
    # The server did hand out a cookie, and it never came back.
    assert nextcloud.sessions
    assert len(nextcloud.log) == 2
    assert nextcloud.cookies_sent() == []
    # Twice as the same user: still no cookie.
    await bart.async_fetch()
    assert nextcloud.cookies_sent() == []


async def test_one_session_per_hass_closed_on_stop(hass: HomeAssistant) -> None:
    session = async_get_caldav_session(hass)
    assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
    assert async_get_caldav_session(hass) is session
    one = CalDavStore(hass, CalDavSettings("https://a.example/c/", "u", "p"))
    two = CalDavStore(hass, CalDavSettings("https://b.example/c/", "v", "q"))
    assert one.session is two.session is session

    hass.bus.async_fire(EVENT_HOMEASSISTANT_CLOSE)
    await hass.async_block_till_done()
    assert session.closed
    # A later caller gets a fresh one rather than a closed session.
    assert async_get_caldav_session(hass) is not session


async def test_validate_twice_with_different_users(
    hass: HomeAssistant, nextcloud: FakeNextcloud
) -> None:
    with pytest.raises(CalDavNotCalendarError):
        await async_validate(hass, CalDavSettings(nextcloud.url, "bb", USERS["bb"]))
    assert (
        await async_validate(hass, CalDavSettings(nextcloud.url, "bart", USERS["bart"]))
        == 1
    )
    assert nextcloud.cookies_sent() == []


async def test_config_flow_corrected_username(
    hass: HomeAssistant, mailbox: FakeMailbox, nextcloud: FakeNextcloud
) -> None:
    """Bb's reproduction: wrong user first, then the right one, same flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "mail.example.com",
            CONF_PORT: 993,
            CONF_USERNAME: "vakantie@example.com",
            CONF_PASSWORD: "pw",
            CONF_FOLDER: "INBOX",
            CONF_PROCESSED_KEYWORD: "InviteCalendarProcessed",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": STORE_CALDAV}
    )
    form = {
        CONF_NAME: "Vakantie",
        CONF_CALDAV_URL: nextcloud.url,
        CONF_CALDAV_USERNAME: "bb",
        CONF_CALDAV_PASSWORD: USERS["bb"],
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "caldav_not_calendar"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**form, CONF_CALDAV_USERNAME: "bart", CONF_CALDAV_PASSWORD: USERS["bart"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CALDAV_USERNAME] == "bart"
    await hass.async_block_till_done()
    # Two validations plus the coordinator's first load: no cookie on any.
    assert len(nextcloud.log) == 3
    assert nextcloud.cookies_sent() == []
    assert await hass.config_entries.async_unload(result["result"].entry_id)
