"""IMAP functions against a fake server (mail/imap.py)."""

from __future__ import annotations

import imaplib
from dataclasses import replace
from unittest.mock import patch

import pytest

from custom_components.invite_calendar.mail import imap

CFG = imap.ImapSettings(
    host="mail.example.com",
    port=993,
    username="cal@example.com",
    password="pw",
    folder="INBOX",
    keyword="InviteCalendarProcessed",
)


class FakeIMAP:
    """Just enough of IMAP4_SSL. Messages: {uid: (raw, set(flags))}."""

    instances: list[FakeIMAP] = []
    messages: dict[str, tuple[bytes, set[str]]] = {}
    password = "pw"
    permanentflags: bytes | None = b"(\\Seen \\Deleted \\*)"
    folders = {"INBOX"}

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.calls: list[tuple] = []
        self.selected_readonly: bool | None = None
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Authentication failed.")
        return "OK", [b"Logged in"]

    def select(self, mailbox, readonly=False):
        self.selected_readonly = readonly
        if mailbox.strip('"') not in self.folders:
            return "NO", [b"Mailbox doesn't exist"]
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        if code == "PERMANENTFLAGS" and self.permanentflags is not None:
            return code, [self.permanentflags]
        return code, [None]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            keyword = args[-1]
            uids = [u for u, (_, f) in self.messages.items() if keyword not in f]
            return "OK", [" ".join(uids).encode()]
        if command == "FETCH":
            uid, what = args
            raw, _ = self.messages[uid]
            return "OK", [
                (f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw),
                b")",
            ]
        if command == "STORE":
            uids, _op, flags = args
            for u in uids.split(","):
                self.messages[u][1].add(flags.strip("()"))
            return "OK", [b"done"]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def fake_imap():
    FakeIMAP.instances = []
    FakeIMAP.messages = {
        "7": (b"seven", set()),
        "12": (b"twelve", {"InviteCalendarProcessed"}),
        "9": (b"nine", set()),
    }
    FakeIMAP.permanentflags = b"(\\Seen \\Deleted \\*)"
    with patch.object(imap.imaplib, "IMAP4_SSL", FakeIMAP):
        yield


def test_fetch_uses_uids_peek_and_readonly() -> None:
    msgs = imap.fetch_unprocessed(CFG)
    assert msgs == [("7", b"seven"), ("9", b"nine")]
    session = FakeIMAP.instances[0]
    assert session.selected_readonly is True
    assert all(c[0] in ("SEARCH", "FETCH") for c in session.calls)
    assert all(c[2] == "(BODY.PEEK[])" for c in session.calls if c[0] == "FETCH")
    assert session.logged_out


def test_fetch_is_capped_oldest_first() -> None:
    FakeIMAP.messages = {str(u): (b"x", set()) for u in range(100, 0, -1)}
    msgs = imap.fetch_unprocessed(CFG)
    assert len(msgs) == imap.MAX_MESSAGES_PER_POLL
    assert msgs[0][0] == "1"


def test_mark_processed_by_uid() -> None:
    assert imap.mark_processed(CFG, ["7", "9"]) == 2
    assert FakeIMAP.instances[0].calls == [
        ("STORE", "7,9", "+FLAGS", "(InviteCalendarProcessed)")
    ]
    assert imap.fetch_unprocessed(CFG) == []


def test_validate_errors() -> None:
    imap.validate(CFG)
    with pytest.raises(imap.ImapAuthError):
        imap.validate(replace(CFG, password="wrong"))
    with pytest.raises(imap.ImapFolderError):
        imap.validate(replace(CFG, folder="Nope"))
    FakeIMAP.permanentflags = b"(\\Seen \\Deleted)"
    with pytest.raises(imap.ImapKeywordError):
        imap.validate(CFG)


def test_connect_error() -> None:
    with (
        patch.object(imap.imaplib, "IMAP4_SSL", side_effect=OSError("refused")),
        pytest.raises(imap.ImapConnectError),
    ):
        imap.fetch_unprocessed(CFG)
