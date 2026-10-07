"""Events this calendar organizes: create, change, cancel. Pure functions;
the coordinator does the locking, storing and sending.

RFC 6047: the calendar's own address is the ORGANIZER, so From: equals the
SMTP login, mailcow's sender check passes and DMARC aligns. The cost: the
people invited are ATTENDEES and can't edit the event in their own client;
Home Assistant is authoritative.

Every REQUEST carries the whole series (master plus overrides), so a
recipient always ends up with the same picture as this calendar. Updates and
cancellations raise SEQUENCE; every VEVENT sent gets a fresh DTSTAMP.

Times are stored in Home Assistant's own time zone (TZID), never in UTC, so
a weekly 09:00 series stays at 09:00 across a DST change.
"""

from __future__ import annotations

import copy
import datetime
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

import recurring_ical_events
from dateutil.rrule import rrulestr
from homeassistant.util import dt as dt_util
from icalendar import Calendar, Event, vCalAddress, vText
from icalendar.prop import vDDDTypes, vRecur

from .ical import events

DEFAULT_DURATION = datetime.timedelta(hours=1)


class OutboundError(ValueError):
    """A request that can't be carried out; `key` is a translation key."""

    def __init__(self, key: str, **placeholders: str) -> None:
        """Error `key` with translation placeholders."""
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


@dataclass(slots=True)
class EventFields:
    """Requested values. None means "leave as it is"; an empty rrule
    removes the recurrence."""

    summary: str | None = None
    description: str | None = None
    location: str | None = None
    start: datetime.date | datetime.datetime | None = None
    end: datetime.date | datetime.datetime | None = None
    rrule: str | None = None
    attendees: list[str] | None = None


# --------------------------------------------------------------------- values


def local_time(
    value: datetime.date | datetime.datetime,
) -> datetime.date | datetime.datetime:
    """A datetime in HA's time zone (naive means local wall time); a date
    stays a date."""
    if isinstance(value, datetime.datetime):
        return events.aware(value).astimezone(dt_util.get_default_time_zone())
    return value


def parse_rrule(text: str) -> vRecur:
    """A validated RRULE ("FREQ=WEEKLY;COUNT=4", an "RRULE:" prefix is
    allowed)."""
    raw = text.strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[6:]
    try:
        recur = vRecur.from_ical(raw)
        rrulestr(recur.to_ical().decode(), dtstart=datetime.datetime(2026, 1, 1))
    except Exception as err:  # noqa: BLE001 - any parse failure
        raise OutboundError("invalid_rrule", rrule=text) from err
    if "FREQ" not in recur:
        raise OutboundError("invalid_rrule", rrule=text)
    return recur


def clean_addresses(addresses: Iterable[str]) -> list[str]:
    """Lowercased, de-duplicated, order kept; rejects anything without @."""
    out: list[str] = []
    for raw in addresses:
        addr = events.address_of(raw)
        if not addr or "@" not in addr:
            raise OutboundError("invalid_attendee", attendee=str(raw))
        addr = addr.lower()
        if addr not in out:
            out.append(addr)
    return out


def _times(
    start: datetime.date | datetime.datetime,
    end: datetime.date | datetime.datetime | None,
) -> tuple[datetime.date | datetime.datetime, datetime.date | datetime.datetime]:
    start = local_time(start)
    is_dt = isinstance(start, datetime.datetime)
    if end is None:
        end = start + (DEFAULT_DURATION if is_dt else datetime.timedelta(days=1))
    end = local_time(end)
    if isinstance(end, datetime.datetime) != is_dt:
        raise OutboundError("mixed_date_types")
    if end <= start:
        raise OutboundError("end_before_start")
    return start, end


def _replace(event: Event, name: str, value) -> None:
    event.pop(name, None)
    if value is not None and value != "":
        event.add(name, value)


def _set_attendees(event: Event, attendees: list[str]) -> None:
    event.pop("ATTENDEE", None)
    for addr in attendees:
        attendee = vCalAddress(f"mailto:{addr}")
        attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
        attendee.params["PARTSTAT"] = vText("NEEDS-ACTION")
        attendee.params["RSVP"] = vText("TRUE")
        event.add("attendee", attendee, encode=0)


def _stamp(event: Event) -> None:
    events.touch_dtstamp(event)
    event.pop("LAST-MODIFIED", None)
    event.add("last-modified", dt_util.utcnow())


# ----------------------------------------------------------------- lookups


def own_series(cal: Calendar, uid: str, own_address: str) -> list[Event]:
    """Every component of `uid`, master first. Raises when the UID is
    unknown or organized by someone else."""
    comps = [c for c in cal.walk("VEVENT") if str(c.get("UID")) == uid]
    if not comps:
        raise OutboundError("event_not_found", uid=uid)
    comps.sort(key=lambda c: events.recurrence_key(c) is not None)
    organizer = events.get_organizer_email(comps[0]) or ""
    if organizer.lower() != own_address.lower():
        raise OutboundError("event_not_own", uid=uid)
    return comps


