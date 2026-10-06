"""Diagnostics: settings and counts, never credentials or event content.

Mail addresses, hosts and the CalDAV URL (it contains the user name) are
redacted too: a diagnostics file gets pasted into public issues.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import InviteCalendarConfigEntry
from .const import (
    CONF_CALDAV_PASSWORD,
    CONF_CALDAV_URL,
    CONF_CALDAV_USERNAME,
    CONF_ICS_PATH,
    CONF_SMTP_HOST,
    CONF_SMTP_PASSWORD,
    CONF_SMTP_USERNAME,
)
from .ical import events

TO_REDACT = {
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CALDAV_URL,
    CONF_CALDAV_USERNAME,
    CONF_CALDAV_PASSWORD,
    CONF_ICS_PATH,
    CONF_SMTP_HOST,
    CONF_SMTP_USERNAME,
    CONF_SMTP_PASSWORD,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: InviteCalendarConfigEntry
) -> dict[str, Any]:
    """Settings, state sizes and the last poll result."""
    coordinator = entry.runtime_data
    opts = coordinator.options
    state = coordinator.state
    cal = coordinator.data
    components = list(cal.walk("VEVENT")) if cal is not None else []
    own = opts.address.lower()
    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "resolved_options": {
            "accept_policy": opts.accept_policy,
            "missing_location_reply": opts.missing_location_reply,
            "retention_days": opts.retention_days,
            "scan_interval_seconds": opts.scan_interval.total_seconds(),
            "smtp_port": opts.smtp.port,
            "smtp_overridden": opts.smtp.host != entry.data[CONF_HOST]
            or opts.smtp.username != entry.data[CONF_USERNAME],
        },
        "store": {
            "type": type(coordinator.store).__name__,
            "shared": coordinator.store.shared,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "update_interval_seconds": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
        },
        "calendar": {
            "uids": len(events.all_uids(cal)) if cal is not None else 0,
            "vevents": len(components),
            "recurring_masters": sum(
                1
                for c in components
                if events.is_recurring(c) and events.recurrence_key(c) is None
            ),
            "overrides": sum(
                1 for c in components if events.recurrence_key(c) is not None
            ),
            "own_events": len(
                {
                    str(c.get("UID"))
                    for c in components
                    if (events.get_organizer_email(c) or "").lower() == own
                }
            ),
        },
        "state": {
            "managed": len(state.organizer),
            "accepted": len(state.accepted),
            "failed_messages": len(state.failed),
            "sent_threads": len(state.sent),
            "rsvp_failed": len(state.rsvp_failed),
            "pending_invites": len(state.pending),
        },
    }
