"""Accept policy if_free: busy rules, first come first served, series with
declined occurrences, and the poll cycle around it."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import recurring_ical_events
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from icalendar import Calendar
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.invite_calendar import freebusy
from custom_components.invite_calendar.const import CONF_ACCEPT_POLICY
from custom_components.invite_calendar.ical import events
from custom_components.invite_calendar.mail import smtp
from custom_components.invite_calendar.state import EntryState

from .conftest import FakeMailbox, Outbox
from .helpers import mail, vev

OWN = "tesla@example.com"
WEEKLY = {"FREQ": "WEEKLY", "COUNT": 4}
NOW = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.UTC)


def at(days: int, hour: int = 19) -> datetime.datetime:
    """A local time `days` after NOW."""
    base = dt_util.as_local(NOW) + datetime.timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def calendar(*comps) -> Calendar:
    cal = events.new_calendar()
    for c in comps:
        cal.add_component(c)
    return cal


def managed(*uids: str) -> EntryState:
    return EntryState(organizer={u: "boss@ext.com" for u in uids})


def by_uid(decisions) -> dict[str, freebusy.FreeDecision]:
    return {d.uid: d for d in decisions}


# ---- what blocks -----------------------------------------------------------


def test_free_time_is_accepted() -> None:
    cal = calendar(vev("inv", at(2)))
    (d,) = freebusy.decide(cal, managed("inv"), OWN, NOW)
    assert not d.decline_whole and d.declined_occurrences == []


def test_timed_event_blocks() -> None:
    cal = calendar(vev("dentist", at(2), organizer=None), vev("inv", at(2)))
    (d,) = freebusy.decide(cal, managed("inv"), OWN, NOW)
    assert d.decline_whole


@pytest.mark.parametrize("kind", ["all_day", "transparent", "cancelled"])
def test_what_does_not_block(kind: str) -> None:
    # Built here, not in the parametrize list: at() depends on the time
    # zone, which the test fixtures only set after collection.
    if kind == "all_day":
        blocker = vev("other", at(2).date(), organizer=None)
    elif kind == "transparent":
        blocker = vev("other", at(2), organizer=None)
        blocker.add("transp", "TRANSPARENT")
    else:
        blocker = vev("other", at(2), organizer=None, status="CANCELLED")
    cal = calendar(blocker, vev("inv", at(2)))
    (d,) = freebusy.decide(cal, managed("inv"), OWN, NOW)
    assert not d.decline_whole


def test_touching_events_do_not_clash() -> None:
    before = vev("before", at(2, 18), organizer=None)  # 18:00 to 19:00
    cal = calendar(before, vev("inv", at(2, 19)))
    (d,) = freebusy.decide(cal, managed("inv"), OWN, NOW)
    assert not d.decline_whole


def test_all_day_invitation_is_accepted_unchecked() -> None:
    cal = calendar(vev("dentist", at(2), organizer=None), vev("inv", at(2).date()))
    (d,) = freebusy.decide(cal, managed("inv"), OWN, NOW)
    assert not d.decline_whole


def test_own_events_block() -> None:
    cal = calendar(vev("mine", at(2), organizer=OWN), vev("inv", at(2)))
    state = managed("inv")
    state.organizer["mine"] = OWN
    (d,) = freebusy.decide(cal, state, OWN, NOW)
    assert d.decline_whole


# ---- first come, first served ----------------------------------------------


def test_accepted_invitation_blocks_a_later_one() -> None:
    cal = calendar(vev("first", at(2)), vev("second", at(2)))
    state = managed("first", "second")
    state.accepted["first"] = 0
    (d,) = freebusy.decide(cal, state, OWN, NOW)
    assert d.uid == "second" and d.decline_whole


def test_oldest_undecided_wins() -> None:
    old = vev("old", at(2))
    new = vev("new", at(2))
    old.pop("DTSTAMP")
    old.add("dtstamp", NOW - datetime.timedelta(hours=2))
    new.pop("DTSTAMP")
    new.add("dtstamp", NOW - datetime.timedelta(hours=1))
    got = by_uid(freebusy.decide(calendar(new, old), managed("old", "new"), OWN, NOW))
    assert not got["old"].decline_whole
    assert got["new"].decline_whole


def test_declined_event_blocks_nothing() -> None:
    """a clashes with the dentist and is declined; b, at a's time but not
    the dentist's, is then free."""
    dentist = vev("dentist", at(2, 19), organizer=None)
    a = vev("a", at(2, 19), end=at(2, 21))
    b = vev("b", at(2, 20))
    a.pop("DTSTAMP")
    a.add("dtstamp", NOW - datetime.timedelta(hours=2))
    got = by_uid(freebusy.decide(calendar(dentist, a, b), managed("a", "b"), OWN, NOW))
    assert got["a"].decline_whole
    assert not got["b"].decline_whole


def test_answered_versions_are_not_decided_again() -> None:
    cal = calendar(vev("dentist", at(2), organizer=None), vev("inv", at(2)))
    state = managed("inv")
    state.declined["inv"] = {"sequence": 0, "whole": True, "until": None}
    assert freebusy.decide(cal, state, OWN, NOW) == []
    state.declined.clear()
    state.accepted["inv"] = 0
    assert freebusy.decide(cal, state, OWN, NOW) == []


