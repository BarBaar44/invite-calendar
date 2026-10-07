# Changelog

## 1.2.0 (2026-10-07)

* New action `decline_event`: decline an invitation, or with
  `recurrence_id` one occurrence of a series, under any accept policy. The
  organizer gets a "declined" reply and the event (or that date) is left
  out of the calendar. It also works on an invitation that was accepted
  before. A resent copy of the same version stays out; a new version from
  the organizer is shown again and waits for a new answer. If the reply
  can't be sent, nothing changes and the action fails.
* The Manual accept policy now means: only `accept_event` and
  `decline_event` answer.

## 1.1.1 (2026-10-06)

* CalDAV: correcting the username of a Nextcloud calendar no longer keeps
  failing with HTTP 404. CalDAV requests went through Home Assistant's
  shared web session, which kept Nextcloud's session cookies; Nextcloud
  then kept serving every later request as the first user, whatever
  username and app password were sent. CalDAV now uses its own session
  without cookies, so each request is checked against its own login. TLS
  verification is unchanged. A restart is no longer needed to recover from
  a wrong username.

## 1.1.0

* New accept policy "If the time is free": accept an invitation when
  nothing else takes that time, decline it when something does. Only
  timed events block (not all day, free or cancelled ones), first come
  first served. A declined event is left out of the calendar. For a
  recurring series only the clashing dates in the coming year are
  declined, each with its own reply, and left out.
* Declined replies that could not be sent are retried every poll.
* Diagnostics count declined invitations and unsent declines.

## 1.0.1 (2026-10-06)

Security and robustness fixes. No configuration changes needed.

* Mail can no longer change or remove events that did not arrive by mail.
  A CANCEL or REQUEST with the UID of an event made by hand (for example
  in Nextcloud) used to remove or overwrite it.
* A CANCEL or REQUEST for an invitation is only applied when it comes from
  the organizer that sent the invitation; others are ignored and logged.
* A REQUEST without ORGANIZER is no longer imported.
* New option "Only read mail from the last (days)", default 14 (IMAP
  `SINCE`). Pointing an entry at a folder with history no longer imports
  every invitation ever received. Existing entries get 14 days too; set 0
  to read the whole folder as before.
* Invitations that could not be sent are now still resent after a restart
  (the pending list was saved but not read back).
* README: filtering who may send invitations with a Sieve rule.

Note for an .ics file carried over from another tool: events already in it
did not arrive through this integration, so mail no longer changes them.

## 1.0.0 (2026-10-06)

First stable release.

* Diagnostics, with credentials, addresses, hosts, paths and event content
  redacted.
* Brand icon shipped with the integration (Home Assistant 2026.3 and later
  show it without an entry in the brands repository).
* Tests check that the English and Dutch translations are complete.

## 0.5.0

* `create_event`, `update_event`, `cancel_event`: events organized by the
  calendar itself, with an invitation email to the attendees (text plus
  .ics attachment). Recurring series, and changing or cancelling a single
  occurrence.
* Times stored in Home Assistant's time zone, so a series keeps its wall
  clock time across a DST change.
* A failed invitation leaves the event saved and pending; the next poll
  sends it. A cancellation is sent before the calendar changes.
* Mail never changes events the calendar organizes.
* `list_events` gains `own`.

## 0.4.0

* Options: accept policy (never, always, if it has a location, manual),
  missing location reply, retention, poll interval, display names, SMTP
  override.
* Acceptances once per series and version, recorded only after a
  confirmed send.
* `accept_event` and `list_events` actions.

## 0.3.0

* CalDAV store (Nextcloud, SOGo, Radicale): only changed events are
  written, with ETags, so edits made in the calendar at the same moment
  are never overwritten. Events made by hand are never touched.
* The `caldav` library is no longer used.

## 0.2.0

* Reads calendar invitations (REQUEST, CANCEL, recurring series and single
  occurrences) from an IMAP mailbox into a local .ics file.
* Calendar entity, `poll` action, `invite_calendar_updated` and
  `invite_calendar_invite_received` events, reauthentication.

## 0.1.0

* Skeleton: installs through HACS, loads and unloads.
