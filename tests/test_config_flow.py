"""Config flow: mailbox, store menu, .ics file or CalDAV, reauth."""

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
    CONF_CALDAV_PASSWORD,
    CONF_CALDAV_URL,
    CONF_CALDAV_USERNAME,
    CONF_FOLDER,
    CONF_ICS_PATH,
    CONF_PROCESSED_KEYWORD,
    CONF_STORE_TYPE,
    DOMAIN,
    STORE_CALDAV,
    STORE_ICS,
)
from custom_components.invite_calendar.mail import imap

from .caldav_fake import FakeCalDav
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


async def to_store_step(hass: HomeAssistant, store: str, mailbox: dict | None = None):
    """Run the mailbox step and pick `store` from the menu."""
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], mailbox or MAILBOX
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "store"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": store}
    )
    assert result["step_id"] == store
    return result


def caldav_input(server: FakeCalDav, **overrides) -> dict:
    return {
        CONF_NAME: "Vakantie",
        CONF_CALDAV_URL: server.base.rstrip("/"),
        CONF_CALDAV_USERNAME: "bart",
        CONF_CALDAV_PASSWORD: server.password,
        **overrides,
    }


# ---- .ics ----------------------------------------------------------------


async def test_full_ics_flow(
    hass: HomeAssistant, mailbox: FakeMailbox, config_dir: Path
) -> None:
    result = await to_store_step(hass, STORE_ICS)
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
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(result["result"].entry_id)


async def test_ics_defaults(
    hass: HomeAssistant, mailbox: FakeMailbox, config_dir: Path
) -> None:
    result = await to_store_step(hass, STORE_ICS)
    schema = {str(k): k.default() for k in result["data_schema"].schema}
    assert schema[CONF_NAME] == "Vakantie"
    assert schema[CONF_ICS_PATH] == str(config_dir / "invite_calendar" / "vakantie.ics")


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
    result = await to_store_step(hass, STORE_ICS)
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
    result = await to_store_step(hass, STORE_ICS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Tesla", CONF_ICS_PATH: str(www / "tesla.ics")}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(result["result"].entry_id)


async def test_path_in_use(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    mock_config_entry: MockConfigEntry,
    ics_path: Path,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await to_store_step(hass, STORE_ICS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "x", CONF_ICS_PATH: str(ics_path)}
    )
    assert result["errors"] == {CONF_ICS_PATH: "path_in_use"}


# ---- mailbox -------------------------------------------------------------


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
    assert result["step_id"] == "store"


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


# ---- CalDAV --------------------------------------------------------------


async def test_full_caldav_flow(
    hass: HomeAssistant, mailbox: FakeMailbox, caldav_server: FakeCalDav
) -> None:
    result = await to_store_step(hass, STORE_CALDAV)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], caldav_input(caldav_server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        **MAILBOX,
        CONF_STORE_TYPE: STORE_CALDAV,
        CONF_CALDAV_URL: caldav_server.base,
        CONF_CALDAV_USERNAME: "bart",
        CONF_CALDAV_PASSWORD: caldav_server.password,
    }
    assert ("REPORT", caldav_server.base) in [(m, u) for m, u, _ in caldav_server.log]
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(result["result"].entry_id)


@pytest.mark.parametrize(
    ("overrides", "where", "key"),
    [
        ({CONF_CALDAV_PASSWORD: "wrong"}, "base", "caldav_invalid_auth"),
        (
            {CONF_CALDAV_URL: "https://cloud.example.com/remote.php/dav/x/"},
            "base",
            "caldav_not_calendar",
        ),
        ({CONF_CALDAV_URL: "cloud.example.com/cal"}, CONF_CALDAV_URL, "invalid_url"),
        ({CONF_NAME: "  "}, CONF_NAME, "name_required"),
    ],
)
async def test_caldav_errors(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    caldav_server: FakeCalDav,
    overrides: dict,
    where: str,
    key: str,
) -> None:
    result = await to_store_step(hass, STORE_CALDAV)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], caldav_input(caldav_server, **overrides)
    )
    assert result["errors"] == {where: key}


async def test_caldav_server_down(
    hass: HomeAssistant, mailbox: FakeMailbox, caldav_server: FakeCalDav
) -> None:
    caldav_server.report_status = 503
    result = await to_store_step(hass, STORE_CALDAV)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], caldav_input(caldav_server)
    )
    assert result["errors"] == {"base": "caldav_cannot_connect"}


async def test_caldav_url_in_use(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    caldav_server: FakeCalDav,
    caldav_entry: MockConfigEntry,
) -> None:
    caldav_entry.add_to_hass(hass)
    other = {**MAILBOX, CONF_USERNAME: "family@example.com"}
    result = await to_store_step(hass, STORE_CALDAV, other)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], caldav_input(caldav_server)
    )
    assert result["errors"] == {CONF_CALDAV_URL: "url_in_use"}


# ---- reauth --------------------------------------------------------------


async def test_reauth_ics(
    hass: HomeAssistant, setup_entry: MockConfigEntry, mailbox: FakeMailbox
) -> None:
    result = await setup_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert [str(k) for k in result["data_schema"].schema] == [CONF_PASSWORD]

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


async def test_reauth_caldav_keeps_empty_fields(
    hass: HomeAssistant,
    setup_caldav_entry: MockConfigEntry,
    caldav_server: FakeCalDav,
) -> None:
    # The CalDAV app password was revoked and replaced.
    caldav_server.password = "new-app-pw"
    result = await setup_caldav_entry.start_reauth_flow(hass)
    assert [str(k) for k in result["data_schema"].schema] == [
        CONF_PASSWORD,
        CONF_CALDAV_PASSWORD,
    ]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "caldav_invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CALDAV_PASSWORD: "new-app-pw"}
    )
    assert result["reason"] == "reauth_successful"
    assert setup_caldav_entry.data[CONF_CALDAV_PASSWORD] == "new-app-pw"
    assert setup_caldav_entry.data[CONF_PASSWORD] == "pw"
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(setup_caldav_entry.entry_id)
