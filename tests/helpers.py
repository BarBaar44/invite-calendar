"""Builders for VEVENTs and iMIP mails used across the tests."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from icalendar import Event, vCalAddress

from custom_components.invite_calendar.ical import events

TZ = ZoneInfo("Europe/Amsterdam")
ORGANIZER = "boss@ext.com"


def vev(
    uid: str,
    start: datetime.datetime | datetime.date,
    *,
    end: datetime.datetime | datetime.date | None = None,
    summary: str | None = None,
    location: str | None = "Utrecht",
    rrule: dict | None = None,
    rid: datetime.datetime | None = None,
    seq: int = 0,
    organizer: str | None = ORGANIZER,
    status: str | None = None,
) -> Event:
    """One VEVENT."""
    e = Event()
    e.add("uid", uid)
    e.add("summary", summary or f"S {uid}")
    e.add("dtstart", start)
    if end is None:
        if isinstance(start, datetime.datetime):
            end = start + datetime.timedelta(hours=1)
        else:
            end = start + datetime.timedelta(days=1)
    e.add("dtend", end)
    e.add("sequence", seq)
    events.touch_dtstamp(e)
    if location:
        e.add("location", location)
    if rrule:
        e.add("rrule", rrule)
    if rid is not None:
        e.add("recurrence-id", rid)
    if organizer:
        e.add("organizer", vCalAddress(f"MAILTO:{organizer}"))
    if status:
        e.add("status", status)
    return e


def mail(method: str, comps: list[Event], msg_id: str, *, twice: bool = False) -> bytes:
    """A raw RFC 822 message with one text/calendar body (twice: also as an
    .ics attachment, like many clients send)."""
    cal = events.new_calendar(method=method)
    for c in comps:
        cal.add_component(c)
    body = cal.to_ical()
    if not twice:
        return (
            f"Message-ID: <{msg_id}>\r\nSubject: inv\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: text/calendar; method={method}\r\n\r\n"
        ).encode() + body
    boundary = "BOUNDARY"
    return (
        (
            f"Message-ID: <{msg_id}>\r\nSubject: inv\r\nMIME-Version: 1.0\r\n"
            f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'
            f"--{boundary}\r\nContent-Type: text/calendar; method={method}\r\n\r\n"
        ).encode()
        + body
        + (
            f"\r\n--{boundary}\r\nContent-Type: application/ics; name=invite.ics\r\n"
            'Content-Disposition: attachment; filename="invite.ics"\r\n\r\n'
        ).encode()
        + body
        + f"\r\n--{boundary}--\r\n".encode()
    )


BROKEN_MAIL = (
    b"Message-ID: <broken>\r\nContent-Type: text/calendar\r\n\r\n"
    b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:broken\r\nDTSTART:garbage\r\n"
    b"END:VEVENT\r\nEND:VCALENDAR\r\n"
)
