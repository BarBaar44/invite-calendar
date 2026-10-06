"""Accept policy "if_free": accept an invitation when its time is free,
decline it (or the clashing occurrences of a series) when it is not.
Pure functions; the coordinator sends the replies and changes the calendar.

WHAT BLOCKS. Only timed events: not all day ones, not ones marked free
(TRANSP:TRANSPARENT), not cancelled ones (STATUS:CANCELLED). An invitation
that is itself all day or marked free is accepted without a check.

FIRST COME, FIRST SERVED. Invitations are decided once, when they arrive
(and again when the organizer sends a new version, a higher SEQUENCE).
Whatever is already in the calendar blocks: events made by hand, events the
calendar organizes, accepted invitations, and invitations decided earlier
in the same scan. Invitations still waiting for their decision do not
block each other; the oldest (DTSTAMP) is decided first. Something added
later never turns an earlier acceptance into a decline.

SERIES. The series is accepted, and each occurrence that clashes within
HORIZON from now is declined on its own (a REPLY with RECURRENCE-ID) and
taken out of the calendar (EXDATE). Occurrences further out are not
checked: checking them later, as they come closer, would decline them
because of events that arrived after the series was accepted.

A SINGLE EVENT that clashes is declined and left out of the calendar.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import recurring_ical_events
from icalendar import Calendar, Event

from .const import FREE_HORIZON
from .ical import events
from .replies import event_end, rsvp_target
from .state import EntryState

Interval = tuple[datetime.datetime, datetime.datetime]


@dataclass(slots=True)
class FreeDecision:
    """What to answer for one invitation (one UID at one SEQUENCE)."""

    uid: str
    sequence: int
    component: Event
    # True: decline the whole event and leave it out of the calendar.
    decline_whole: bool = False
    # Series: occurrence starts (RECURRENCE-ID values) to decline.
    declined_occurrences: list[datetime.datetime] = field(default_factory=list)


def blocks(component: Event) -> bool:
    """True when this (expanded) component occupies time: timed, not
    marked free, not cancelled."""
    start = component.get("DTSTART")
    if start is None or not isinstance(start.dt, datetime.datetime):
        return False
    if str(component.get("TRANSP", "")).upper() == "TRANSPARENT":
        return False
    return str(component.get("STATUS", "")).upper() != "CANCELLED"


def _span(component: Event) -> Interval:
    start = events.aware(component["DTSTART"].dt)
    end_raw = component.get("DTEND")
    if end_raw is not None:
        # A date only DTEND is local midnight: an all day event spans the
        # whole day (whether it blocks is decided by blocks(), not here).
        end = events.aware(end_raw.dt)
    elif component.get("DURATION") is not None:
        end = start + component["DURATION"].dt
    elif not isinstance(component["DTSTART"].dt, datetime.datetime):
        end = start + datetime.timedelta(days=1)
    else:
        end = start
    return start, max(start, end)


def _expand(
    cal: Calendar, start: datetime.datetime, end: datetime.datetime
) -> list[Event]:
    return list(recurring_ical_events.of(cal, skip_bad_series=True).between(start, end))


def _overlaps(a: Interval, b: Interval) -> bool:
    # Zero length events still clash with what they sit inside.
    if a[0] == a[1]:
        return b[0] <= a[0] < b[1]
    if b[0] == b[1]:
        return a[0] <= b[0] < a[1]
    return a[0] < b[1] and b[0] < a[1]


def _components(cal: Calendar, uid: str) -> Calendar:
    """A calendar holding only the VEVENTs of `uid` (plus VTIMEZONEs)."""
    out = events.new_calendar()
    for c in cal.subcomponents:
        if c.name == "VTIMEZONE" or (c.name == "VEVENT" and str(c.get("UID")) == uid):
            out.add_component(c)
    return out


def _stamp(component: Event) -> datetime.datetime:
    raw = component.get("DTSTAMP")
    try:
        return (
            events.aware(raw.dt)
            if raw is not None
            else datetime.datetime.max.replace(tzinfo=datetime.UTC)
        )
    except Exception:  # noqa: BLE001 - unreadable DTSTAMP: decide last
        return datetime.datetime.max.replace(tzinfo=datetime.UTC)


def free_candidates(
    cal: Calendar, state: EntryState, own_address: str, now: datetime.datetime
) -> list[Event]:
    """Managed invitations from someone else, not answered at their current
    SEQUENCE and not over yet, oldest first."""
    own = own_address.lower()
    out = []
    for uid in state.organizer:
        component = rsvp_target(cal, uid)
        if component is None:
            continue
        organizer = events.get_organizer_email(component)
        if not organizer or organizer.lower() == own:
            continue
        seq = int(component.get("SEQUENCE", 0))
        if state.accepted.get(uid) == seq or state.rsvp_failed.get(uid) == seq:
            continue
        if state.declined_sequence(uid) == seq:
            continue
        end = event_end(component)
        if end is not None and end < now:
            continue
        out.append(component)
    out.sort(key=lambda c: (_stamp(c), str(c.get("UID"))))
    return out


def decide(
    cal: Calendar, state: EntryState, own_address: str, now: datetime.datetime
) -> list[FreeDecision]:
    """Accept or decline every candidate, oldest first (see the module
    docstring for the rules)."""
    candidates = free_candidates(cal, state, own_address, now)
    if not candidates:
        return []
    undecided = {str(c.get("UID")) for c in candidates}
    horizon = now + FREE_HORIZON

    # Everything that might block, expanded once over the widest window.
    window_end = horizon
    for c in candidates:
        if not events.is_recurring(c) and c.get("DTSTART") is not None:
            window_end = max(window_end, _span(c)[1] + datetime.timedelta(minutes=1))
    busy: list[tuple[str, Interval]] = [
        (str(o.get("UID")), _span(o))
        for o in _expand(cal, now, window_end)
        if blocks(o)
    ]

    decisions: list[FreeDecision] = []
    for component in candidates:
        uid = str(component.get("UID"))
        seq = int(component.get("SEQUENCE", 0))
        undecided.discard(uid)
        decision = FreeDecision(uid=uid, sequence=seq, component=component)
        decisions.append(decision)
        others = [span for u, span in busy if u != uid and u not in undecided]

        series = (
            events.is_recurring(component) and events.recurrence_key(component) is None
        )
        if not series:
            if blocks(component) and any(
                _overlaps(_span(component), b) for b in others
            ):
                decision.decline_whole = True
                # A declined event no longer blocks anything.
                busy = [(u, s) for u, s in busy if u != uid]
            continue

        for occurrence in _expand(_components(cal, uid), now, horizon):
            if not blocks(occurrence):
                continue
            span = _span(occurrence)
            if span[1] <= now:
                continue
            if any(_overlaps(span, b) for b in others):
                rid = occurrence.get("RECURRENCE-ID")
                decision.declined_occurrences.append(
                    rid.dt if rid is not None else occurrence["DTSTART"].dt
                )
        if decision.declined_occurrences:
            gone = {
                int(events.aware(d).timestamp()) for d in decision.declined_occurrences
            }
            busy = [
                (u, s)
                for u, s in busy
                if not (u == uid and int(s[0].timestamp()) in gone)
            ]
    return decisions


def occurrence_stub(master: Event, rid: datetime.datetime) -> Event:
    """The component a REPLY for one occurrence answers: the master's UID,
    SEQUENCE, ORGANIZER and SUMMARY with RECURRENCE-ID (and DTSTART) set to
    the occurrence."""
    stub = Event()
    stub.add("uid", str(master.get("UID")))
    stub.add("sequence", int(master.get("SEQUENCE", 0)))
    if (organizer := master.get("ORGANIZER")) is not None:
        stub.add("organizer", organizer)
    if (summary := master.get("SUMMARY")) is not None:
        stub.add("summary", str(summary))
    stub.add("recurrence-id", rid)
    stub.add("dtstart", rid)
    return stub


def apply_declines(cal: Calendar, state: EntryState) -> list[str]:
    """Bring the calendar in line with the recorded declines, for the
    version (SEQUENCE) they were made for: a declined event is removed, a
    declined occurrence gets an EXDATE. Idempotent. Returns the UIDs that
    changed."""
    changed = []
    for uid, record in state.declined.items():
        component = events.find_event(cal, uid)
        if component is None:
            continue
        if int(component.get("SEQUENCE", 0)) != int(record.get("sequence", -1)):
            continue  # a newer version: it gets decided again
        if record.get("whole"):
            if events.remove_event(cal, uid):
                changed.append(uid)
            continue
        hit = False
        for iso in record.get("occurrences", []):
            # UTC: an ISO string keeps only the offset, and a UTC EXDATE is
            # valid next to TZID ones (matched by instant, not by text).
            rid = datetime.datetime.fromisoformat(iso).astimezone(datetime.UTC)
            if events.cancel_occurrence(cal, uid, _Prop(rid)):
                hit = True
        if hit:
            changed.append(uid)
    return changed


@dataclass(slots=True)
class _Prop:
    """Just enough of an icalendar property for events.cancel_occurrence."""

    dt: datetime.datetime
