"""Constants for Invite Calendar."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

DOMAIN: Final = "invite_calendar"
LOGGER = logging.getLogger(__package__)

# Config entry data
CONF_FOLDER: Final = "folder"
CONF_PROCESSED_KEYWORD: Final = "processed_keyword"
CONF_STORE_TYPE: Final = "store_type"
CONF_ICS_PATH: Final = "ics_path"
CONF_CALDAV_URL: Final = "caldav_url"
CONF_CALDAV_USERNAME: Final = "caldav_username"
CONF_CALDAV_PASSWORD: Final = "caldav_password"
# Only used by 0.1.0 entries, see async_migrate_entry.
CONF_CALENDAR_NAME: Final = "calendar_name"

STORE_ICS: Final = "ics"
STORE_CALDAV: Final = "caldav"

DEFAULT_IMAP_PORT: Final = 993
DEFAULT_FOLDER: Final = "INBOX"
# A private IMAP keyword marks processed mail, never \Seen. CHANGING IT on a
# live mailbox makes every message in it look new.
DEFAULT_PROCESSED_KEYWORD: Final = "InviteCalendarProcessed"
DEFAULT_ICS_DIR: Final = "invite_calendar"

# Options (entry.options); see options.py for the defaults.
CONF_ACCEPT_POLICY: Final = "accept_policy"
CONF_MISSING_LOCATION_REPLY: Final = "missing_location_reply"
CONF_MISSING_LOCATION_TEXT: Final = "missing_location_text"
CONF_RETENTION_DAYS: Final = "retention_days"
CONF_LOOKBACK_DAYS: Final = "lookback_days"
CONF_SCAN_INTERVAL_MINUTES: Final = "scan_interval_minutes"
CONF_FROM_NAME: Final = "from_name"
CONF_ATTENDEE_CN: Final = "attendee_cn"
CONF_SMTP_SECTION: Final = "smtp"
CONF_SMTP_HOST: Final = "smtp_host"
CONF_SMTP_PORT: Final = "smtp_port"
CONF_SMTP_USERNAME: Final = "smtp_username"
CONF_SMTP_PASSWORD: Final = "smtp_password"

ACCEPT_NEVER: Final = "never"
ACCEPT_ALWAYS: Final = "always"
ACCEPT_IF_LOCATION: Final = "if_location"
# Only the accept_event service accepts: a consumer gates acceptance on its
# own check (the EV project: once the location geocodes).
ACCEPT_MANUAL: Final = "manual"
# Accept when the time is free, decline (the clashing occurrences) when not.
ACCEPT_IF_FREE: Final = "if_free"
ACCEPT_POLICIES: Final = (
    ACCEPT_NEVER,
    ACCEPT_ALWAYS,
    ACCEPT_IF_LOCATION,
    ACCEPT_IF_FREE,
    ACCEPT_MANUAL,
)
# if_free checks the occurrences of a series this far ahead, once, when the
# invitation arrives.
FREE_HORIZON: Final = timedelta(days=365)

DEFAULT_SMTP_PORT: Final = 587
DEFAULT_MISSING_LOCATION_TEXT: Final = (
    "This event doesn't have a location set, and the automation that reads "
    "this calendar needs one."
)

DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)
DEFAULT_RETENTION_DAYS_ICS: Final = 30
# Off on a shared CalDAV calendar; when enabled (M3 options) it only ever
# prunes managed events.
DEFAULT_RETENTION_DAYS_CALDAV: Final = 0
MAX_MESSAGE_ATTEMPTS: Final = 3
# Only mail that arrived this many days ago or later is read (IMAP SINCE);
# 0 reads the whole folder. Keeps a folder with history from importing
# every invitation it ever received.
DEFAULT_LOOKBACK_DAYS: Final = 14

# Bus events
EVENT_UPDATED: Final = "invite_calendar_updated"
EVENT_INVITE_RECEIVED: Final = "invite_calendar_invite_received"

SERVICE_POLL: Final = "poll"
SERVICE_ACCEPT_EVENT: Final = "accept_event"
SERVICE_LIST_EVENTS: Final = "list_events"
SERVICE_CREATE_EVENT: Final = "create_event"
SERVICE_UPDATE_EVENT: Final = "update_event"
SERVICE_CANCEL_EVENT: Final = "cancel_event"

STATE_STORAGE_VERSION: Final = 1
