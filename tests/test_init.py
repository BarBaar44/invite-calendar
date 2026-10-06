"""Setup, unload, removal and migration of config entries."""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar.const import CONF_CALENDAR_NAME, DOMAIN

from .conftest import FakeMailbox


async def test_setup_creates_file_and_unloads(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ics_path: Path
) -> None:
    """An entry loads, creates its .ics on the first poll, and unloads."""
    assert setup_entry.state is ConfigEntryState.LOADED
    assert ics_path.exists()
    assert hass.states.get("calendar.tesla") is not None

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED


async def test_remove_keeps_calendar_file(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ics_path: Path
) -> None:
    """Removing the entry deletes its state but never the user's calendar."""
    assert await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert ics_path.exists()


async def test_version_1_entry_is_not_migrated(
    hass: HomeAssistant, mailbox: FakeMailbox
) -> None:
    """A 0.1.0 entry holds no mailbox; migration fails clearly."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="old", version=1, data={CONF_CALENDAR_NAME: "old"}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
