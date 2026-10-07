"""CalDAV calendar store (Nextcloud, SOGo, Radicale, ...).

Speaks the three requests it needs directly over an async HTTP session
instead of using the `caldav` library: HA core is moving that library from
2.x to 3.x, and owning REPORT/PUT/DELETE is less code than wrapping two
major versions. Parsing runs in the executor; network I/O is async.

SESSION. One session per Home Assistant instance, WITHOUT cookies, shared
by every CalDAV store and the config flow. HA's shared session keeps a
cookie jar, and Nextcloud answers Basic auth with session cookies
(nc_session_id and others). Sent back, those cookies win over the
Authorization header: after one request as user A, a request with user B's
app password was still served as A (HTTP 404 on B's calendar). With no
cookie jar, Basic auth on each request is the only identity the server
sees. The session uses HA's connector (TLS verified) and is closed by HA
on shutdown.

READ. One REPORT calendar-query on the collection returns every VEVENT
resource with its href and ETag. A resource that does not parse is skipped
with a warning: it is then invisible to this integration and never written.

WRITE. Only the per UID diff. Changed UIDs are PUT to the href they were
loaded from (new ones to <collection>/<uid>.ics) with If-Match on the ETag
seen at load, or If-None-Match: * for new resources. Removed UIDs are
DELETEd with If-Match. A 412 means someone changed that event in the
seconds between load and save: the save fails, nothing is flagged, and the
next poll starts again from what is on the server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree as ET

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util.hass_dict import HassKey
from icalendar import Calendar, Event

from ..const import DOMAIN
from ..ical import events
from . import Diff, Snapshot, StoreAuthError, StoreError, diff, fingerprints, group

_LOGGER = logging.getLogger(__name__)

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
TIMEOUT = aiohttp.ClientTimeout(total=30)

REPORT_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    b"<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    b'<c:filter><c:comp-filter name="VCALENDAR">'
    b'<c:comp-filter name="VEVENT"/>'
    b"</c:comp-filter></c:filter>"
    b"</c:calendar-query>"
)

DATA_SESSION: HassKey[aiohttp.ClientSession] = HassKey(f"{DOMAIN}_caldav_session")


@callback
def async_get_caldav_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """The cookieless CalDAV session for this hass, created on first use."""
    session = hass.data.get(DATA_SESSION)
    if session is None or session.closed:
        session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
        hass.data[DATA_SESSION] = session
    return session


class CalDavNotCalendarError(StoreError):
    """The URL is reachable but is not a calendar collection."""


class CalDavConflictError(StoreError):
    """An event changed on the server between load and save (HTTP 412)."""


@dataclass(frozen=True, slots=True)
class CalDavSettings:
    """Where the calendar lives."""

    url: str
    username: str
    password: str


def _basic_auth(username: str, password: str) -> str:
    """Authorization header value (UTF-8, so non ASCII passwords work)."""
    encode = getattr(aiohttp, "encode_basic_auth", None)
    if encode is not None:
        return encode(username, password)
    return aiohttp.BasicAuth(username, password, encoding="utf-8").encode()


def normalize_url(url: str) -> str:
    """Collection URL with exactly one trailing slash."""
    return url.strip().rstrip("/") + "/"


@dataclass(slots=True)
class Resource:
    """One CalDAV resource as returned by the REPORT."""

    href: str
    etag: str | None
    data: str


def parse_multistatus(base_url: str, body: str) -> list[Resource]:
    """Resources with calendar data from a 207 multistatus body."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as err:
        raise CalDavNotCalendarError(f"not a multistatus response: {err}") from err
    if root.tag != f"{{{DAV}}}multistatus":
        raise CalDavNotCalendarError(f"unexpected response element {root.tag}")
    out = []
    for response in root.findall(f"{{{DAV}}}response"):
        href = response.findtext(f"{{{DAV}}}href")
        if not href:
            continue
        for propstat in response.findall(f"{{{DAV}}}propstat"):
            status = propstat.findtext(f"{{{DAV}}}status") or ""
            if " 200 " not in f"{status} ":
                continue
            prop = propstat.find(f"{{{DAV}}}prop")
            if prop is None:
                continue
            data = prop.findtext(f"{{{CALDAV}}}calendar-data")
            if not data:
                continue
            etag = prop.findtext(f"{{{DAV}}}getetag")
            out.append(Resource(urljoin(base_url, href.strip()), etag, data))
    return out


