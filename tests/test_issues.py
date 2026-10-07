"""Repairs issues: raised after persistent failures, cleared by recovery."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar import issues
from custom_components.invite_calendar.const import CONF_ACCEPT_POLICY, DOMAIN
from custom_components.invite_calendar.mail import imap, smtp
from custom_components.invite_calendar.store import StoreError

from .conftest import FakeMailbox, Outbox
from .helpers import mail, vev

ENTITY = "calendar.tesla"


def future(days: int) -> datetime.datetime:
    return (dt_util.now() + datetime.timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    outbox: Outbox,
    mock_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    mock_config_entry.add_to_hass(hass)
    return mock_config_entry


async def start(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def issue(
    hass: HomeAssistant, entry: MockConfigEntry, key: str
) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, issues.issue_id(entry.entry_id, key)
    )


async def test_imap_unreachable_after_an_hour(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, entry)
    mailbox.fetch_error = imap.ImapConnectError("timed out")
    await poll(hass, entry)
    freezer.tick(datetime.timedelta(minutes=30))
    await poll(hass, entry)
    assert issue(hass, entry, issues.IMAP_UNREACHABLE) is None

    freezer.tick(datetime.timedelta(minutes=31))
    await poll(hass, entry)
    found = issue(hass, entry, issues.IMAP_UNREACHABLE)
    assert found is not None
    assert found.translation_placeholders["host"] == "mail.example.com"
    assert "timed out" in found.translation_placeholders["error"]

    mailbox.fetch_error = None
    await poll(hass, entry)
    assert issue(hass, entry, issues.IMAP_UNREACHABLE) is None


async def test_imap_auth_failure_is_reauth_not_an_issue(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, entry)
    mailbox.fetch_error = imap.ImapAuthError("no")
    await poll(hass, entry)
    freezer.tick(datetime.timedelta(hours=2))
    await poll(hass, entry)
    assert issue(hass, entry, issues.IMAP_UNREACHABLE) is None
    assert any(entry.async_get_active_flows(hass, {"reauth"}))


async def test_store_unreachable_after_an_hour(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, entry)
    store = entry.runtime_data.store
    with patch.object(store, "async_load", side_effect=StoreError("disk gone")):
        await poll(hass, entry)
        freezer.tick(datetime.timedelta(minutes=61))
        await poll(hass, entry)
    found = issue(hass, entry, issues.STORE_UNREACHABLE)
    assert found is not None and "disk gone" in found.translation_placeholders["error"]

    await poll(hass, entry)
    assert issue(hass, entry, issues.STORE_UNREACHABLE) is None


async def test_smtp_sender_refused_until_a_send_works(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    outbox.error = smtp.SmtpSenderRefusedError("not allowed")
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, entry)
    found = issue(hass, entry, issues.SMTP_SENDER_REFUSED)
    assert found is not None
    assert found.translation_placeholders["address"] == "tesla@example.com"

    # A refused recipient is about one message, not the setup.
    outbox.error = None
    mailbox.add(mail("REQUEST", [vev("b", future(3))], "m2"))
    await poll(hass, entry)
    assert issue(hass, entry, issues.SMTP_SENDER_REFUSED) is None


async def test_recipient_refused_is_no_issue(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "always"})
    outbox.error = smtp.SmtpRefusedError("recipient refused")
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, entry)
    assert issue(hass, entry, issues.SMTP_SENDER_REFUSED) is None
    assert issue(hass, entry, issues.SMTP_AUTH_FAILED) is None


async def test_smtp_auth_failed_from_a_service_call(
    hass: HomeAssistant, entry: MockConfigEntry, mailbox: FakeMailbox, outbox: Outbox
) -> None:
    await start(hass, entry, **{CONF_ACCEPT_POLICY: "manual"})
    mailbox.add(mail("REQUEST", [vev("a", future(2))], "m1"))
    await poll(hass, entry)
    outbox.error = smtp.SmtpAuthError("535 bad password")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "accept_event",
            {"entity_id": ENTITY, "uid": "a"},
            blocking=True,
            return_response=True,
        )
    assert issue(hass, entry, issues.SMTP_AUTH_FAILED) is not None

    # Unloading the entry takes its issues along.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert issue(hass, entry, issues.SMTP_AUTH_FAILED) is None
