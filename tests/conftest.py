"""Shared fixtures for Invite Calendar tests."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar.const import CONF_CALENDAR_NAME, DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components."""
    return


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A minimal config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="tesla",
        data={CONF_CALENDAR_NAME: "tesla"},
    )
