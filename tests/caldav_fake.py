"""A small in memory CalDAV server behind a fake aiohttp session."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import aiohttp


class FakeResponse:
    def __init__(
        self, status: int, text: str = "", headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self, errors: str = "strict") -> str:
        return self._text

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


@dataclass
class FakeCalDav:
    """{href: (etag, body)} plus a request log and failure switches."""

    base: str = "https://cloud.example.com/remote.php/dav/calendars/bart/test/"
    password: str = "app-pw"
    resources: dict[str, tuple[str, str]] = field(default_factory=dict)
    log: list[tuple[str, str, dict]] = field(default_factory=list)
    report_status: int = 207
    base_writable: bool = True
    # Discovery, laid out as Nextcloud does it.
    host: str = "https://cloud.example.com"
    principal: str = "/remote.php/dav/principals/users/bart/"
    home: str = "/remote.php/dav/calendars/bart/"
    # Other collections in the calendar home: (name, displayname, components,
    # writable). The test calendar at `base` is always listed first.
    others: list[tuple[str, str, str, bool]] = field(
        default_factory=lambda: [
            ("contact_birthdays", "Contact birthdays", "VEVENT", False),
            ("tasks", "Tasks", "VTODO", True),
            ("family", "Family", "VEVENT", True),
        ]
    )
    fail_put: int | None = None
    _etag: itertools.count = field(default_factory=lambda: itertools.count(1))

    def put_raw(self, name: str, body: str) -> str:
        """A resource created by a person (in Nextcloud), at an odd name."""
        href = self.base + name
        self.resources[href] = (f'"e{next(self._etag)}"', body)
        return href

    def touch(self, href: str) -> None:
        """Someone edits the resource on the server: new ETag."""
        _, body = self.resources[href]
        self.resources[href] = (f'"e{next(self._etag)}"', body)

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u, _ in self.log if m in ("PUT", "DELETE")]

    def _multistatus(self) -> str:
        path = urlsplit(self.base).path
        parts = []
        for href, (etag, body) in self.resources.items():
            rel = urlsplit(href).path
            parts.append(
                f"<d:response><d:href>{escape(rel)}</d:href><d:propstat><d:prop>"
                f"<d:getetag>{escape(etag)}</d:getetag>"
                f"<cal:calendar-data>{escape(body)}</cal:calendar-data>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                "</d:response>"
            )
        # A collection entry without calendar data, as real servers send.
        parts.append(
            f"<d:response><d:href>{escape(path)}</d:href><d:propstat><d:prop/>"
            "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
        )
        return (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
            + "".join(parts)
            + "</d:multistatus>"
        )

    def _propfind(self, url: str, depth: str) -> FakeResponse:
        if not url.startswith(self.host + "/"):
            return FakeResponse(404)  # another server, without CalDAV
        path = urlsplit(url).path
        ns = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        )

        def response(href: str, props: str) -> str:
            return (
                f"<d:response><d:href>{escape(href)}</d:href><d:propstat>"
                f"<d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status>"
                "</d:propstat></d:response>"
            )

        def calendar(href: str, name: str, comps: str, writable: bool) -> str:
            priv = "<d:privilege><d:read/></d:privilege>"
            if writable:
                priv += "<d:privilege><d:write/></d:privilege>"
            return response(
                href,
                "<d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>"
                f"<d:displayname>{escape(name)}</d:displayname>"
                "<cal:supported-calendar-component-set>"
                f'<cal:comp name="{comps}"/></cal:supported-calendar-component-set>'
                f"<d:current-user-privilege-set>{priv}</d:current-user-privilege-set>",
            )

        if path == "/.well-known/caldav":
            return FakeResponse(301, headers={"Location": "/remote.php/dav/"})
        if path == "/remote.php/dav/":
            return FakeResponse(
                207,
                ns
                + response(
                    path,
                    "<d:current-user-principal><d:href>"
                    f"{self.principal}</d:href></d:current-user-principal>"
                    "<d:resourcetype><d:collection/></d:resourcetype>",
                )
                + "</d:multistatus>",
            )
        if path == self.principal:
            return FakeResponse(
                207,
                ns
                + response(
                    path,
                    f"<cal:calendar-home-set><d:href>{self.home}</d:href>"
                    "</cal:calendar-home-set>",
                )
                + "</d:multistatus>",
            )
        base_path = urlsplit(self.base).path
        if path == base_path:
            return FakeResponse(
                207,
                ns
                + calendar(path, "Test", "VEVENT", self.base_writable)
                + "</d:multistatus>",
            )
        if path == self.home and depth == "1":
            parts = [
                response(path, "<d:resourcetype><d:collection/></d:resourcetype>"),
                calendar(base_path, "Test", "VEVENT", self.base_writable),
                response(
                    path + "inbox/",
                    "<d:resourcetype><d:collection/><cal:schedule-inbox/>"
                    "</d:resourcetype>",
                ),
            ]
            for name, display, comps, writable in self.others:
                parts.append(calendar(path + name + "/", display, comps, writable))
            return FakeResponse(207, ns + "".join(parts) + "</d:multistatus>")
        return FakeResponse(405)

    # aiohttp.ClientSession.request stand in
    def request(
        self,
        method,
        url,
        *,
        data=None,
        headers=None,
        auth=None,
        timeout=None,
        allow_redirects=True,
    ):
        headers = dict(headers or {})
        self.log.append((method, url, headers))
        expected = aiohttp.encode_basic_auth("bart", self.password)
        if headers.get("Authorization") != expected:
            return FakeResponse(401)
        if method == "PROPFIND":
            return self._propfind(url, headers.get("Depth", "0"))
        if method == "REPORT":
            others = {f"{self.host}{self.home}{name}/" for name, *_ in self.others}
            if url in others:
                return FakeResponse(
                    207,
                    '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>',
                )
            if url != self.base:
                return FakeResponse(404)
            if self.report_status != 207:
                return FakeResponse(self.report_status)
            return FakeResponse(207, self._multistatus())
        if method == "PUT":
            if self.fail_put:
                return FakeResponse(self.fail_put, "nope")
            current = self.resources.get(url)
            if headers.get("If-None-Match") == "*" and current is not None:
                return FakeResponse(412)
            if "If-Match" in headers and (
                current is None or current[0] != headers["If-Match"]
            ):
                return FakeResponse(412)
            body = data.decode() if isinstance(data, bytes) else data
            self.resources[url] = (f'"e{next(self._etag)}"', body)
            return FakeResponse(201 if current is None else 204)
        if method == "DELETE":
            current = self.resources.get(url)
            if current is None:
                return FakeResponse(404)
            if "If-Match" in headers and current[0] != headers["If-Match"]:
                return FakeResponse(412)
            del self.resources[url]
            return FakeResponse(204)
        return FakeResponse(405)
