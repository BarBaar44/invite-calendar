"""Entry options with their defaults, read in one place.

Defaults that depend on the entry (store type, title, IMAP login) are
resolved here, so the coordinator, the options flow and the services all
agree on them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from .const import (
    ACCEPT_NEVER,
    CONF_ACCEPT_POLICY,
    CONF_ATTENDEE_CN,
    CONF_FROM_NAME,
    CONF_LOOKBACK_DAYS,
    CONF_MISSING_LOCATION_REPLY,
    CONF_MISSING_LOCATION_TEXT,
    CONF_RETENTION_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SMTP_HOST,
    CONF_SMTP_PASSWORD,
    CONF_SMTP_PORT,
    CONF_SMTP_SECTION,
    CONF_SMTP_USERNAME,
    CONF_STORE_TYPE,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MISSING_LOCATION_TEXT,
    DEFAULT_RETENTION_DAYS_CALDAV,
    DEFAULT_RETENTION_DAYS_ICS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SMTP_PORT,
    STORE_CALDAV,
)
from .mail.smtp import SmtpSettings


@dataclass(frozen=True, slots=True)
class EntryOptions:
    """Everything the options flow controls, defaults applied."""

    accept_policy: str
    missing_location_reply: bool
    missing_location_text: str
    retention_days: int
    lookback_days: int
    scan_interval: timedelta
    from_name: str
    attendee_cn: str
    smtp: SmtpSettings
    # The calendar's own address: ORGANIZER of what it creates, ATTENDEE in
    # its replies. Always the IMAP username.
    address: str

    @property
    def sends_mail(self) -> bool:
        """True when anything may be sent automatically."""
        return self.accept_policy != ACCEPT_NEVER or self.missing_location_reply


def default_retention(data: Mapping[str, Any]) -> int:
    """30 days for a dedicated .ics, off for a shared CalDAV calendar."""
    if data.get(CONF_STORE_TYPE) == STORE_CALDAV:
        return DEFAULT_RETENTION_DAYS_CALDAV
    return DEFAULT_RETENTION_DAYS_ICS


def smtp_settings(data: Mapping[str, Any], smtp: Mapping[str, Any]) -> SmtpSettings:
    """SMTP override fields, each falling back to the IMAP login."""
    return SmtpSettings(
        host=(smtp.get(CONF_SMTP_HOST) or data[CONF_HOST]).strip(),
        port=int(smtp.get(CONF_SMTP_PORT) or DEFAULT_SMTP_PORT),
        username=(smtp.get(CONF_SMTP_USERNAME) or data[CONF_USERNAME]).strip(),
        password=smtp.get(CONF_SMTP_PASSWORD) or data[CONF_PASSWORD],
    )


def resolve(
    data: Mapping[str, Any], options: Mapping[str, Any], title: str
) -> EntryOptions:
    """Options with every default filled in."""
    minutes = options.get(CONF_SCAN_INTERVAL_MINUTES)
    retention = options.get(CONF_RETENTION_DAYS)
    lookback = options.get(CONF_LOOKBACK_DAYS)
    return EntryOptions(
        accept_policy=options.get(CONF_ACCEPT_POLICY, ACCEPT_NEVER),
        missing_location_reply=bool(options.get(CONF_MISSING_LOCATION_REPLY, False)),
        missing_location_text=options.get(CONF_MISSING_LOCATION_TEXT)
        or DEFAULT_MISSING_LOCATION_TEXT,
        retention_days=int(retention)
        if retention is not None
        else default_retention(data),
        lookback_days=int(lookback) if lookback is not None else DEFAULT_LOOKBACK_DAYS,
        scan_interval=timedelta(minutes=int(minutes))
        if minutes
        else DEFAULT_SCAN_INTERVAL,
        from_name=options.get(CONF_FROM_NAME) or f"{title} Calendar",
        attendee_cn=options.get(CONF_ATTENDEE_CN) or title,
        smtp=smtp_settings(data, options.get(CONF_SMTP_SECTION) or {}),
        address=data[CONF_USERNAME].strip(),
    )


def entry_options(entry: ConfigEntry) -> EntryOptions:
    """Resolved options of a config entry."""
    return resolve(entry.data, entry.options, entry.title)
