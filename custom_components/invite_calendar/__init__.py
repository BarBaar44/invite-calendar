"""Invite Calendar: turn an IMAP mailbox of calendar invitations into a calendar.

Milestone 0: the integration loads and unloads an entry. Mailbox, store,
coordinator and calendar entity arrive in milestone 1.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import LOGGER

PLATFORMS: list[Platform] = []

type InviteCalendarConfigEntry = ConfigEntry[None]


async def async_setup_entry(
    hass: HomeAssistant, entry: InviteCalendarConfigEntry
) -> bool:
    """Set up an Invite Calendar entry."""
    LOGGER.debug("Setting up entry %s", entry.title)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: InviteCalendarConfigEntry
) -> bool:
    """Unload an Invite Calendar entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
