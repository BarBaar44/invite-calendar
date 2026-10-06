"""VEVENT helpers on an icalendar Calendar (ported from pyscript ics_store).

An icalendar Calendar is the in memory model of one calendar. Everything
here is synchronous and pure (no I/O), so it runs fine inside an executor
job and in tests.

RECURRING EVENTS. A recurring invite is one UID spread over several
VEVENTs: a MASTER carrying RRULE (and possibly EXDATE/RDATE), plus zero or
more OVERRIDES carrying RECURRENCE-ID. Everything keys on
(UID, RECURRENCE-ID) or works on the whole series:

* replace_series()    a REQUEST carrying the master replaces every
                      component of that UID.
* upsert_event()      matches on (UID, RECURRENCE-ID), so an override for
                      one instance sits next to the master.
* cancel_occurrence() a CANCEL with RECURRENCE-ID drops that instance
                      only (removes the override, adds EXDATE to master).
* prune_past_events() a master is judged by the end of its LAST
                      occurrence; an unbounded series is never pruned.

Recurrence for retention is computed with dateutil in the DTSTART's own
wall clock time, so a weekly 09:00 meeting stays at 09:00 across DST.
Display expansion (the calendar entity) uses recurring-ical-events.
"""

from __future__ import annotations

import datetime
from typing import Any

from dateutil.rrule import rrulestr
from homeassistant.util import dt as dt_util
from icalendar import Calendar, Event
from icalendar.prop import vRecur

PRODID = "-//Home Assistant//Invite Calendar//EN"


def new_calendar(method: str | None = None, prodid: str = PRODID) -> Calendar:
    """An empty VCALENDAR."""
    cal = Calendar()
    cal.add("prodid", prodid)
    cal.add("version", "2.0")
    if method:
        cal.add("method", method)
    return cal


# --------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------


def aware(value: datetime.date | datetime.datetime) -> datetime.datetime:
    """Tz aware datetime for a DTSTART/DTEND/RECURRENCE-ID value.

    A date only value becomes local midnight; a naive datetime is taken as
    local wall time (never as UTC: dt_util.as_local assumes UTC for naive).
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.get_default_time_zone())
        return value
    return dt_util.start_of_local_day(value)


def recurrence_key(component: Event) -> int | None:
    """RECURRENCE-ID as a whole second Unix timestamp, or None for a master
    or a plain single event."""
    raw = component.get("RECURRENCE-ID")
    if raw is None:
        return None
    return int(aware(raw.dt).timestamp())


def is_recurring(component: Event) -> bool:
    """True when the component carries an RRULE."""
    return component.get("RRULE") is not None


def duration(component: Event) -> datetime.timedelta:
    """DTEND minus DTSTART, else DURATION, else zero (all day: one day)."""
    start_raw = component.get("DTSTART")
    end_raw = component.get("DTEND")
    if start_raw is not None and end_raw is not None:
        try:
            return aware(end_raw.dt) - aware(start_raw.dt)
        except TypeError, ValueError:
            pass
    dur_raw = component.get("DURATION")
    if dur_raw is not None:
        try:
            return dur_raw.dt
        except TypeError, ValueError:
            pass
    if start_raw is not None and not isinstance(start_raw.dt, datetime.datetime):
        return datetime.timedelta(days=1)
    return datetime.timedelta(0)


def _first_rrule(component: Event) -> Any:
    raw = component.get("RRULE")
    if isinstance(raw, list):
        return raw[0] if raw else None
    return raw


def _rule(component: Event) -> tuple[Any, datetime.tzinfo] | None:
    """(dateutil rule over NAIVE wall clock times, tzinfo to attach) for a
    master VEVENT, or None if it has no usable RRULE. A UTC UNTIL is
    converted into the same wall clock frame first."""
    recur = _first_rrule(component)
    start_raw = component.get("DTSTART")
    if recur is None or start_raw is None:
        return None
    start = aware(start_raw.dt)
    tz = start.tzinfo
    parts = vRecur(dict(recur))
    until = parts.get("UNTIL")
    if until:
        u = until[0] if isinstance(until, list) else until
        if isinstance(u, datetime.datetime):
            if u.tzinfo is not None:
                u = u.astimezone(tz)
            u = u.replace(tzinfo=None)
        else:
            u = datetime.datetime.combine(u, datetime.time(23, 59, 59))
        parts["UNTIL"] = [u]
    text = parts.to_ical().decode()
    return rrulestr(text, dtstart=start.replace(tzinfo=None)), tz


def _rdates(component: Event) -> list[datetime.datetime]:
    """Every RDATE as an aware datetime (rare, but legal alongside RRULE)."""
    raw = component.get("RDATE")
    if raw is None:
        return []
    groups = raw if isinstance(raw, list) else [raw]
    out = []
    for group in groups:
        for item in getattr(group, "dts", []):
            value = item.dt
            if isinstance(value, tuple):  # PERIOD form
                value = value[0]
            out.append(aware(value))
    return out


def series_end(component: Event) -> datetime.datetime | None:
    """Tz aware end of the LAST occurrence of a master, or None when the
    series never ends or the end can't be worked out ("keep it")."""
    try:
        built = _rule(component)
    except Exception:  # noqa: BLE001 - "can't work it out" means keep it
        return None
    if built is None:
        return None
    rule, tz = built
    recur = _first_rrule(component)
    if not recur.get("UNTIL") and not recur.get("COUNT"):
        return None  # unbounded
    try:
        last_naive = rule[-1]
    except IndexError:
        last_naive = None
    except Exception:  # noqa: BLE001
        return None
    candidates = [aware(component.get("DTSTART").dt)]
    if last_naive is not None:
        candidates.append(last_naive.replace(tzinfo=tz))
    candidates.extend(_rdates(component))
    return max(candidates) + duration(component)