def build_calendar(resources: list[Resource], prodid: str) -> tuple[Calendar, Snapshot]:
    """One in memory Calendar from all resources, plus the snapshot. BLOCKING
    (CPU): run in the executor."""
    cal = events.new_calendar(prodid=prodid)
    hrefs: dict[str, str] = {}
    etags: dict[str, str] = {}
    tzids: set[str] = set()
    for res in resources:
        try:
            parsed = Calendar.from_ical(res.data)
        except Exception as err:  # noqa: BLE001 - any parse failure: skip it
            _LOGGER.warning("Skipping unparseable CalDAV object %s: %s", res.href, err)
            continue
        for comp in parsed.subcomponents:
            if comp.name == "VTIMEZONE":
                tzid = str(comp.get("TZID"))
                if tzid not in tzids:
                    tzids.add(tzid)
                    cal.add_component(comp)
            elif comp.name == "VEVENT" and comp.get("UID") is not None:
                cal.add_component(comp)
                hrefs[str(comp.get("UID"))] = res.href
        if res.etag:
            etags[res.href] = res.etag
    return cal, Snapshot(fingerprints=fingerprints(cal), hrefs=hrefs, etags=etags)


def resource_body(cal: Calendar, components: list[Event], prodid: str) -> bytes:
    """One CalDAV resource for one UID: its components plus the VTIMEZONEs
    they reference."""
    out = events.new_calendar(prodid=prodid)
    event_bytes = b"".join(c.to_ical() for c in components)
    for comp in cal.subcomponents:
        if comp.name == "VTIMEZONE":
            tzid = str(comp.get("TZID"))
            if f"TZID={tzid}".encode() in event_bytes:
                out.add_component(comp)
    for comp in components:
        out.add_component(comp)
    try:
        out.add_missing_timezones()
    except Exception:  # noqa: BLE001 - a missing VTIMEZONE is not fatal
        _LOGGER.debug("Could not add missing timezones", exc_info=True)
    return out.to_ical()


@dataclass(slots=True)
class SavePlan:
    """What a save will send."""

    changes: Diff
    puts: list[tuple[str, bytes, str | None]]  # href, body, etag (None: new)
    deletes: list[tuple[str, str | None]]  # href, etag


def plan_save(url: str, cal: Calendar, snapshot: Snapshot, prodid: str) -> SavePlan:
    """Work out the requests for a save. BLOCKING (CPU)."""
    changes = diff(cal, snapshot)
    groups = group(cal)
    puts = []
    for uid in changes.changed:
        href = snapshot.hrefs.get(uid)
        if href is None:
            href = url + quote(uid, safe="") + ".ics"
            etag = None
        else:
            etag = snapshot.etags.get(href)
        puts.append((href, resource_body(cal, groups[uid], prodid), etag))
    deletes = [
        (snapshot.hrefs[uid], snapshot.etags.get(snapshot.hrefs[uid]))
        for uid in changes.removed
        if uid in snapshot.hrefs
    ]
    return SavePlan(changes, puts, deletes)