def test_past_invitation_is_skipped() -> None:
    cal = calendar(vev("inv", at(-3)))
    assert freebusy.decide(cal, managed("inv"), OWN, NOW) == []


# ---- series ----------------------------------------------------------------


def test_series_declines_only_clashing_occurrences() -> None:
    dentist = vev("dentist", at(9), organizer=None)  # second Tuesday
    series = vev("football", at(2), rrule=WEEKLY)
    (d,) = freebusy.decide(calendar(dentist, series), managed("football"), OWN, NOW)
    assert not d.decline_whole
    assert [int(events.aware(r).timestamp()) for r in d.declined_occurrences] == [
        int(at(9).timestamp())
    ]


def test_series_beyond_horizon_is_not_checked() -> None:
    far = at(2) + datetime.timedelta(weeks=60)
    dentist = vev("dentist", far, organizer=None)
    series = vev("football", at(2), rrule={"FREQ": "WEEKLY"})
    (d,) = freebusy.decide(calendar(dentist, series), managed("football"), OWN, NOW)
    assert d.declined_occurrences == []


def test_apply_declines_whole_and_occurrences() -> None:
    cal = calendar(vev("single", at(2)), vev("football", at(2), rrule=WEEKLY))
    state = managed("single", "football")
    state.declined = {
        "single": {"sequence": 0, "whole": True, "until": None},
        "football": {
            "sequence": 0,
            "whole": False,
            "occurrences": [at(9).isoformat()],
            "unsent": [],
        },
    }
    assert sorted(freebusy.apply_declines(cal, state)) == ["football", "single"]
    assert events.all_uids(cal) == {"football"}
    occ = list(recurring_ical_events.of(cal).between(at(0), at(30)))
    assert len(occ) == 3 and at(9) not in [o["DTSTART"].dt for o in occ]
    assert freebusy.apply_declines(cal, state) == []  # idempotent


def test_apply_declines_skips_a_newer_version() -> None:
    cal = calendar(vev("single", at(2), seq=1))
    state = managed("single")
    state.declined = {"single": {"sequence": 0, "whole": True, "until": None}}
    assert freebusy.apply_declines(cal, state) == []
    assert events.all_uids(cal) == {"single"}


def test_state_keeps_whole_decline_until_the_event_is_over() -> None:
    state = EntryState(
        declined={
            "gone": {"sequence": 0, "whole": True, "until": NOW.timestamp() - 60},
            "soon": {"sequence": 0, "whole": True, "until": NOW.timestamp() + 60},
            "series": {"sequence": 0, "whole": False, "occurrences": []},
        }
    )
    state.prune_to(set(), NOW)
    assert set(state.declined) == {"soon"}


# ---- the poll cycle ---------------------------------------------------------


def replies(outbox: Outbox) -> list[tuple[str, str, str | None]]:
    """(UID, PARTSTAT, RECURRENCE-ID date or None) of every REPLY sent."""
    out = []
    for msg in outbox.sent:
        for part in msg.walk():
            if part.get_content_type() != "text/calendar":
                continue
            cal = Calendar.from_ical(part.get_payload(decode=True))
            for ev in cal.walk("VEVENT"):
                rid = ev.get("RECURRENCE-ID")
                out.append(
                    (
                        str(ev["UID"]),
                        str(ev["ATTENDEE"].params["PARTSTAT"]),
                        dt_util.as_local(events.aware(rid.dt)).date().isoformat()
                        if rid is not None
                        else None,
                    )
                )
    return out


def future(days: int, hour: int = 19) -> datetime.datetime:
    return (dt_util.now() + datetime.timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    mailbox: FakeMailbox,
    outbox: Outbox,
    mock_config_entry: MockConfigEntry,
    ics_path: Path,
) -> MockConfigEntry:
    """An if_free entry whose calendar already holds a hand made dentist
    appointment, five days from now at 19:00."""
    ics_path.parent.mkdir(parents=True, exist_ok=True)
    ics_path.write_bytes(calendar(vev("dentist", future(5), organizer=None)).to_ical())
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_ACCEPT_POLICY: "if_free"}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def stored(ics_path: Path) -> Calendar:
    return Calendar.from_ical(ics_path.read_bytes())


async def test_clashing_invitation_is_declined_and_left_out(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    outbox: Outbox,
    ics_path: Path,
) -> None:
    fired = []
    hass.bus.async_listen("invite_calendar_updated", fired.append)
    uid = mailbox.add(mail("REQUEST", [vev("clash", future(5))], "m1"))
    mailbox.add(mail("REQUEST", [vev("free", future(6))], "m2"))
    await poll(hass, entry)

    assert sorted(replies(outbox)) == [
        ("clash", "DECLINED", None),
        ("free", "ACCEPTED", None),
    ]
    assert uid in mailbox.flagged
    assert events.all_uids(stored(ics_path)) == {"dentist", "free"}
    assert events.all_uids(entry.runtime_data.data) == {"dentist", "free"}
    # Consumers only ever hear about the accepted one.
    assert [e.data["added"] for e in fired] == [["free"]]

    # Quiet poll and a resent copy of the same version: nothing new.
    mailbox.add(mail("REQUEST", [vev("clash", future(5))], "m3"))
    await poll(hass, entry)
    await poll(hass, entry)
    assert len(outbox.sent) == 2
    assert events.all_uids(stored(ics_path)) == {"dentist", "free"}


