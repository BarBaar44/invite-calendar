# Invite Calendar

A Home Assistant integration that turns a mailbox into a calendar. Give the
house (or the car, or a meeting room) its own email address, invite it to
things like a person, and Home Assistant knows about them.

* Polls an IMAP mailbox for calendar invitations (iMIP REQUEST and CANCEL),
  recurring series and single occurrence changes included.
* Mirrors them into a local .ics file, or into a CalDAV calendar such as
  Nextcloud, which people can keep using directly.
* Exposes them as a calendar entity.
* Optionally accepts invitations (always, once they have a location, or
  only when an automation says so) and asks organizers for a missing
  location.
* Lets automations create, move and cancel events, recurring series and
  single occurrences included, with a proper invitation email.

Works with a local .ics file or any CalDAV server (tested with Nextcloud
35 and Radicale), and with any IMAP/SMTP mail server that allows custom
IMAP keywords (Dovecot, mailcow and most others).

## Installation (HACS custom repository)

1. HACS, three dots menu, **Custom repositories**.
2. Repository `https://github.com/BarBaar44/invite-calendar`, type
   **Integration**.
3. Install **Invite Calendar**, restart Home Assistant.
4. Settings, Devices & services, **Add integration**, Invite Calendar.

Entries made with 0.1.0 can't be upgraded: delete them and add the
integration again.

## Troubleshooting

* Turn on debug logging for `custom_components.invite_calendar`, poll, and
  read the log; every invitation it applies, accepts or skips is logged.
* Settings, Devices & services, Invite Calendar, the entry, three dots,
  **Download diagnostics** gives settings and counts without passwords,
  addresses or event content, safe to attach to an issue.
* An invitation that can't be read is retried twice and then skipped with a
  notification.
* `list_events` only looks 7 days ahead unless you pass `duration` or
  `end`.

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

### Options

Settings, Devices & services, Invite Calendar, the entry, **Configure**.

| Option | Default | What it does |
|---|---|---|
| Accept invitations | Never | Never, Always, If it has a location, or Manual (only the `accept_event` action accepts, so an automation decides) |
| Ask for a missing location | off | Email the organizer of an invitation without a location, with your sentence on why it matters |
| Remove events after | 30 days (.ics), 0 (CalDAV) | 0 keeps everything; on CalDAV only events that arrived by mail are ever removed |
| Only read mail from the last | 14 days | Mail that arrived earlier is left alone (IMAP `SINCE`, whole days); 0 reads the whole folder |
| Check the mailbox every | 5 minutes | |
| Sender name, Name in replies | "<name> Calendar", "<name>" | Display names on outgoing mail |
| Outgoing mail (SMTP) | mailbox host and login, port 587 | Override host, port (465 for implicit TLS), username, password |

Replies are sent as the mailbox address, so it must be an email address.
Saving options that send mail tests the SMTP login first.

An acceptance is sent once per series (not per occurrence) and again only
when the organizer changes the event. It is recorded only after the mail
server took it, so a failed send is retried on the next poll. Events that
are already over are not answered. If the server refuses the recipient, that
version of the event is not tried again.

### The processed keyword

Processed mail gets a private IMAP keyword (default
`InviteCalendarProcessed`). The integration never marks mail as read and
never moves or deletes it, so opening a message in webmail changes nothing.
Changing the keyword later makes every message in the folder look new, so
pick it once. The lookback option limits what that would mean: only mail
from the last 14 days is read by default.

### Who may send invitations: filter on the mail server

The integration imports every invitation in its folder. To limit who can
put events in the calendar, let the mail server file allowed invitations
into a dedicated folder and point the entry at that folder. With Sieve
(mailcow: Mailbox, Filters; SOGo: Preferences, Mail, Filters):