def parse_recurrence_id(text: str, master: Event) -> datetime.date | datetime.datetime:
    """The value list_events returns as recurrence_id ("20261020T090000",
    "20261020T070000Z" or "20261020") as a date or aware datetime in the
    master's frame."""
    try:
        value = vDDDTypes.from_ical(text.strip())
    except Exception as err:  # noqa: BLE001 - any parse failure
        raise OutboundError("invalid_recurrence_id", recurrence_id=text) from err
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            tz = getattr(master["DTSTART"].dt, "tzinfo", None)
            value = value.replace(tzinfo=tz or dt_util.get_default_time_zone())
        return value
    if isinstance(value, datetime.date):
        return value
    raise OutboundError("invalid_recurrence_id", recurrence_id=text)


def is_occurrence(
    cal: Calendar, uid: str, rid: datetime.date | datetime.datetime
) -> bool:
    """True when the series `uid` has an occurrence originally at `rid`
    (cancelled ones excluded)."""
    single = events.new_calendar()
    for c in cal.walk("VEVENT"):
        if str(c.get("UID")) == uid:
            single.add_component(c)
    key = int(events.aware(rid).timestamp())
    start = events.aware(rid) - datetime.timedelta(days=1)
    end = events.aware(rid) + datetime.timedelta(days=2)
    for occ in recurring_ical_events.of(single).between(start, end):
        occ_rid = occ.get("RECURRENCE-ID")
        if occ_rid is not None and int(events.aware(occ_rid.dt).timestamp()) == key:
            return True
    return False


# -------------------------------------------------------------- operations


def new_event(fields: EventFields, own_address: str, organizer_cn: str) -> Event:
    """A new VEVENT organized by this calendar (not yet in any calendar)."""
    if not fields.summary or fields.start is None:
        raise OutboundError("summary_and_start_required")
    start, end = _times(fields.start, fields.end)
    domain = own_address.rsplit("@", 1)[-1] or "invite-calendar"
    event = Event()
    event.add("uid", f"{uuid.uuid4()}@{domain}")
    event.add("summary", fields.summary)
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("sequence", 0)
    event.add("status", "CONFIRMED")
    event.add("created", dt_util.utcnow())
    organizer = vCalAddress(f"mailto:{own_address}")
    organizer.params["CN"] = vText(organizer_cn)
    event.add("organizer", organizer, encode=0)
    _replace(event, "description", fields.description)
    _replace(event, "location", fields.location)
    if fields.rrule:
        event.add("rrule", parse_rrule(fields.rrule))
    _set_attendees(event, clean_addresses(fields.attendees or []))
    _stamp(event)
    return event


def _apply(event: Event, fields: EventFields, *, allow_rrule: bool) -> None:
    if fields.summary is not None:
        if not fields.summary.strip():
            raise OutboundError("summary_and_start_required")
        _replace(event, "summary", fields.summary)
    if fields.description is not None:
        _replace(event, "description", fields.description)
    if fields.location is not None:
        _replace(event, "location", fields.location)
    if fields.start is not None or fields.end is not None:
        old_start = event["DTSTART"].dt
        old_end = event["DTEND"].dt if event.get("DTEND") else None
        if fields.start is not None:
            new_start = local_time(fields.start)
            same_kind = isinstance(new_start, datetime.datetime) == isinstance(
                old_start, datetime.datetime
            )
            if fields.end is None and old_end is not None and same_kind:
                # Moving the start keeps the duration.
                new_end = new_start + events.duration(event)
            else:
                new_end = fields.end
        else:
            new_start, new_end = old_start, fields.end
        start, end = _times(new_start, new_end)
        _replace(event, "dtstart", start)
        _replace(event, "dtend", end)
    if fields.rrule is not None:
        if not allow_rrule:
            raise OutboundError("rrule_on_occurrence")
        event.pop("RRULE", None)
        if fields.rrule.strip():
            event.add("rrule", parse_rrule(fields.rrule))
        else:
            # No longer recurring: exceptions to the old rule mean nothing.
            event.pop("EXDATE", None)
            event.pop("RDATE", None)
    if fields.attendees is not None:
        _set_attendees(event, clean_addresses(fields.attendees))


@dataclass(slots=True)
class UpdatePlan:
    """What an update produced."""

    request: list[Event]  # the full series to send as REQUEST
    removed_attendees: list[str]  # get a CANCEL
    cancel: list[Event]  # what to CANCEL for them
    sequence: int


