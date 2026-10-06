"""Inbound iMIP: apply one email's REQUEST/CANCEL parts to a calendar.

Ported from pyscript calendar_mailbox._handle_message and
_validate_component, as pure functions (no I/O, no HA state).

ONLY REQUEST AND CANCEL touch the calendar. A METHOD:REPLY carries a minimal
stub with the same UID and used to overwrite the real event.

RECURRING INVITES. One UID over several VEVENTs: a master with RRULE plus
RECURRENCE-ID overrides. Each calendar part's VEVENTs are grouped by UID and
applied as a set:
  REQUEST with the master    the whole series is replaced
  REQUEST, overrides only    each override is added beside the master
  CANCEL with the master     the whole series is removed
  CANCEL with RECURRENCE-ID  that instance only (override removed, EXDATE
                             added to the master)
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from email.message import Message

from icalendar import Calendar, Event

from . import events

_LOGGER = logging.getLogger(__name__)

HANDLED_METHODS = ("REQUEST", "CANCEL")
CALENDAR_CONTENT_TYPES = ("text/calendar", "application/ics")


@dataclass(slots=True)
class ReceivedInvite:
    """One new or changed inbound invitation (per UID)."""

    uid: str
    organizer: str | None
    summary: str
    start: datetime.date | datetime.datetime | None
    location: str | None
    # The email it came in, for a threaded reply.
    message_id: str | None = None
    subject: str | None = None
    references: str | None = None


@dataclass(slots=True)
class MessageResult:
    """What one message did to the calendar."""

    changed: bool = False
    organizers: dict[str, str] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    received: list[ReceivedInvite] = field(default_factory=list)


DATE_PROPS = ("DTSTART", "DTEND", "RECURRENCE-ID")


def validate_component(component: Event, method: str = "REQUEST") -> None:
    """Raise ValueError when a VEVENT can't be stored safely: no UID, a date
    property that doesn't parse, or (REQUEST) no DTSTART at all.

    icalendar 6.3 drops an unparseable property and records it in
    `component.errors`; older versions kept it and failed lazily on `.dt`.
    Both are caught here, so a broken invite goes down the retry / give up
    path instead of into the calendar as an event without a date.
    """
    if component.get("UID") is None:
        raise ValueError("VEVENT without UID")
    for prop, err in getattr(component, "errors", None) or []:
        if str(prop).upper() in DATE_PROPS:
            raise ValueError(
                f"VEVENT {component.get('UID')} has a broken {prop}: {err}"
            )
    if method == "REQUEST" and component.get("DTSTART") is None:
        raise ValueError(f"VEVENT {component.get('UID')} has no DTSTART")
    for prop in DATE_PROPS:
        raw = component.get(prop)
        if raw is None:
            continue
        try:
            raw.dt  # noqa: B018 - forces the lazy parse
        except Exception as err:
            raise ValueError(
                f"VEVENT {component.get('UID')} has a broken {prop}: {err}"
            ) from err


def calendar_parts(msg: Message) -> list[tuple[str, bytes]]:
    """(content type, payload) of every calendar part in a message: a
    text/calendar or application/ics part, or any attachment named *.ics."""
    out = []
    for part in msg.walk():
        ctype = part.get_content_type()
        filename = (part.get_filename() or "").lower()
        if ctype not in CALENDAR_CONTENT_TYPES and not filename.endswith(".ics"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            out.append((ctype, payload))
    return out


def apply_message(msg: Message, cal: Calendar, name: str = "") -> MessageResult:
    """Apply one email's calendar parts to `cal`.

    A calendar part that does not parse at all is skipped with a warning.
    Raises ValueError on a VEVENT that parses but can't be stored safely,
    so the caller can retry or give up on the whole message.
    """
    result = MessageResult()
    seen: set[tuple[str, str]] = set()
    prefix = f"[{name}] " if name else ""

    for ctype, payload in calendar_parts(msg):
        try:
            invite = Calendar.from_ical(payload)
        except Exception as err:  # noqa: BLE001 - any parse error: skip part
            _LOGGER.warning(
                "%scould not parse calendar part (%s): %s", prefix, ctype, err
            )
            continue

        method = str(invite.get("METHOD", "REQUEST")).upper()
        if method not in HANDLED_METHODS:
            _LOGGER.info("%signoring METHOD:%s", prefix, method)
            continue

        groups: dict[str, list[Event]] = {}
        for component in invite.walk("VEVENT"):
            validate_component(component, method)
            groups.setdefault(str(component.get("UID")), []).append(component)

        for uid, components in groups.items():
            # Inline plus attachment often carry the same object twice.
            if (method, uid) in seen:
                continue
            seen.add((method, uid))

            masters = [c for c in components if events.recurrence_key(c) is None]
            master = masters[0] if masters else None

            if method == "CANCEL":
                if master is not None:
                    if events.remove_event(cal, uid):
                        result.changed = True
                        _LOGGER.info("%scancelled event %s", prefix, uid)
                    result.cancelled.append(uid)
                    continue
                for c in components:
                    if events.cancel_occurrence(cal, uid, c["RECURRENCE-ID"]):
                        result.changed = True
                        _LOGGER.info(
                            "%scancelled one occurrence of %s (%s)",
                            prefix,
                            uid,
                            c["RECURRENCE-ID"].dt,
                        )
                continue

            # METHOD:REQUEST
            if not events.replace_series(cal, components):
                _LOGGER.debug("%sstale or unchanged REQUEST for %s", prefix, uid)
                continue
            result.changed = True
            _LOGGER.info("%sadded/updated %s", prefix, uid)

            # Per series, from the master; an invite to a single instance of
            # someone else's series has no master, its override stands in.
            primary = master if master is not None else components[0]
            organizer = events.get_organizer_email(primary)
            if organizer:
                result.organizers[uid] = organizer
            start = primary.get("DTSTART")
            location = primary.get("LOCATION")
            result.received.append(
                ReceivedInvite(
                    uid=uid,
                    organizer=organizer,
                    summary=str(primary.get("SUMMARY", "")),
                    start=start.dt if start is not None else None,
                    location=str(location) if location else None,
                    message_id=msg.get("Message-ID"),
                    subject=msg.get("Subject"),
                    references=msg.get("References"),
                )
            )

    return result
