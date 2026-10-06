"""Which events get an RSVP, and building the messages. Pure functions.

ONE RSVP PER SERIES, for its master: accepting overrides separately flips
the stored SEQUENCE back and forth and re-sends forever. An override is only
accepted on its own when its master isn't there (an invite to a single
instance of someone else's series).

The "accepted" marker is written by the caller only after a confirmed send,
so a failed send is retried on the next poll without needing the email.
"""

from __future__ import annotations

import datetime

from icalendar import Calendar, Event

from .const import ACCEPT_ALWAYS, ACCEPT_IF_LOCATION
from .ical import events
from .state import EntryState


def event_end(component: Event) -> datetime.datetime | None:
    """End of the last occurrence (series) or of the event; None: unknown or
    never ending."""
    try:
        if events.is_recurring(component) and events.recurrence_key(component) is None:
            return events.series_end(component)
        return events.get_event_end(component)
    except Exception:  # noqa: BLE001 - unreadable dates: treat as unknown
        return None


def rsvp_target(cal: Calendar, uid: str) -> Event | None:
    """The component an RSVP for `uid` answers: the master, or the first
    override when there is no master."""
    return events.find_event(cal, uid)


def rsvp_candidates(
    cal: Calendar,
    state: EntryState,
    policy: str,
    own_address: str,
    now: datetime.datetime,
) -> list[Event]:
    """Components that should get an ACCEPTED reply now, under `policy`
    (always or if_location; other policies never send automatically)."""
    if policy not in (ACCEPT_ALWAYS, ACCEPT_IF_LOCATION):
        return []
    own = own_address.lower()
    out = []
    for uid in sorted(state.organizer):
        component = rsvp_target(cal, uid)
        if component is None:
            continue
        organizer = events.get_organizer_email(component)
        if not organizer or organizer.lower() == own:
            continue
        seq = int(component.get("SEQUENCE", 0))
        if state.accepted.get(uid) == seq or state.rsvp_failed.get(uid) == seq:
            continue
        if policy == ACCEPT_IF_LOCATION and not str(
            component.get("LOCATION", "") or ""
        ):
            continue
        end = event_end(component)
        if end is not None and end < now:
            continue  # over already: an RSVP would be noise
        out.append(component)
    return out
