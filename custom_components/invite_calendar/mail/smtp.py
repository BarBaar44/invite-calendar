"""Outbound mail (ported from pyscript imip_mail). BLOCKING: run every send
through hass.async_add_executor_job.

  build_invite()                 iMIP REQUEST / CANCEL for events this
                                 calendar organizes
  build_accept_reply()           iMIP REPLY, PARTSTAT=ACCEPTED
  build_missing_location_reply() plain threaded reply asking for a LOCATION

SENDER IDENTITY. RFC 6047: From: must be the ATTENDEE for a REPLY. The
calendar's own address is the IMAP username and SMTP defaults to the same
login, so From equals the login, mailcow's sender check passes and DMARC
aligns. When the SMTP login differs, a Sender: header is added; mailcow
rejects that unless the login may send as the calendar address.

MESSAGE STRUCTURE. REQUEST/CANCEL is text/plain plus an application/ics
attachment, with NO inline text/calendar part: Gmail on an IMAP account
printed the raw VCALENDAR into the body and never shows an invite card for
non Google accounts anyway. A REPLY keeps an inline text/calendar part (its
reader is a calendar server) inside multipart/alternative with a text part.

TRANSPORT. Port 465 uses implicit TLS, anything else STARTTLS (587 by
default). A send is tried twice for transient errors; a refused sender or
recipient is permanent and raises SmtpRefusedError immediately.
"""

from __future__ import annotations

import datetime
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email import encoders
from email.header import decode_header, make_header
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import client_context
from icalendar import Calendar, Event, vCalAddress, vText

from ..ical import events

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 20
ATTEMPTS = 2


class SmtpError(Exception):
    """A send failed (possibly transient: retried next time)."""


class SmtpAuthError(SmtpError):
    """Login rejected."""


class SmtpRefusedError(SmtpError):
    """Sender or recipient refused: retrying the same message won't help."""


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """Where and as whom to send."""

    host: str
    port: int
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class OriginalMessage:
    """What a threaded reply needs from the message it answers."""

    message_id: str | None
    subject: str | None
    references: str | None


def _connect(cfg: SmtpSettings) -> smtplib.SMTP:
    """Connected, encrypted and logged in."""
    try:
        if cfg.port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=TIMEOUT, context=client_context()
            )
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT)
            server.starttls(context=client_context())
    except (TimeoutError, OSError, ssl.SSLError, smtplib.SMTPException) as err:
        raise SmtpError(f"connect {cfg.host}:{cfg.port}: {err}") from err
    try:
        server.login(cfg.username, cfg.password)
    except smtplib.SMTPAuthenticationError as err:
        _quit(server)
        raise SmtpAuthError(str(err)) from err
    except (OSError, smtplib.SMTPException) as err:
        _quit(server)
        raise SmtpError(f"login: {err}") from err
    return server


def _quit(server: smtplib.SMTP) -> None:
    try:
        server.quit()
    except Exception:  # noqa: BLE001 - closing a dead connection
        server.close()


def validate(cfg: SmtpSettings) -> None:
    """Connect and log in. Raises SmtpAuthError or SmtpError."""
    _quit(_connect(cfg))


def send(cfg: SmtpSettings, msg: Message) -> None:
    """Send with one retry for transient failures."""
    last: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            server = _connect(cfg)
        except SmtpAuthError:
            raise
        except SmtpError as err:
            last = err
            continue
        try:
            server.send_message(msg)
            return
        except smtplib.SMTPSenderRefused as err:
            raise SmtpRefusedError(
                f"sender {msg.get('From')} refused by {cfg.host}; is "
                f"{cfg.username} allowed to send as that address? {err}"
            ) from err
        except smtplib.SMTPRecipientsRefused as err:
            raise SmtpRefusedError(f"recipient refused: {err}") from err
        except (OSError, smtplib.SMTPException) as err:
            last = err
        finally:
            _quit(server)
    raise SmtpError(f"send failed after {ATTEMPTS} attempts: {last}")


def _headers(
    msg: Message, cfg: SmtpSettings, from_name: str, from_addr: str, to_addr: str
) -> None:
    msg["From"] = formataddr((from_name, from_addr))
    if from_addr.lower() != cfg.username.lower():
        msg["Sender"] = cfg.username
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()


def _thread(msg: Message, original: OriginalMessage | None) -> None:
    if original is None or not original.message_id:
        return
    msg["In-Reply-To"] = original.message_id
    refs = f"{original.references or ''} {original.message_id}".strip()
    msg["References"] = refs


def decoded_subject(raw: str | None) -> str | None:
    """An RFC 2047 encoded subject as plain text."""
    if not raw:
        return None
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 - keep the raw header then
        return raw


