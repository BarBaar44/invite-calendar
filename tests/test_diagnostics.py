"""Diagnostics never leak credentials, addresses or event content."""

from __future__ import annotations

import datetime
import json

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.invite_calendar.const import CONF_SMTP_SECTION

from .conftest import FakeMailbox
from .helpers import mail, vev


async def test_diagnostics_redacted(
    hass: HomeAssistant,
    hass_client,
    mailbox: FakeMailbox,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            "accept_policy": "manual",
            CONF_SMTP_SECTION: {
                "smtp_username": "relay@example.com",
                "smtp_password": "relay-pw",
            },
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    start = (dt_util.now() + datetime.timedelta(days=2)).replace(microsecond=0)
    mailbox.add(
        mail("REQUEST", [vev("secret-uid", start, summary="Secret meeting")], "m1")
    )
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)
    text = json.dumps(diag)
    for secret in (
        "pw",
        "relay-pw",
        "tesla@example.com",
        "relay@example.com",
        "mail.example.com",
        "tesla.ics",
        "Secret meeting",
        "secret-uid",
        "boss@ext.com",
    ):
        assert f'"{secret}"' not in text and secret not in text.replace(
            '"**REDACTED**"', ""
        ), secret
    assert diag["resolved_options"]["accept_policy"] == "manual"
    assert diag["resolved_options"]["smtp_overridden"] is True
    assert diag["calendar"]["uids"] == 1
    assert diag["state"]["managed"] == 1
    assert diag["coordinator"]["last_update_success"] is True
