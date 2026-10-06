# Changelog

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
