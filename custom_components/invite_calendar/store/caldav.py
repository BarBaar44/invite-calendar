"""CalDAV calendar store (Nextcloud, SOGo, Radicale, ...).

Speaks the three requests it needs directly over Home Assistant's async HTTP
session instead of using the `caldav` library: HA core is moving that
library from 2.x to 3.x, and owning REPORT/PUT/DELETE is less code than
wrapping two major versions. Parsing runs in the executor; network I/O is
async.

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
from urllib.parse import quote, urljoin
from xml.etree import ElementTree as ET

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from icalendar import Calendar, Event

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
        """HA's shared client session (verifies TLS)."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
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
