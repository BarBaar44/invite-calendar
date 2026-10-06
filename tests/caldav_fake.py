"""A small in memory CalDAV server behind a fake aiohttp session."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import aiohttp


class FakeResponse:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self._text = text

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

    # aiohttp.ClientSession.request stand in
    def request(self, method, url, *, data=None, headers=None, auth=None, timeout=None):
        headers = dict(headers or {})
        self.log.append((method, url, headers))
        expected = aiohttp.encode_basic_auth("bart", self.password)
        if headers.get("Authorization") != expected:
            return FakeResponse(401)
        if method == "REPORT":
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
