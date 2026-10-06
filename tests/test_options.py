"""Options: defaults and the options flow."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar.const import (
    CONF_ACCEPT_POLICY,
    CONF_ATTENDEE_CN,
    CONF_FROM_NAME,
    CONF_MISSING_LOCATION_REPLY,
    CONF_RETENTION_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SMTP_HOST,
    CONF_SMTP_PASSWORD,
    CONF_SMTP_PORT,
    CONF_SMTP_SECTION,
    CONF_SMTP_USERNAME,
)
from custom_components.invite_calendar.mail import smtp
from custom_components.invite_calendar.options import entry_options

from .conftest import FakeMailbox

BASE = {
    CONF_ACCEPT_POLICY: "never",
    CONF_MISSING_LOCATION_REPLY: False,
    CONF_RETENTION_DAYS: 30,
    CONF_SCAN_INTERVAL_MINUTES: 5,
    CONF_SMTP_SECTION: {},
}


def test_defaults(
    mock_config_entry: MockConfigEntry, caldav_entry: MockConfigEntry
) -> None:
    opts = entry_options(mock_config_entry)
    assert opts.accept_policy == "never"
    assert opts.retention_days == 30
    assert opts.scan_interval == timedelta(minutes=5)
    assert opts.from_name == "Tesla Calendar" and opts.attendee_cn == "Tesla"
    assert opts.address == "tesla@example.com"
    assert opts.smtp == smtp.SmtpSettings(
        "mail.example.com", 587, "tesla@example.com", "pw"
    )
    assert not opts.sends_mail
    assert entry_options(caldav_entry).retention_days == 0


async def start_options(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    return result


async def test_options_saved_and_applied(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    result = await start_options(hass, setup_entry)
    with patch.object(smtp, "validate") as validate:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                **BASE,
                CONF_ACCEPT_POLICY: "manual",
                CONF_SCAN_INTERVAL_MINUTES: 15.0,
                CONF_FROM_NAME: "  Auto  ",
                CONF_ATTENDEE_CN: "",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert validate.call_count == 1
    assert setup_entry.options == {
        **BASE,
        CONF_ACCEPT_POLICY: "manual",
        CONF_SCAN_INTERVAL_MINUTES: 15,
        CONF_FROM_NAME: "Auto",
    }
    await hass.async_block_till_done()
    # Reloaded with the new options.
    coordinator = setup_entry.runtime_data
    assert coordinator.update_interval == timedelta(minutes=15)
    assert coordinator.options.accept_policy == "manual"
    assert coordinator.options.attendee_cn == "Tesla"
    await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_no_smtp_check_when_nothing_is_sent(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    result = await start_options(hass, setup_entry)
    with patch.object(smtp, "validate") as validate:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**BASE, CONF_RETENTION_DAYS: 0}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert validate.call_count == 0
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(setup_entry.entry_id)


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (smtp.SmtpAuthError("535"), "smtp_invalid_auth"),
        (smtp.SmtpError("down"), "smtp_cannot_connect"),
    ],
)
async def test_smtp_errors(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    mailbox: FakeMailbox,
    error: Exception,
    key: str,
) -> None:
    result = await start_options(hass, setup_entry)
    with patch.object(smtp, "validate", side_effect=error):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**BASE, CONF_ACCEPT_POLICY: "always"}
        )
    assert result["errors"] == {"base": key}
    await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_username_must_be_an_address(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, "username": "tesla"}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    result = await start_options(hass, mock_config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASE, CONF_MISSING_LOCATION_REPLY: True}
    )
    assert result["errors"] == {"base": "username_not_address"}
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_smtp_override_keeps_password(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    smtp_in = {
        CONF_SMTP_HOST: "relay.example.com",
        CONF_SMTP_PORT: 465.0,
        CONF_SMTP_USERNAME: "ha@example.com",
        CONF_SMTP_PASSWORD: "relay-pw",
    }
    with patch.object(smtp, "validate") as validate:
        result = await start_options(hass, setup_entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {**BASE, CONF_ACCEPT_POLICY: "always", CONF_SMTP_SECTION: smtp_in},
        )
        await hass.async_block_till_done()
        assert validate.call_args.args[0] == smtp.SmtpSettings(
            "relay.example.com", 465, "ha@example.com", "relay-pw"
        )

        # Saving again with the password field empty keeps it.
        result = await start_options(hass, setup_entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                **BASE,
                CONF_ACCEPT_POLICY: "always",
                CONF_SMTP_SECTION: {**smtp_in, CONF_SMTP_PASSWORD: ""},
            },
        )
        await hass.async_block_till_done()
    assert setup_entry.options[CONF_SMTP_SECTION][CONF_SMTP_PASSWORD] == "relay-pw"
    assert setup_entry.options[CONF_SMTP_SECTION][CONF_SMTP_PORT] == 465
    await hass.config_entries.async_unload(setup_entry.entry_id)
