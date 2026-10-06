"""Shared fixtures for Invite Calendar tests."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components."""
    return


@pytest.fixture(autouse=True)
async def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """A writable config dir and the Dutch time zone."""
    hass.config.config_dir = str(tmp_path)
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    return tmp_path


@dataclass
class FakeMailbox:
    """IMAP stand in: {uid: raw}, the flagged UIDs, and failure switches."""

    messages: dict[str, bytes] = field(default_factory=dict)
    flagged: set[str] = field(default_factory=set)
    fetch_error: Exception | None = None
    flag_error: Exception | None = None
    validate_error: Exception | None = None
    _next: int = 1

    def add(self, raw: bytes) -> str:
        uid = str(self._next)
        self._next += 1
        self.messages[uid] = raw
        return uid

    def fetch(self, cfg: imap.ImapSettings) -> list[tuple[str, bytes]]:
        if self.fetch_error:
            raise self.fetch_error
        return [(u, r) for u, r in self.messages.items() if u not in self.flagged]

    def mark(self, cfg: imap.ImapSettings, uids: list[str]) -> int:
        if self.flag_error:
            raise self.flag_error
        self.flagged.update(uids)
        return len(uids)

    def validate(self, cfg: imap.ImapSettings) -> None:
        if self.validate_error:
            raise self.validate_error


@pytest.fixture
def mailbox() -> Generator[FakeMailbox]:
    """Patch the IMAP functions with a fake mailbox."""
    box = FakeMailbox()
    with (
        patch.object(imap, "fetch_unprocessed", side_effect=box.fetch),
        patch.object(imap, "mark_processed", side_effect=box.mark),
        patch.object(imap, "validate", side_effect=box.validate),
    ):
        yield box


@pytest.fixture
def ics_path(config_dir: Path) -> Path:
    """Where the test entry stores its calendar."""
    return config_dir / "invite_calendar" / "tesla.ics"


@pytest.fixture
def mock_config_entry(ics_path: Path) -> MockConfigEntry:
    """A configured entry for tesla@example.com."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tesla",
        version=2,
        unique_id="tesla@example.com@mail.example.com/inbox",
        data={
            CONF_HOST: "mail.example.com",
            CONF_PORT: 993,
            CONF_USERNAME: "tesla@example.com",
            CONF_PASSWORD: "pw",
            CONF_FOLDER: "INBOX",
            CONF_PROCESSED_KEYWORD: "InviteCalendarProcessed",
            CONF_STORE_TYPE: STORE_ICS,
            CONF_ICS_PATH: str(ics_path),
        },
    )


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, mailbox: FakeMailbox, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """The entry, set up."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