async def test_new_version_in_free_time_is_accepted(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    outbox: Outbox,
    ics_path: Path,
) -> None:
    mailbox.add(mail("REQUEST", [vev("clash", future(5))], "m1"))
    await poll(hass, entry)
    mailbox.add(mail("REQUEST", [vev("clash", future(5, 21), seq=1)], "m2"))
    await poll(hass, entry)
    assert replies(outbox) == [
        ("clash", "DECLINED", None),
        ("clash", "ACCEPTED", None),
    ]
    assert "clash" in events.all_uids(stored(ics_path))


async def test_series_accepted_with_one_date_declined(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    outbox: Outbox,
    ics_path: Path,
) -> None:
    """Weekly from 5 days from now: the first date clashes with the
    dentist, the other three are free."""
    mailbox.add(mail("REQUEST", [vev("football", future(5), rrule=WEEKLY)], "m1"))
    await poll(hass, entry)

    day = dt_util.as_local(future(5)).date().isoformat()
    assert replies(outbox) == [
        ("football", "ACCEPTED", None),
        ("football", "DECLINED", day),
    ]
    master = events.find_event(stored(ics_path), "football")
    assert master.get("EXDATE") is not None
    state = entry.runtime_data.state
    assert state.accepted["football"] == 0
    assert state.declined["football"]["unsent"] == []

    await poll(hass, entry)
    assert len(outbox.sent) == 2  # nothing twice


async def test_occurrence_decline_retried_after_send_failure(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    outbox: Outbox,
) -> None:
    sent_ok = 0

    def flaky(cfg, msg) -> None:
        nonlocal sent_ok
        if sent_ok >= 1:  # the accept goes out, the decline does not
            raise smtp.SmtpError("timeout")
        sent_ok += 1
        outbox.sent.append(msg)

    outbox_send = smtp.send
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(smtp, "send", flaky)
        mailbox.add(mail("REQUEST", [vev("football", future(5), rrule=WEEKLY)], "m1"))
        await poll(hass, entry)
    assert [r[1] for r in replies(outbox)] == ["ACCEPTED"]
    assert entry.runtime_data.state.declined["football"]["unsent"]

    assert smtp.send is outbox_send
    await poll(hass, entry)
    assert [r[1] for r in replies(outbox)] == ["ACCEPTED", "DECLINED"]
    assert entry.runtime_data.state.declined["football"]["unsent"] == []


async def test_transient_failure_decides_again_next_poll(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    outbox: Outbox,
    ics_path: Path,
) -> None:
    outbox.error = smtp.SmtpError("down")
    mailbox.add(mail("REQUEST", [vev("clash", future(5))], "m1"))
    await poll(hass, entry)
    assert "clash" in events.all_uids(stored(ics_path))
    assert "clash" not in entry.runtime_data.state.declined

    outbox.error = None
    await poll(hass, entry)
    assert replies(outbox) == [("clash", "DECLINED", None)]
    assert "clash" not in events.all_uids(stored(ics_path))


async def test_cancel_of_declined_invitation_forgets_it(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mailbox: FakeMailbox,
    outbox: Outbox,
) -> None:
    mailbox.add(mail("REQUEST", [vev("clash", future(5))], "m1"))
    await poll(hass, entry)
    mailbox.add(mail("CANCEL", [vev("clash", future(5), seq=1)], "m2"))
    await poll(hass, entry)
    assert "clash" not in entry.runtime_data.state.declined


def test_merge_diffs() -> None:
    from custom_components.invite_calendar.coordinator import merge_diffs
    from custom_components.invite_calendar.store import Diff

    got = merge_diffs(
        Diff(added=["a", "b"], updated=["c"], removed=[]),
        Diff(added=[], updated=["b", "c"], removed=["a"]),
    )
    assert got == Diff(added=["b"], updated=["c"], removed=[])


def test_decline_reply_names_the_occurrence() -> None:
    master = vev("football", at(2), rrule=WEEKLY)
    stub = freebusy.occurrence_stub(master, at(9).astimezone(datetime.UTC))
    msg = smtp.build_accept_reply(
        smtp.SmtpSettings("h", 587, OWN, "p"),
        "Tesla Calendar",
        OWN,
        "Tesla",
        "boss@ext.com",
        stub,
        partstat="DECLINED",
    )
    assert msg["Subject"] == "Declined: S football"
    body = next(
        p.get_payload(decode=True).decode()
        for p in msg.walk()
        if p.get_content_type() == "text/plain"
    )
    assert "has declined" in body and " on " in body
