"""Outbound mail (mail/smtp.py): message structure and transport."""

from __future__ import annotations

import datetime
import smtplib
from unittest.mock import patch

import pytest
from icalendar import Calendar

from custom_components.invite_calendar.mail import smtp

from .helpers import TZ, vev

CFG = smtp.SmtpSettings("mail.example.com", 587, "tesla@example.com", "pw")
T0 = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=TZ)


def calendar_part(msg) -> Calendar:
    for part in msg.walk():
        if part.get_content_type() == "text/calendar":
            return Calendar.from_ical(part.get_payload(decode=True))
    raise AssertionError("no text/calendar part")


def test_accept_reply_structure() -> None:
    source = vev("u1", T0, seq=3, summary="Dentist")
    original = smtp.OriginalMessage("<m1@ext.com>", "Invitation", "<m0@ext.com>")
    msg = smtp.build_accept_reply(
        CFG,
        "Tesla Calendar",
        "tesla@example.com",
        "Tesla",
        "boss@ext.com",
        source,
        original=original,
    )
    assert msg["From"] == "Tesla Calendar <tesla@example.com>"
    assert msg["Sender"] is None  # From equals the SMTP login
    assert msg["To"] == "boss@ext.com"
    assert msg["Subject"] == "Accepted: Dentist"
    assert msg["In-Reply-To"] == "<m1@ext.com>"
    assert msg["References"] == "<m0@ext.com> <m1@ext.com>"

    cal_part = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    assert cal_part.get_param("method") == "REPLY"
    cal = calendar_part(msg)
    assert str(cal["METHOD"]) == "REPLY"
    (event,) = cal.walk("VEVENT")
    assert str(event["UID"]) == "u1" and int(event["SEQUENCE"]) == 3
    assert event.get("DTSTAMP") is not None
    attendee = event["ATTENDEE"]
    assert not isinstance(attendee, list)  # exactly one: ours
    assert str(attendee).lower() == "mailto:tesla@example.com"
    assert attendee.params["PARTSTAT"] == "ACCEPTED"
    assert event.get("LOCATION") is None  # not echoed back


def test_accept_reply_for_lone_override_keeps_recurrence_id() -> None:
    override = vev("u1", T0, rid=T0)
    msg = smtp.build_accept_reply(
        CFG, "T", "tesla@example.com", "T", "boss@ext.com", override
    )
    (event,) = calendar_part(msg).walk("VEVENT")
    assert event.get("RECURRENCE-ID") is not None


def test_sender_header_when_login_differs() -> None:
    relay = smtp.SmtpSettings("relay", 587, "ha@example.com", "pw")
    msg = smtp.build_accept_reply(
        relay, "T", "tesla@example.com", "T", "boss@ext.com", vev("u1", T0)
    )
    assert msg["Sender"] == "ha@example.com"


def test_missing_location_reply() -> None:
    original = smtp.OriginalMessage(
        "<m1@ext.com>", "=?utf-8?q?Vergadering_=C3=A9n_lunch?=", None
    )
    msg = smtp.build_missing_location_reply(
        CFG,
        "Tesla Calendar",
        "tesla@example.com",
        "boss@ext.com",
        original,
        "Lunch",
        "Tuesday 20 October 2026, 09:00",
        "The car needs to know where.",
    )
    assert msg["Subject"] == "Re: Vergadering én lunch"
    assert msg["Auto-Submitted"] == "auto-replied"
    assert msg["In-Reply-To"] == "<m1@ext.com>"
    body = msg.get_payload(decode=True).decode()
    assert "The car needs to know where." in body and '"Lunch"' in body


def test_human_start() -> None:
    assert smtp.human_start(None) == "an unspecified time"
    assert smtp.human_start(datetime.date(2026, 10, 20)) == "Tuesday 20 October 2026"


class FakeServer:
    """smtplib.SMTP / SMTP_SSL stand in."""

    instances: list[FakeServer] = []
    behaviour: list[str] = []  # per connection: "ok", "drop", "refuse_rcpt", ...

    def __init__(self, host, port, timeout=None, context=None):
        self.port = port
        self.tls = False
        self.sent = []
        FakeServer.instances.append(self)

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        if password != "pw":
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        what = FakeServer.behaviour.pop(0) if FakeServer.behaviour else "ok"
        if what == "drop":
            raise smtplib.SMTPServerDisconnected("gone")
        if what == "refuse_rcpt":
            raise smtplib.SMTPRecipientsRefused({"x@y": (550, b"no")})
        if what == "refuse_sender":
            raise smtplib.SMTPSenderRefused(553, b"not yours", "tesla@example.com")
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_smtp():
    FakeServer.instances = []
    FakeServer.behaviour = []
    with (
        patch.object(smtp.smtplib, "SMTP", FakeServer),
        patch.object(smtp.smtplib, "SMTP_SSL", FakeServer),
    ):
        yield


def _msg():
    return smtp.build_accept_reply(
        CFG, "T", "tesla@example.com", "T", "boss@ext.com", vev("u1", T0)
    )


def test_send_starttls_on_587() -> None:
    smtp.send(CFG, _msg())
    (server,) = FakeServer.instances
    assert server.tls and len(server.sent) == 1


def test_send_implicit_tls_on_465() -> None:
    smtp.send(smtp.SmtpSettings("h", 465, "tesla@example.com", "pw"), _msg())
    assert not FakeServer.instances[0].tls  # SMTP_SSL: no STARTTLS


def test_send_retries_transient_once() -> None:
    FakeServer.behaviour = ["drop", "ok"]
    smtp.send(CFG, _msg())
    assert len(FakeServer.instances) == 2

    FakeServer.behaviour = ["drop", "drop"]
    with pytest.raises(smtp.SmtpError, match="2 attempts"):
        smtp.send(CFG, _msg())


@pytest.mark.parametrize("what", ["refuse_rcpt", "refuse_sender"])
def test_refusal_is_permanent(what: str) -> None:
    FakeServer.behaviour = [what]
    with pytest.raises(smtp.SmtpRefusedError):
        smtp.send(CFG, _msg())
    assert len(FakeServer.instances) == 1  # not retried


def test_auth_error() -> None:
    with pytest.raises(smtp.SmtpAuthError):
        smtp.validate(smtp.SmtpSettings("h", 587, "u", "wrong"))
    smtp.validate(CFG)