def update_series(
    cal: Calendar, uid: str, fields: EventFields, own_address: str
) -> UpdatePlan:
    """Change the master (the whole event or series) in place."""
    comps = own_series(cal, uid, own_address)
    master = comps[0]
    if events.recurrence_key(master) is not None:
        raise OutboundError("event_not_found", uid=uid)
    before = set(events.get_attendee_emails(master))
    old_start = master["DTSTART"].dt
    _apply(master, fields, allow_rrule=True)
    moved = master["DTSTART"].dt != old_start
    if comps[1:] and (moved or not events.is_recurring(master)):
        # Overrides and EXDATEs point at the old occurrence times, which no
        # longer exist after the series moved or stopped repeating; drop
        # them, as Google and Outlook do.
        for override in comps[1:]:
            cal.subcomponents.remove(override)
        comps = [master]
    if moved:
        master.pop("EXDATE", None)
    seq = events.bump_sequence(master)
    _stamp(master)
    after = set(events.get_attendee_emails(master))
    removed = sorted(before - after)
    if fields.attendees is not None:
        for override in comps[1:]:
            _set_attendees(override, sorted(after))
    for override in comps[1:]:
        _stamp(override)
    cancel = []
    if removed:
        stub = copy.deepcopy(master)
        stub.pop("STATUS", None)
        stub.add("status", "CANCELLED")
        _set_attendees(stub, removed)
        cancel = [stub]
    return UpdatePlan(
        request=comps, removed_attendees=removed, cancel=cancel, sequence=seq
    )


def update_occurrence(
    cal: Calendar, uid: str, recurrence_id: str, fields: EventFields, own_address: str
) -> UpdatePlan:
    """Change one occurrence of a series: create or update its override."""
    comps = own_series(cal, uid, own_address)
    master = comps[0]
    if not events.is_recurring(master):
        raise OutboundError("not_recurring", uid=uid)
    rid = parse_recurrence_id(recurrence_id, master)
    key = int(events.aware(rid).timestamp())
    override = next((c for c in comps[1:] if events.recurrence_key(c) == key), None)
    if override is None:
        if not is_occurrence(cal, uid, rid):
            raise OutboundError("invalid_recurrence_id", recurrence_id=recurrence_id)
        override = copy.deepcopy(master)
        for prop in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
            override.pop(prop, None)
        duration = events.duration(master)
        start = rid
        override.pop("DTSTART", None)
        override.pop("DTEND", None)
        override.add("dtstart", start)
        override.add("dtend", start + duration)
        override.add("recurrence-id", rid)
        cal.add_component(override)
        comps.append(override)
    _apply(override, fields, allow_rrule=False)
    # One SEQUENCE for the series: raise the master's and give the
    # override the same, so every client sees a newer version.
    seq = events.bump_sequence(master)
    override.pop("SEQUENCE", None)
    override.add("sequence", seq)
    for c in comps:
        _stamp(c)
    return UpdatePlan(request=comps, removed_attendees=[], cancel=[], sequence=seq)


def cancel_series(cal: Calendar, uid: str, own_address: str) -> list[Event]:
    """Remove the whole event or series; returns the CANCEL to send."""
    comps = own_series(cal, uid, own_address)
    master = copy.deepcopy(comps[0])
    events.bump_sequence(master)
    master.pop("STATUS", None)
    master.add("status", "CANCELLED")
    _stamp(master)
    events.remove_event(cal, uid)
    return [master]


def cancel_occurrence_own(
    cal: Calendar, uid: str, recurrence_id: str, own_address: str
) -> list[Event]:
    """Cancel one occurrence (EXDATE on the master); returns the CANCEL."""
    comps = own_series(cal, uid, own_address)
    master = comps[0]
    if not events.is_recurring(master):
        raise OutboundError("not_recurring", uid=uid)
    rid = parse_recurrence_id(recurrence_id, master)
    key = int(events.aware(rid).timestamp())
    if not any(
        events.recurrence_key(c) == key for c in comps[1:]
    ) and not is_occurrence(cal, uid, rid):
        raise OutboundError("invalid_recurrence_id", recurrence_id=recurrence_id)
    seq = events.bump_sequence(master)
    _stamp(master)
    rid_event = Event()
    rid_event.add("recurrence-id", rid)
    events.cancel_occurrence(cal, uid, rid_event["RECURRENCE-ID"])

    stub = Event()
    stub.add("uid", uid)
    stub.add("recurrence-id", rid)
    stub.add("dtstart", rid)
    stub.add("sequence", seq)
    stub.add("status", "CANCELLED")
    stub.add("organizer", master["ORGANIZER"])
    if master.get("SUMMARY") is not None:
        stub.add("summary", str(master["SUMMARY"]))
    _set_attendees(stub, events.get_attendee_emails(master))
    events.touch_dtstamp(stub)
    return [stub]


# --------------------------------------------------------------------- text


def describe(event: Event, start_text: str) -> str:
    """Plain text body for an invite."""
    lines = [str(event.get("SUMMARY", "")), "", f"When: {start_text}"]
    if event.get("LOCATION"):
        lines.append(f"Where: {event['LOCATION']}")
    if event.get("RRULE"):
        lines.append(f"Repeats: {event['RRULE'].to_ical().decode()}")
    if event.get("DESCRIPTION"):
        lines += ["", str(event["DESCRIPTION"])]
    lines += ["", "The calendar file is attached."]
    return "\n".join(lines)
