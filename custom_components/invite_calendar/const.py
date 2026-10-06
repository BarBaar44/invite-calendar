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
# Only used by 0.1.0 entries, see async_migrate_entry.
CONF_CALENDAR_NAME: Final = "calendar_name"

STORE_ICS: Final = "ics"

DEFAULT_IMAP_PORT: Final = 993
DEFAULT_FOLDER: Final = "INBOX"
# A private IMAP keyword marks processed mail, never \Seen. CHANGING IT on a
# live mailbox makes every message in it look new.
DEFAULT_PROCESSED_KEYWORD: Final = "InviteCalendarProcessed"
DEFAULT_ICS_DIR: Final = "invite_calendar"

# Options arrive in milestone 3; these are the fixed values until then.
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)
DEFAULT_RETENTION_DAYS_ICS: Final = 30
MAX_MESSAGE_ATTEMPTS: Final = 3

# Bus events
EVENT_UPDATED: Final = "invite_calendar_updated"
EVENT_INVITE_RECEIVED: Final = "invite_calendar_invite_received"

SERVICE_POLL: Final = "poll"

STATE_STORAGE_VERSION: Final = 1
