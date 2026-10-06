"""Invite Calendar: turn an IMAP mailbox of calendar invitations into a calendar."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CALDAV_PASSWORD,
    CONF_CALDAV_URL,
    CONF_CALDAV_USERNAME,
    CONF_FOLDER,
    CONF_ICS_PATH,
    CONF_PROCESSED_KEYWORD,
    CONF_STORE_TYPE,
    DOMAIN,
    LOGGER,
    SERVICE_ACCEPT_EVENT,
    SERVICE_LIST_EVENTS,
    SERVICE_POLL,
    STORE_CALDAV,
)
from .coordinator import InviteCalendarCoordinator
from .mail.imap import ImapSettings
from .options import entry_options
from .state import StateStore
from .store import StoreBackend
from .store.caldav import CalDavSettings, CalDavStore
from .store.ics_file import IcsFileStore

PLATFORMS: list[Platform] = [Platform.CALENDAR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type InviteCalendarConfigEntry = ConfigEntry[InviteCalendarCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (they target calendar entities)."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_POLL,
        entity_domain=CALENDAR_DOMAIN,
        schema=None,
        func="async_poll",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_ACCEPT_EVENT,
        entity_domain=CALENDAR_DOMAIN,
        schema={vol.Required("uid"): cv.string},
        func="async_accept_event",
        supports_response=SupportsResponse.OPTIONAL,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_LIST_EVENTS,
        entity_domain=CALENDAR_DOMAIN,
        schema={
            vol.Optional("start"): cv.datetime,
            vol.Optional("end"): cv.datetime,
            vol.Optional("duration"): vol.All(cv.time_period, cv.positive_timedelta),
        },
        func="async_list_events",
        supports_response=SupportsResponse.ONLY,
    )
    return True


def imap_settings(entry: ConfigEntry) -> ImapSettings:
    """IMAP settings from an entry."""
    data = entry.data
    return ImapSettings(
        host=data[CONF_HOST],
        port=int(data[CONF_PORT]),
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        folder=data[CONF_FOLDER],
        keyword=data[CONF_PROCESSED_KEYWORD],
    )


def caldav_settings(data: Mapping[str, Any]) -> CalDavSettings:
    """CalDAV settings from entry data."""
    return CalDavSettings(
        url=data[CONF_CALDAV_URL],
        username=data[CONF_CALDAV_USERNAME],
        password=data[CONF_CALDAV_PASSWORD],
    )


def make_store(hass: HomeAssistant, entry: ConfigEntry) -> StoreBackend:
    """The entry's calendar store."""
    if entry.data.get(CONF_STORE_TYPE) == STORE_CALDAV:
        return CalDavStore(hass, caldav_settings(entry.data))
    return IcsFileStore(hass, entry.data[CONF_ICS_PATH])


async def async_setup_entry(
    hass: HomeAssistant, entry: InviteCalendarConfigEntry
) -> bool:
    """Set up an Invite Calendar entry."""
    coordinator = InviteCalendarCoordinator(
        hass, entry, imap_settings(entry), make_store(hass, entry), entry_options(entry)
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: InviteCalendarConfigEntry
) -> bool:
    """Unload an Invite Calendar entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the entry's state. The calendar itself (file or CalDAV
    collection) is left alone."""
    await StateStore(hass, entry.entry_id).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Entries from 0.1.0 (version 1) only held a name; they can't be
    migrated and must be removed and added again."""
    if entry.version == 1:
        LOGGER.error(
            "Invite Calendar entry '%s' was created by 0.1.0 and holds no "
            "mailbox. Delete it and add the integration again",
            entry.title,
        )
        return False
    return True