```sieve
require ["fileinto", "mailbox", "body"];

# Invitations from these senders go to the Calendar folder.
if allof (
  anyof (
    header :contains "Content-Type" "text/calendar",
    body :raw :contains "BEGIN:VCALENDAR"
  ),
  address :is :all "from" ["partner@example.com", "boss@example.org"]
) {
  fileinto :create "Calendar";
  stop;
}
```

Use `address :domain :is "from" "example.org"` for a whole domain. The rule
checks the From address; a forged From is only stopped when the mail
server enforces DMARC for that domain (rspamd does for domains with a
reject policy). A new folder starts empty, so nothing older is imported.

This also keeps a personal INBOX out of the integration entirely.

## What it does with an invitation

| Invitation | Result |
|---|---|
| REQUEST | added, or replaces the stored copy when its SEQUENCE is not older |
| REQUEST for one occurrence | that occurrence changes, the rest of the series stays |
| CANCEL | the event or whole series is removed |
| CANCEL for one occurrence | only that occurrence disappears |
| REPLY and anything else | ignored |

Mail only changes events that arrived by mail, and only when it comes from
the same organizer:

* an event made by hand in the calendar (or by another system) is never
  changed or removed by mail, even when the UID matches;
* a REQUEST or CANCEL for a known event from a different organizer is
  ignored and logged;
* a REQUEST without an organizer is not imported;
* events the calendar organizes itself are never changed by mail.

An invitation that can't be read is retried on the next two polls, then
skipped with a notification. In an .ics file, events that ended more than
30 days ago are removed.

## Services

| Service | What it does |
|---|---|
| `invite_calendar.poll` | Check the mailbox now. |
| `invite_calendar.list_events` | Occurrences in a window (default: the next 7 days) with what `calendar.get_events` leaves out: `uid`, `recurrence_id`, `organizer`, `attendees`, `status`, `sequence`, `managed` (arrived by mail), `accepted`. Returns a response. |
| `invite_calendar.accept_event` | Accept the invitation with this `uid` now, under any policy. Does nothing when this version was already accepted. |
| `invite_calendar.create_event` | Add an event organized by this calendar and email the invitation to `attendees`. Timed (`start_date_time`, `end_date_time`, default one hour) or all day (`start_date`, `end_date`), optional `rrule` such as `FREQ=WEEKLY;COUNT=4`. Returns `uid`, `invited`, `pending`. |
| `invite_calendar.update_event` | Change an event this calendar organizes; only the fields given change. With `recurrence_id` (from `list_events`) only that occurrence changes. Moving only the start keeps the duration. Removed attendees get a cancellation. `rrule: ""` stops the repetition. |
| `invite_calendar.cancel_event` | Cancel an event this calendar organizes, or with `recurrence_id` one occurrence of it. The cancellation is sent first; if it can't be sent, nothing changes. |

All of them target the calendar entity.

### Events the calendar organizes

The calendar's own address is the organizer of what it creates, so the
mail is sent as the mailbox itself and passes sender and DMARC checks. The
people invited are attendees: they get an invitation card and can accept,
but can't change the event in their own calendar app; Home Assistant is in
charge. Mail coming back for these events (replies, forwards) never changes
them. Only events the calendar organizes can be updated or cancelled; an
invitation someone else sent stays theirs.

Every invitation carries the whole series, with a higher sequence number on
each change, and replies thread under the first invitation. If an invitation
can't be sent, the event is still saved, the response says `pending: true`,
and it is sent again on the next poll. Moving a series or stopping the
repetition drops changes made to single occurrences, as Google and Outlook
do.

```yaml
action: invite_calendar.create_event
target:
  entity_id: calendar.tesla
data:
  summary: Trip to Delft
  start_date_time: "2026-10-20 08:00:00"
  end_date_time: "2026-10-20 17:00:00"
  location: Markt 87, Delft
  attendees: [bart@example.com]
response_variable: created
```

```yaml
action: invite_calendar.list_events
target:
  entity_id: calendar.tesla
data:
  duration:
    days: 14
response_variable: trips
```

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
