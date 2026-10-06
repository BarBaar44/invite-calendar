"""Config flow: mailbox, calendar file, reauth."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar.const import (
    CONF_FOLDER,
    CONF_ICS_PATH,
    CONF_PROCESSED_KEYWORD,
    CONF_STORE_TYPE,
    DOMAIN,
    STORE_ICS,
)
from custom_components.invite_calendar.mail import imap

from .conftest import FakeMailbox

MAILBOX = {
    CONF_HOST: "mail.example.com",
    CONF_PORT: 993,
    CONF_USERNAME: "vakantie@example.com",
    CONF_PASSWORD: "pw",
    CONF_FOLDER: "INBOX",
    CONF_PROCESSED_KEYWORD: "InviteCalendarProcessed",
}


async def start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_full_flow(
    hass: HomeAssistant, mailbox: FakeMailbox, config_dir: Path
) -> None:
    result = await start(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MAILBOX)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ics"

    path = str(config_dir / "invite_calendar" / "vakantie.ics")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Vakantie", CONF_ICS_PATH: path}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Vakantie"
    assert result["data"] == {
        **MAILBOX,
        CONF_STORE_TYPE: STORE_ICS,
        CONF_ICS_PATH: path,
    }
    assert result["result"].unique_id == "vakantie@example.com@mail.example.com/inbox"


async def test_ics_defaults(
    hass: HomeAssistant, mailbox: FakeMailbox, config_dir: Path
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MAILBOX)
    schema = {str(k): k.default() for k in result["data_schema"].schema}
    assert schema[CONF_NAME] == "Vakantie"
    assert schema[CONF_ICS_PATH] == str(config_dir / "invite_calendar" / "vakantie.ics")


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (imap.ImapAuthError("no"), "invalid_auth"),
        (imap.ImapConnectError("down"), "cannot_connect"),
        (imap.ImapFolderError("nope"), "folder_not_found"),
        (imap.ImapKeywordError("(\\Seen)"), "keywords_not_supported"),
    ],
)
async def test_mailbox_errors(
    hass: HomeAssistant, mailbox: FakeMailbox, error: Exception, key: str
) -> None:
    mailbox.validate_error = error
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MAILBOX)
    assert result["errors"] == {"base": key}

    mailbox.validate_error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MAILBOX)
    assert result["step_id"] == "ics"


async def test_invalid_keyword(hass: HomeAssistant, mailbox: FakeMailbox) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**MAILBOX, CONF_PROCESSED_KEYWORD: "has space"}
    )
    assert result["errors"] == {CONF_PROCESSED_KEYWORD: "invalid_keyword"}


async def test_same_mailbox_twice_aborts(
    hass: HomeAssistant, mailbox: FakeMailbox, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**MAILBOX, CONF_USERNAME: "Tesla@Example.com"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("relative/cal.ics", "invalid_path"),
        ("{config}/cal.txt", "invalid_path"),
        ("/tmp/elsewhere/cal.ics", "path_outside_config"),
        ("{config}/../escape/cal.ics", "path_outside_config"),
    ],
)
async def test_bad_paths(
    hass: HomeAssistant, mailbox: FakeMailbox, config_dir: Path, path: str, key: str
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MAILBOX)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "x", CONF_ICS_PATH: path.format(config=config_dir)},
    )
    assert result["errors"] == {CONF_ICS_PATH: key}


async def test_existing_file_is_accepted(
    hass: HomeAssistant, mailbox: FakeMailbox, config_dir: Path
) -> None:
    """Migration keeps an existing file such as /config/www/tesla.ics."""
    www = config_dir / "www"
    www.mkdir()
    (www / "tesla.ics").write_bytes(
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    )
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MAILBOX)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Tesla", CONF_ICS_PATH: str(www / "tesla.ics")}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_path_in_use(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    mock_config_entry: MockConfigEntry,
    ics_path: Path,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MAILBOX)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "x", CONF_ICS_PATH: str(ics_path)}
    )
    assert result["errors"] == {CONF_ICS_PATH: "path_in_use"}


async def test_reauth(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    result = await setup_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mailbox.validate_error = imap.ImapAuthError("no")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mailbox.validate_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_entry.data[CONF_PASSWORD] == "new"
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
