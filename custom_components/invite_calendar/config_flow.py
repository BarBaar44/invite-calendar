"""Config flow for Invite Calendar.

Milestone 0 placeholder: asks only for the calendar name. Milestone 1
replaces this with the mailbox, store and name steps from DESIGN.md.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_CALENDAR_NAME, DOMAIN

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_CALENDAR_NAME): str})


class InviteCalendarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Invite Calendar."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the calendar name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_CALENDAR_NAME].strip()
            if not name:
                errors[CONF_CALENDAR_NAME] = "name_required"
            else:
                return self.async_create_entry(
                    title=name, data={CONF_CALENDAR_NAME: name}
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
