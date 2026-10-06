# Invite Calendar

A Home Assistant integration that turns a mailbox into a calendar. Give the
house (or the car, or a meeting room) its own email address, invite it to
things like a person, and Home Assistant knows about them.

* Polls an IMAP mailbox for calendar invitations (iMIP REQUEST and CANCEL),
  recurring series included.
* Mirrors them into a local .ics file or a CalDAV calendar (Nextcloud).
* Exposes a calendar entity, optionally RSVPs, and lets automations create,
  move and cancel events with a proper invite email.

> **Status: pre release (0.1.0).** This version only installs and creates an
> empty entry. Mailbox polling arrives in 0.2. Not for use yet.

## Installation (HACS custom repository)

1. HACS, three dots menu, **Custom repositories**.
2. Repository `https://github.com/BarBaar44/invite-calendar`, type
   **Integration**.
3. Install **Invite Calendar**, restart Home Assistant.
4. Settings, Devices & services, **Add integration**, Invite Calendar.

## Requirements

* Home Assistant 2026.9 or newer.
* An IMAP mailbox with password or app password login (no OAuth).

## Licence

MIT