class CalDavStore:
    """A calendar collection on a CalDAV server, shared with people."""

    shared = True

    def __init__(
        self,
        hass: HomeAssistant,
        settings: CalDavSettings,
        prodid: str = events.PRODID,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Store for the collection at settings.url."""
        self.hass = hass
        self.url = normalize_url(settings.url)
        self._authorization = _basic_auth(settings.username, settings.password)
        self.prodid = prodid
        self._session = session

    def describe(self) -> str:
        """The collection URL."""
        return self.url

    @property
    def session(self) -> aiohttp.ClientSession:
        """The cookieless CalDAV session (verifies TLS), see module doc."""
        if self._session is None:
            self._session = async_get_caldav_session(self.hass)
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        all_headers = {"Authorization": self._authorization, **(headers or {})}
        try:
            async with self.session.request(
                method, url, data=data, headers=all_headers, timeout=TIMEOUT
            ) as resp:
                text = await resp.text(errors="replace")
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise StoreError(f"{method} {url}: {err!r}") from err
        if status == 401:
            raise StoreAuthError(f"{method} {url}: HTTP 401 Unauthorized")
        return status, text

    async def async_fetch(self) -> list[Resource]:
        """Every VEVENT resource in the collection. Raises StoreError, so the
        caller skips the poll rather than acting on an empty calendar."""
        status, text = await self._request(
            "REPORT",
            self.url,
            data=REPORT_BODY,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        if status in (403, 404, 405, 501):
            raise CalDavNotCalendarError(f"REPORT {self.url}: HTTP {status}")
        if status != 207:
            raise StoreError(f"REPORT {self.url}: HTTP {status}")
        return parse_multistatus(self.url, text)

    async def async_load(self) -> tuple[Calendar, Snapshot]:
        """Read the whole collection."""
        resources = await self.async_fetch()
        return await self.hass.async_add_executor_job(
            build_calendar, resources, self.prodid
        )

    async def async_save(self, cal: Calendar, snapshot: Snapshot) -> Diff:
        """PUT changed UIDs, DELETE removed ones. Every request is tried;
        any failure raises StoreError afterwards."""
        plan = await self.hass.async_add_executor_job(
            plan_save, self.url, cal, snapshot, self.prodid
        )
        errors: list[str] = []
        conflict = False

        for href, body, etag in plan.puts:
            headers = {"Content-Type": "text/calendar; charset=utf-8"}
            if etag:
                headers["If-Match"] = etag
            else:
                headers["If-None-Match"] = "*"
            try:
                status, text = await self._request(
                    "PUT", href, data=body, headers=headers
                )
            except StoreAuthError:
                raise
            except StoreError as err:
                errors.append(str(err))
                continue
            if status == 412:
                conflict = True
                errors.append(f"PUT {href}: changed on the server meanwhile")
            elif status not in (200, 201, 204):
                errors.append(f"PUT {href}: HTTP {status} {text[:200]}")

        for href, etag in plan.deletes:
            headers = {"If-Match": etag} if etag else {}
            try:
                status, text = await self._request("DELETE", href, headers=headers)
            except StoreAuthError:
                raise
            except StoreError as err:
                errors.append(str(err))
                continue
            if status == 412:
                conflict = True
                errors.append(f"DELETE {href}: changed on the server meanwhile")
            elif status not in (200, 204, 404):  # 404: already gone
                errors.append(f"DELETE {href}: HTTP {status} {text[:200]}")

        if errors:
            cls = CalDavConflictError if conflict else StoreError
            raise cls("; ".join(errors))
        return plan.changes


async def async_validate(hass: HomeAssistant, settings: CalDavSettings) -> int:
    """Check credentials and that the URL is a calendar. Returns the number
    of events found. Raises StoreAuthError, CalDavNotCalendarError or
    StoreError."""
    store = CalDavStore(hass, settings)
    return len(await store.async_fetch())


# ------------------------------------------------------------------ discovery
#
# RFC 6764 / 4791 / 5397: from a server address to its calendars.
#
#   1. PROPFIND the address itself: a calendar collection is the answer
#      already (someone pasted the full URL).
#   2. Its current-user-principal; if it has none, /.well-known/caldav first
#      (Nextcloud redirects that to /remote.php/dav/).
#   3. The principal's calendar-home-set.
#   4. PROPFIND Depth 1 on the home: every child whose resourcetype is a
#      calendar, that holds events (VEVENT) and that this login may write to.
#
# Redirects are followed by hand, at most MAX_REDIRECTS, and only on the same
# host, so the Authorization header never goes anywhere else.

MAX_REDIRECTS = 5

_PROPFIND_START = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:current-user-principal/><d:resourcetype/><d:displayname/>"
    b"</d:prop></d:propfind>"
)
_PROPFIND_HOME = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    b"<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
)
_PROPFIND_CALENDARS = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    b"<d:prop><d:resourcetype/><d:displayname/>"
    b"<c:supported-calendar-component-set/><d:current-user-privilege-set/>"
    b"</d:prop></d:propfind>"
)
_WRITE_PRIVILEGES = {"write", "write-content", "all"}


class CalDavDiscoveryError(StoreError):
    """The server answered, but no calendar could be found through it."""


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    """One calendar the login can use."""

    url: str
    name: str


def server_url(text: str) -> str:
    """What someone typed as the server: https:// is assumed when no scheme
    is given."""
    text = text.strip()
    if "://" not in text:
        text = "https://" + text
    return text


def _ok_props(response: ET.Element) -> list[ET.Element]:
    out = []
    for propstat in response.findall(f"{{{DAV}}}propstat"):
        status = propstat.findtext(f"{{{DAV}}}status") or ""
        prop = propstat.find(f"{{{DAV}}}prop")
        if " 200 " in f"{status} " and prop is not None:
            out.append(prop)
    return out


def _responses(base_url: str, body: str) -> list[tuple[str, list[ET.Element]]]:
    """(absolute href, [200 prop elements]) per response in a multistatus."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as err:
        raise CalDavDiscoveryError(f"not a multistatus response: {err}") from err
    if root.tag != f"{{{DAV}}}multistatus":
        raise CalDavDiscoveryError(f"unexpected response element {root.tag}")
    out = []
    for response in root.findall(f"{{{DAV}}}response"):
        href = response.findtext(f"{{{DAV}}}href")
        if href:
            out.append((urljoin(base_url, href.strip()), _ok_props(response)))
    return out


def _find(props: list[ET.Element], tag: str) -> ET.Element | None:
    for prop in props:
        found = prop.find(tag)
        if found is not None:
            return found
    return None


def _href_in(props: list[ET.Element], tag: str, base_url: str) -> str | None:
    element = _find(props, tag)
    href = element.findtext(f"{{{DAV}}}href") if element is not None else None
    return urljoin(base_url, href.strip()) if href else None


def _is_calendar(props: list[ET.Element]) -> bool:
    rtype = _find(props, f"{{{DAV}}}resourcetype")
    return rtype is not None and rtype.find(f"{{{CALDAV}}}calendar") is not None


def _holds_events(props: list[ET.Element]) -> bool:
    comps = _find(props, f"{{{CALDAV}}}supported-calendar-component-set")
    if comps is None:
        return True  # not reported: any component
    names = {c.get("name", "").upper() for c in comps.findall(f"{{{CALDAV}}}comp")}
    return "VEVENT" in names


def _writable(props: list[ET.Element]) -> bool:
    privs = _find(props, f"{{{DAV}}}current-user-privilege-set")
    if privs is None:
        return True  # not reported: assume so, the first PUT will tell
    names = {
        child.tag.split("}", 1)[-1]
        for priv in privs.findall(f"{{{DAV}}}privilege")
        for child in priv
    }
    return bool(names & _WRITE_PRIVILEGES)


def _name(props: list[ET.Element], url: str) -> str:
    element = _find(props, f"{{{DAV}}}displayname")
    name = (element.text or "").strip() if element is not None else ""
    return name or url.rstrip("/").rsplit("/", 1)[-1]


class _Discovery:
    """One discovery run with one login."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str):
        self.session = session
        self.authorization = _basic_auth(username, password)

    async def propfind(self, url: str, depth: str, body: bytes) -> tuple[str, str]:
        """(final URL, multistatus body). Follows same host redirects."""
        origin = urlsplit(url)[:2]
        for _ in range(MAX_REDIRECTS + 1):
            headers = {
                "Authorization": self.authorization,
                "Depth": depth,
                "Content-Type": "application/xml; charset=utf-8",
            }
            try:
                async with self.session.request(
                    "PROPFIND",
                    url,
                    data=body,
                    headers=headers,
                    timeout=TIMEOUT,
                    allow_redirects=False,
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location")
                    text = await resp.text(errors="replace")
            except (aiohttp.ClientError, TimeoutError) as err:
                raise StoreError(f"PROPFIND {url}: {err!r}") from err
            if status == 401:
                raise StoreAuthError(f"PROPFIND {url}: HTTP 401 Unauthorized")
            if status in (301, 302, 303, 307, 308) and location:
                target = urljoin(url, location)
                if urlsplit(target)[:2] != origin:
                    raise CalDavDiscoveryError(
                        f"PROPFIND {url}: redirected to another server {target}"
                    )
                url = target
                continue
            if status != 207:
                raise CalDavDiscoveryError(f"PROPFIND {url}: HTTP {status}")
            return url, text
        raise CalDavDiscoveryError(f"PROPFIND {url}: too many redirects")

    async def principal(self, start: str) -> tuple[list[CalendarInfo], str | None]:
        """([that calendar] when `start` is one, else []), and the principal
        URL found through `start`."""
        url, text = await self.propfind(start, "0", _PROPFIND_START)
        responses = _responses(url, text)
        if not responses:
            return [], None
        href, props = responses[0]
        if _is_calendar(props):
            return [CalendarInfo(normalize_url(href), _name(props, href))], None
        return [], _href_in(props, f"{{{DAV}}}current-user-principal", url)

    async def calendars(self, principal: str) -> list[CalendarInfo]:
        url, text = await self.propfind(principal, "0", _PROPFIND_HOME)
        responses = _responses(url, text)
        home = (
            _href_in(responses[0][1], f"{{{CALDAV}}}calendar-home-set", url)
            if responses
            else None
        )
        if home is None:
            raise CalDavDiscoveryError(f"{principal} has no calendar-home-set")
        url, text = await self.propfind(home, "1", _PROPFIND_CALENDARS)
        out = [
            CalendarInfo(normalize_url(href), _name(props, href))
            for href, props in _responses(url, text)
            if _is_calendar(props) and _holds_events(props) and _writable(props)
        ]
        return sorted(out, key=lambda c: (c.name.lower(), c.url))


async def async_discover(
    hass: HomeAssistant,
    server: str,
    username: str,
    password: str,
    session: aiohttp.ClientSession | None = None,
) -> list[CalendarInfo]:
    """The event calendars `username` can write to on `server` (an address
    like cloud.example.com, any URL on it, or a calendar's own URL).

    Raises StoreAuthError (login rejected), StoreError (unreachable) or
    CalDavDiscoveryError (no CalDAV, or no usable calendar found)."""
    disc = _Discovery(
        session or async_get_caldav_session(hass), username.strip(), password
    )
    start = server_url(server)
    principal = None
    try:
        found, principal = await disc.principal(start)
        if found:
            return found
    except CalDavDiscoveryError:
        _LOGGER.debug("No CalDAV answer at %s, trying .well-known", start)
    if principal is None:
        found, principal = await disc.principal(urljoin(start, "/.well-known/caldav"))
        if found:
            return found
    if principal is None:
        raise CalDavDiscoveryError(f"{start}: no current-user-principal")
    calendars = await disc.calendars(principal)
    if not calendars:
        raise CalDavDiscoveryError(f"{principal}: no writable event calendars")
    return calendars
