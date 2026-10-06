"""Config flow (milestone 0 placeholder)."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.invite_calendar.const import CONF_CALENDAR_NAME, DOMAIN


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The user step creates an entry named after the calendar."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CALENDAR_NAME: "  tesla  "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "tesla"
    assert result["data"] == {CONF_CALENDAR_NAME: "tesla"}


async def test_user_flow_rejects_blank_name(hass: HomeAssistant) -> None:
    """A blank name shows the form again with an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CALENDAR_NAME: "   "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CALENDAR_NAME: "name_required"}
