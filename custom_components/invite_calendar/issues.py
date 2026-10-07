"""Repairs issues for problems that need the user, instead of log lines.

Raised per entry, cleared by the code path that proves the problem gone:

  imap_unreachable    the mailbox could not be read for ISSUE_AFTER; cleared
                      by the next successful fetch
  store_unreachable   the calendar (file or CalDAV) could not be read or
                      written for ISSUE_AFTER; cleared by the next good poll
  smtp_auth_failed    the SMTP server rejected the login; cleared by the next
                      message that goes out
  smtp_sender_refused the SMTP server refused to send as the calendar's own
                      address (mailcow sender check); cleared likewise

Rejected IMAP or CalDAV logins are not issues: they start HA's reauth flow,
which HA already shows. None is fixable from the issue itself: the text
says what to change (reconfigure, options, the mail server).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

IMAP_UNREACHABLE = "imap_unreachable"
STORE_UNREACHABLE = "store_unreachable"
SMTP_AUTH_FAILED = "smtp_auth_failed"
SMTP_SENDER_REFUSED = "smtp_sender_refused"
ALL = (IMAP_UNREACHABLE, STORE_UNREACHABLE, SMTP_AUTH_FAILED, SMTP_SENDER_REFUSED)


def issue_id(entry_id: str, key: str) -> str:
    """One issue per entry and kind."""
    return f"{entry_id}_{key}"


def raise_issue(
    hass: HomeAssistant, entry_id: str, key: str, placeholders: dict[str, str]
) -> None:
    """Create or update the issue (idempotent)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry_id, key),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def clear_issue(hass: HomeAssistant, entry_id: str, key: str) -> None:
    """Remove the issue if it is there."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id, key))


def clear_all(hass: HomeAssistant, entry_id: str) -> None:
    """Entry unloaded or removed: its issues go with it."""
    for key in ALL:
        clear_issue(hass, entry_id, key)
