# Invite Calendar

A Home Assistant integration that turns a mailbox into a calendar. Give the
house (or the car, or a meeting room) its own email address, invite it to
things like a person, and Home Assistant knows about them.

* Polls an IMAP mailbox for calendar invitations (iMIP REQUEST and CANCEL),
  recurring series and single occurrence changes included.
* Mirrors them into a local .ics file, or into a CalDAV calendar such as
  Nextcloud, which people can keep using directly.
* Exposes them as a calendar entity.

> **Status: pre release (0.3.0).** Inbound invitations into a local .ics
> file or a CalDAV calendar work. RSVPs and outbound invites are not there
> yet.

## Installation (HACS custom repository)

1. HACS, three dots menu, **Custom repositories**.
2. Repository `https://github.com/BarBaar44/invite-calendar`, type
   **Integration**.
3. Install **Invite Calendar**, restart Home Assistant.
4. Settings, Devices & services, **Add integration**, Invite Calendar.

Entries made with 0.1.0 can't be upgraded: delete them and add the
integration again.

## Setup

1. **Mailbox**: IMAP server (implicit TLS, port 993), username, password
   (use an app password where your provider offers one), folder, and the
   processed keyword.
2. **Store**: a local .ics file or a CalDAV calendar.
3. **Calendar**, for an .ics file: name, and the file inside the
   configuration folder. Default `/config/invite_calendar/<mailbox>.ics`. An
   existing file is used as is.
4. **Calendar**, for CalDAV: name, collection URL, username and an app
   password. For Nextcloud the URL is
   `https://<host>/remote.php/dav/calendars/<user>/<calendar>/` (Calendar
   app, calendar menu, Copy private link); create the app password under
   Personal settings, Security, Devices & sessions.

### A shared CalDAV calendar

The calendar can be one people use directly. The integration:

* shows every event in it, including ones made by hand;
* only ever writes events that arrived by mail, and writes them with
  `If-Match`, so an edit made in Nextcloud at the same moment is never
  overwritten (the poll retries instead);
* never removes old events from it (retention is off for CalDAV).

### The processed keyword

Processed mail gets a private IMAP keyword (default
`InviteCalendarProcessed`). The integration never marks mail as read and
never moves or deletes it, so opening a message in webmail changes nothing.
Changing the keyword later makes every message in the folder look new, so
pick it once.

## What it does with an invitation

| Invitation | Result |
|---|---|
| REQUEST | added, or replaces the stored copy when its SEQUENCE is not older |
| REQUEST for one occurrence | that occurrence changes, the rest of the series stays |
| CANCEL | the event or whole series is removed |
| CANCEL for one occurrence | only that occurrence disappears |
| REPLY and anything else | ignored |

An invitation that can't be read is retried on the next two polls, then
skipped with a notification. In an .ics file, events that ended more than
30 days ago are removed.

## Services

| Service | What it does |
|---|---|
| `invite_calendar.poll` | Check the mailbox now. Target the calendar entity. |

## Events

* `invite_calendar_updated`: `entity_id`, `added`, `updated`, `removed`
  (UIDs), after every change to the calendar.
* `invite_calendar_invite_received`: `entity_id`, `uid`, `organizer`,
  `summary`, `start`, `location`, once per new or changed invitation.

## Requirements

* Home Assistant 2026.9 or newer.
* An IMAP mailbox with password or app password login (no OAuth) whose
  server allows custom keywords (Dovecot, mailcow, Fastmail and most others
  do; setup checks it).
* For CalDAV: a server that supports calendar-query REPORT and ETags
  (Nextcloud, SOGo, Radicale, Baïkal). Automated tests run against
  Radicale 3; Nextcloud 35 is the reference server.

## Licence

MIT