# --------------------------------------------------------------------
# VEVENT CRUD
# --------------------------------------------------------------------


def _same(c: Any, uid: str, rid: int | None) -> bool:
    return c.name == "VEVENT" and str(c.get("UID")) == uid and recurrence_key(c) == rid


def remove_event(cal: Calendar, uid: str) -> bool:
    """Remove EVERY component with this UID: a plain event, or a whole
    recurring series (master plus all overrides)."""
    before = len(cal.subcomponents)
    cal.subcomponents = [
        c
        for c in cal.subcomponents
        if not (c.name == "VEVENT" and str(c.get("UID")) == uid)
    ]
    return len(cal.subcomponents) != before


def upsert_event(cal: Calendar, new_event: Event) -> bool:
    """Add or replace ONE component, matched on (UID, RECURRENCE-ID).
    A lower SEQUENCE than the stored copy is stale and ignored."""
    uid = str(new_event.get("UID"))
    rid = recurrence_key(new_event)
    new_seq = int(new_event.get("SEQUENCE", 0))

    for i, c in enumerate(cal.subcomponents):
        if _same(c, uid, rid):
            if new_seq < int(c.get("SEQUENCE", 0)):
                return False
            cal.subcomponents[i] = new_event
            return True

    cal.add_component(new_event)
    return True


def replace_series(cal: Calendar, components: list[Event]) -> bool:
    """Apply one REQUEST's components for ONE UID.

    With the master in the set, it is the whole series as the organizer now
    sees it (RFC 5546), so every stored component of that UID is replaced.
    Without a master each override is upserted on its own. A set whose
    master is older than the stored master changes nothing.
    Returns True if the calendar changed.
    """
    if not components:
        return False
    uid = str(components[0].get("UID"))
    masters = [c for c in components if recurrence_key(c) is None]

    if not masters:
        changed = False
        for c in components:
            if upsert_event(cal, c):
                changed = True
        return changed

    new_seq = int(masters[0].get("SEQUENCE", 0))
    old = find_event(cal, uid)
    if old is not None and new_seq < int(old.get("SEQUENCE", 0)):
        return False

    remove_event(cal, uid)
    for c in components:
        cal.add_component(c)
    return True


def _exdate_keys(component: Event) -> set[int]:
    raw = component.get("EXDATE")
    if raw is None:
        return set()
    groups = raw if isinstance(raw, list) else [raw]
    keys = set()
    for group in groups:
        for item in getattr(group, "dts", []):
            keys.add(int(aware(item.dt).timestamp()))
    return keys