def build_accept_reply(
    cfg: SmtpSettings,
    from_name: str,
    attendee_addr: str,
    attendee_cn: str,
    organizer_addr: str,
    source: Event,
    prodid: str = events.PRODID,
    original: OriginalMessage | None = None,
    partstat: str = "ACCEPTED",
) -> Message:
    """A minimal REPLY per RFC 5546: same UID and SEQUENCE, DTSTAMP, the
    original ORGANIZER and exactly ONE ATTENDEE, ours. The event itself is
    deliberately not echoed back. `partstat` ACCEPTED or DECLINED; with a
    RECURRENCE-ID on `source` it answers that one occurrence."""
    reply = Event()
    reply.add("uid", str(source.get("UID")))
    reply.add("dtstamp", dt_util.utcnow())
    reply.add("sequence", int(source.get("SEQUENCE", 0)))
    if (organizer := source.get("ORGANIZER")) is not None:
        reply.add("organizer", organizer)
    if (rid := source.get("RECURRENCE-ID")) is not None:
        reply.add("recurrence-id", rid.dt)
    if (dtstart := source.get("DTSTART")) is not None:
        reply.add("dtstart", dtstart.dt)
    if (summary := source.get("SUMMARY")) is not None:
        reply.add("summary", str(summary))
    attendee = vCalAddress(f"mailto:{attendee_addr}")
    attendee.params["CN"] = vText(attendee_cn)
    attendee.params["PARTSTAT"] = vText(partstat)
    attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
    reply.add("attendee", attendee, encode=0)

    cal = Calendar()
    cal.add("prodid", prodid)
    cal.add("version", "2.0")
    cal.add("method", "REPLY")
    cal.add("calscale", "GREGORIAN")
    cal.add_component(reply)
    try:
        cal.add_missing_timezones()
    except Exception:  # noqa: BLE001 - a missing VTIMEZONE is not fatal
        _LOGGER.debug("Could not add missing timezones", exc_info=True)

    event_summary = str(source.get("SUMMARY", "your invitation"))
    verb, label = (
        ("declined", "Declined") if partstat == "DECLINED" else ("accepted", "Accepted")
    )
    what = f'"{event_summary}"'
    if rid is not None:
        what += f" on {human_start(rid.dt)}"
    body = f"{attendee_cn} has {verb} {what}.\n\n{from_name} (automated)"
    outer = MIMEMultipart("mixed")
    outer["Subject"] = f"{label}: {event_summary}"
    _headers(outer, cfg, from_name, attendee_addr, organizer_addr)
    outer["Reply-To"] = attendee_addr
    _thread(outer, original)

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(body, "plain", "utf-8"))
    cal_part = MIMEText(cal.to_ical().decode("utf-8"), "calendar", "utf-8")
    cal_part.set_param("method", "REPLY")
    cal_part.set_param("component", "VEVENT")
    alternative.attach(cal_part)
    outer.attach(alternative)
    return outer


def build_invite(
    cfg: SmtpSettings,
    from_name: str,
    organizer_addr: str,
    to_addrs: list[str],
    components: list[Event],
    method: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    prodid: str = events.PRODID,
) -> Message:
    """An iMIP REQUEST or CANCEL for `components` (one UID: a master and
    its overrides, or a single instance). The caller has set DTSTAMP and,
    for updates and cancellations, a higher SEQUENCE."""
    cal = Calendar()
    cal.add("prodid", prodid)
    cal.add("version", "2.0")
    cal.add("method", method)
    cal.add("calscale", "GREGORIAN")
    for component in components:
        cal.add_component(component)
    try:
        cal.add_missing_timezones()
    except Exception:  # noqa: BLE001 - a missing VTIMEZONE is not fatal
        _LOGGER.debug("Could not add missing timezones", exc_info=True)

    outer = MIMEMultipart("mixed")
    outer["Subject"] = subject
    _headers(outer, cfg, from_name, organizer_addr, ", ".join(to_addrs))
    outer["Reply-To"] = organizer_addr
    if in_reply_to:
        outer["In-Reply-To"] = in_reply_to
        outer["References"] = in_reply_to
    outer.attach(MIMEText(body, "plain", "utf-8"))

    name = "cancel.ics" if method == "CANCEL" else "invite.ics"
    attachment = MIMEBase("application", "ics", name=name)
    attachment.set_payload(cal.to_ical())
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", "attachment", filename=name)
    outer.attach(attachment)
    return outer


def build_missing_location_reply(
    cfg: SmtpSettings,
    from_name: str,
    from_addr: str,
    to_addr: str,
    original: OriginalMessage,
    event_summary: str,
    event_start: str,
    purpose: str,
) -> Message:
    """A threaded plain reply asking the organizer to add a location."""
    subject = decoded_subject(original.subject) or "your event"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    body = (
        f'Hi,\n\nThanks for the invite to "{event_summary}" on {event_start}.\n\n'
        f"{purpose} Could you add a location to the event and send it again?\n\n"
        f"{from_name} (automated)"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    _headers(msg, cfg, from_name, from_addr, to_addr)
    # RFC 3834: machine generated, so autoresponders don't ping pong.
    msg["Auto-Submitted"] = "auto-replied"
    _thread(msg, original)
    return msg


def human_start(value: datetime.date | datetime.datetime | None) -> str:
    """'Tuesday 20 October 2026, 09:00' in local time, or a fallback."""
    if value is None:
        return "an unspecified time"
    if isinstance(value, datetime.datetime):
        return (
            events.aware(value)
            .astimezone(dt_util.get_default_time_zone())
            .strftime("%A %d %B %Y, %H:%M")
        )
    return value.strftime("%A %d %B %Y")
