"""Calendar entity: the store's events, recurring series expanded."""

from __future__ import annotations

import datetime
import logging

import recurring_ical_events
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from icalendar import Calendar, Event

from . import InviteCalendarConfigEntry
from .const import DOMAIN
from .coordinator import InviteCalendarCoordinator
from .ical.events import aware

_LOGGER = logging.getLogger(__name__)

# Window kept ready for the `event` property (current or next event).
UPCOMING_WINDOW = datetime.timedelta(days=400)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InviteCalendarConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One calendar entity per entry."""
    async_add_entities([InviteCalendarEntity(entry.runtime_data, entry)])


def _to_calendar_event(component: Event) -> CalendarEvent | None:
    """One expanded occurrence as a CalendarEvent, or None to hide it."""
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start_raw = component.get("DTSTART")
    if start_raw is None:
        return None
    start = start_raw.dt
    end_raw = component.get("DTEND")
    end = end_raw.dt if end_raw is not None else None

    if isinstance(start, datetime.datetime):
        start = aware(start)
        # datetime is a date subclass: a date only DTEND becomes local midnight
        end = aware(end) if isinstance(end, datetime.date) else start
        end = max(end, start)
    else:
        if not isinstance(end, datetime.date) or isinstance(end, datetime.datetime):
            end = start + datetime.timedelta(days=1)
        end = max(end, start + datetime.timedelta(days=1))

    rid = component.get("RECURRENCE-ID")
    description = component.get("DESCRIPTION")
    location = component.get("LOCATION")
    return CalendarEvent(
        start=start,
        end=end,
        summary=str(component.get("SUMMARY", "")),
        description=str(description) if description else None,
        location=str(location) if location else None,
        uid=str(component.get("UID")) if component.get("UID") else None,
        recurrence_id=rid.to_ical().decode() if rid is not None else None,
    )


def expand(
    cal: Calendar, start: datetime.datetime, end: datetime.datetime
) -> list[CalendarEvent]:
    """Every occurrence overlapping [start, end), sorted by start.

    Uses recurring-ical-events, which applies EXDATE and RECURRENCE-ID
    overrides and keeps wall clock time across DST. A series that can't be
    expanded is skipped instead of hiding the whole calendar.
    """
    out: list[CalendarEvent] = []
    query = recurring_ical_events.of(cal, skip_bad_series=True)
    for component in query.between(start, end):
        try:
            event = _to_calendar_event(component)
        except HomeAssistantError as err:
            _LOGGER.warning("Skipping event %s: %s", component.get("UID"), err)
            continue
        if event is not None:
            out.append(event)
    out.sort(key=lambda e: e.start_datetime_local)
    return out


class InviteCalendarEntity(
    CoordinatorEntity[InviteCalendarCoordinator], CalendarEntity
):
    """The calendar of one mailbox."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, coordinator: InviteCalendarCoordinator, entry: InviteCalendarConfigEntry
    ) -> None:
        """Entity for `entry`."""
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Invite Calendar",
            model=coordinator.imap_settings.username,
        )
        self._upcoming: list[CalendarEvent] = []
        self._refresh_upcoming()

    @callback
    def _refresh_upcoming(self) -> None:
        cal = self.coordinator.data
        if cal is None:
            self._upcoming = []
            return
        now = dt_util.now()
        self._upcoming = expand(
            cal, now - datetime.timedelta(days=1), now + UPCOMING_WINDOW
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_upcoming()
        super()._handle_coordinator_update()

    @property
    def event(self) -> CalendarEvent | None:
        """The current event, else the next upcoming one."""
        now = dt_util.now()
        for event in self._upcoming:
            if event.end_datetime_local > now:
                return event
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        """Occurrences between start_date and end_date."""
        cal = self.coordinator.data
        if cal is None:
            return []
        return await hass.async_add_executor_job(expand, cal, start_date, end_date)

    async def async_poll(self) -> None:
        """Service invite_calendar.poll: poll the mailbox now."""
        await self.coordinator.async_refresh()
        if not self.coordinator.last_update_success:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="poll_failed",
                translation_placeholders={
                    "error": str(self.coordinator.last_exception)
                },
            )
