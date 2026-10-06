"""IMAP access. Every function here is BLOCKING: call it through
hass.async_add_executor_job.

Differences from the pyscript version, both deliberate:

* Messages are addressed by IMAP UID, not sequence number. Fetch and flag
  run in two separate sessions with the calendar save in between; a message
  deleted in webmail meanwhile shifts every sequence number after it, and
  the old code would then flag the wrong mail.
* Bodies are fetched with BODY.PEEK[] instead of RFC822, which sets \\Seen
  as a side effect. Processing is tracked with a private keyword only, so
  the poll never changes what a person sees in webmail.
"""

from __future__ import annotations

import imaplib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from homeassistant.util.ssl import client_context

TIMEOUT = 30
# One poll handles at most this many messages, oldest first; the rest wait
# for the next poll. Keeps the first run on a mailbox with history bounded.
MAX_MESSAGES_PER_POLL = 50


class ImapError(Exception):
    """Base class for IMAP failures."""


class ImapAuthError(ImapError):
    """Login rejected."""


class ImapConnectError(ImapError):
    """Server unreachable, TLS failure or protocol error."""


class ImapFolderError(ImapError):
    """The folder can't be selected."""


class ImapKeywordError(ImapError):
    """The folder doesn't allow custom keywords (flags)."""


@dataclass(frozen=True, slots=True)
class ImapSettings:
    """Connection settings for one mailbox."""

    host: str
    port: int
    username: str
    password: str
    folder: str
    keyword: str


def _quote(folder: str) -> str:
    """Quote a folder name for SELECT (spaces, quotes)."""
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@contextmanager
def _session(cfg: ImapSettings, readonly: bool) -> Iterator[imaplib.IMAP4_SSL]:
    """Logged in session with the folder selected; always logs out."""
    try:
        imap = imaplib.IMAP4_SSL(
            cfg.host, cfg.port, ssl_context=client_context(), timeout=TIMEOUT
        )
    except (TimeoutError, OSError, ssl.SSLError, imaplib.IMAP4.error) as err:
        raise ImapConnectError(f"connect {cfg.host}:{cfg.port}: {err}") from err
    try:
        try:
            imap.login(cfg.username, cfg.password)
        except imaplib.IMAP4.error as err:
            raise ImapAuthError(str(err)) from err
        try:
            status, data = imap.select(_quote(cfg.folder), readonly=readonly)
        except imaplib.IMAP4.error as err:
            raise ImapFolderError(f"{cfg.folder}: {err}") from err
        if status != "OK":
            raise ImapFolderError(f"{cfg.folder}: {data}")
        yield imap
    except (TimeoutError, OSError) as err:
        raise ImapConnectError(str(err)) from err
    except imaplib.IMAP4.abort as err:
        raise ImapConnectError(str(err)) from err
    finally:
        with suppress(Exception):  # logout failures are irrelevant
            imap.logout()


def validate(cfg: ImapSettings) -> None:
    """Log in, select the folder (read write) and check that private keywords
    can be stored. Raises an ImapError subclass describing what is wrong."""
    with _session(cfg, readonly=False) as imap:
        _status, data = imap.response("PERMANENTFLAGS")
        flags = b" ".join(d for d in data if isinstance(d, bytes))
        # Absent PERMANENTFLAGS: the server did not say, assume it works.
        if flags and b"\\*" not in flags:
            raise ImapKeywordError(flags.decode(errors="replace"))


def fetch_unprocessed(cfg: ImapSettings) -> list[tuple[str, bytes]]:
    """[(uid, raw_rfc822_bytes), ...] for messages without the processed
    keyword, oldest first, at most MAX_MESSAGES_PER_POLL."""
    messages: list[tuple[str, bytes]] = []
    with _session(cfg, readonly=True) as imap:
        try:
            status, data = imap.uid("SEARCH", None, "UNKEYWORD", cfg.keyword)
        except imaplib.IMAP4.error as err:
            raise ImapConnectError(f"search: {err}") from err
        if status != "OK" or not data or not data[0]:
            return messages
        uids = [u.decode() for u in data[0].split()]
        uids.sort(key=int)
        for uid in uids[:MAX_MESSAGES_PER_POLL]:
            try:
                status, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            except imaplib.IMAP4.error as err:
                raise ImapConnectError(f"fetch {uid}: {err}") from err
            if status != "OK" or not msg_data:
                continue
            for item in msg_data:
                if isinstance(item, tuple) and len(item) == 2:
                    messages.append((uid, item[1]))
                    break
    return messages


def mark_processed(cfg: ImapSettings, uids: list[str]) -> int:
    """Add the private keyword to messages. Deliberately not \\Seen: opening
    a message in webmail marks it read, which must not hide it."""
    if not uids:
        return 0
    with _session(cfg, readonly=False) as imap:
        try:
            status, data = imap.uid(
                "STORE", ",".join(uids), "+FLAGS", f"({cfg.keyword})"
            )
        except imaplib.IMAP4.error as err:
            raise ImapConnectError(f"store flags: {err}") from err
        if status != "OK":
            raise ImapConnectError(f"store flags: {data}")
    return len(uids)