def cancel_occurrence(cal: Calendar, uid: str, rid_prop: Any) -> bool:
    """Cancel ONE instance of a series: remove a stored override for it, and
    add it to the master's EXDATE (in the same form as the RECURRENCE-ID it
    came with). Returns True if anything changed."""
    rid = int(aware(rid_prop.dt).timestamp())
    before = len(cal.subcomponents)
    cal.subcomponents = [c for c in cal.subcomponents if not _same(c, uid, rid)]
    changed = len(cal.subcomponents) != before

    master = find_event(cal, uid)
    if master is not None and is_recurring(master) and rid not in _exdate_keys(master):
        master.add("exdate", rid_prop.dt)
        changed = True
    return changed


def find_event(cal: Calendar, uid: str) -> Event | None:
    """The MASTER (or plain) VEVENT for uid, or None. Falls back to the first
    override when a UID has no master at all (an invite to a single instance
    of someone else's series)."""
    fallback = None
    for c in cal.subcomponents:
        if c.name == "VEVENT" and str(c.get("UID")) == uid:
            if recurrence_key(c) is None:
                return c
            if fallback is None:
                fallback = c
    return fallback


def all_uids(cal: Calendar) -> set[str]:
    """Every VEVENT UID currently in the calendar."""
    return {
        str(c.get("UID"))
        for c in cal.subcomponents
        if c.name == "VEVENT" and c.get("UID") is not None
    }


def get_event_end(component: Event) -> datetime.datetime | None:
    """Best effort tz aware end time for ONE VEVENT instance (DTEND, else
    DTSTART; date only means the end of that local day). Ignores RRULE: for
    a series, use series_end()."""
    for prop in ("DTEND", "DTSTART"):
        raw = component.get(prop)
        if raw is None:
            continue
        value = raw.dt
        if isinstance(value, datetime.datetime):
            return aware(value)
        return dt_util.start_of_local_day(value) + datetime.timedelta(days=1)
    return None


def prune_past_events(
    cal: Calendar, cutoff: datetime.datetime, only_uids: set[str] | None = None
) -> list[str]:
    """Drop VEVENTs that finished before `cutoff` (tz aware).

    `only_uids`: when given, only events with one of these UIDs may be
    pruned (a shared store passes the managed UIDs). A recurring master is
    judged by series_end(); an unbounded series, or one whose end can't be
    computed, is kept. Overrides are judged on their own instance.
    Returns the UIDs that no longer have any component left.
    """
    kept = []
    touched: set[str] = set()
    for c in cal.subcomponents:
        if c.name != "VEVENT":
            kept.append(c)
            continue
        uid = str(c.get("UID"))
        if only_uids is not None and uid not in only_uids:
            kept.append(c)
            continue
        try:
            if is_recurring(c) and recurrence_key(c) is None:
                end = series_end(c)
            else:
                end = get_event_end(c)
        except Exception:  # noqa: BLE001
            end = None  # unreadable dates: keep it, never crash the poll
        if end is not None and end < cutoff:
            touched.add(uid)
            continue
        kept.append(c)
    if not touched:
        return []
    cal.subcomponents = kept
    live = all_uids(cal)
    return sorted(touched - live)


# --------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------


def address_of(value: Any) -> str | None:
    """Plain email address from a CAL-ADDRESS value (strips mailto:)."""
    if value is None:
        return None
    addr = str(value).strip()
    if addr.lower().startswith("mailto:"):
        addr = addr[7:]
    addr = addr.strip().strip("<>").strip()
    return addr or None


def get_organizer_email(component: Event) -> str | None:
    """The plain email address from a VEVENT's ORGANIZER property."""
    return address_of(component.get("ORGANIZER"))


def get_attendee_emails(component: Event) -> list[str]:
    """Every ATTENDEE address on a VEVENT, lowercased."""
    raw = component.get("ATTENDEE")
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    out = []
    for value in values:
        addr = address_of(value)
        if addr:
            out.append(addr.lower())
    return out


# --------------------------------------------------------------------
# iMIP bookkeeping properties
# --------------------------------------------------------------------


def touch_dtstamp(event: Event) -> None:
    """Set DTSTAMP to now (UTC), replacing any existing value."""
    event.pop("DTSTAMP", None)
    event.add("dtstamp", dt_util.utcnow())


def bump_sequence(event: Event) -> int:
    """Increment SEQUENCE and return the new value."""
    new_seq = int(event.get("SEQUENCE", 0)) + 1
    event.pop("SEQUENCE", None)
    event.add("sequence", new_seq)
    return new_seq
