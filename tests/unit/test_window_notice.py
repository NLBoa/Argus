"""The work-window message (D061): once per window, before it starts, what to do and why."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from argus.clock import local_datetime, to_local
from argus.config import TimeWindow
from argus.domain.models import MeetingRecord, Weekday
from argus.planner.freetime import meetings_on
from argus.reminders.guards import GuardContext, Verdict, check
from argus.reminders.rules import NoticeEntry, Rule, Snapshot, WindowNotice, window_plan
from argus.reminders.text import SEGMENT, gsm_length

MON = date(2026, 10, 19)
START = local_datetime(MON, 19)
END = local_datetime(MON, 23)
QUIET = TimeWindow(start=time(23), end=time(8))
HW3 = NoticeEntry(3, "CMSC351", "Homework 3", 4.0, local_datetime(MON + timedelta(days=3), 23, 59))
PS6 = NoticeEntry(
    6,
    "MATH240",
    "Problem Set 6",
    3.0,
    local_datetime(MON + timedelta(days=1), 23, 59),
    at_risk=True,
)
STUDY = NoticeEntry(None, "CMSC351", "Midterm 1", 0.75, None)


def notice(*entries: NoticeEntry, start: datetime = START, end: datetime = END) -> WindowNotice:
    wid = f"{to_local(start).date().isoformat()}T{to_local(start):%H:%M}"
    return WindowNotice(wid, start, end, entries)


def snap(now: datetime, w: WindowNotice | None, sent: frozenset[str] = frozenset()) -> Snapshot:
    return Snapshot(now=now, items=(), sent_keys=sent, window=w)


def test_says_what_to_do_tonight_and_why() -> None:
    [r] = window_plan(snap(START - timedelta(minutes=20), notice(HW3, PS6, STUDY)))
    assert r.rule is Rule.WINDOW_PLAN
    assert r.title == "Tonight 7-11pm"
    assert r.body == (
        "[1] Homework 3, 4h left, due Thu. [2] Problem Set 6, 3h left, due tomorrow (at risk). "
        "Study 45m: Midterm 1."
    )
    assert r.item_ids == (3, 6)  # study is never numbered: DONE n must not hit an exam
    assert gsm_length(r.sms) <= SEGMENT
    assert not r.counts_toward_cap and not r.bypass_quiet
    assert r.keys == (f"window:{notice().window_id}",)


@pytest.mark.parametrize(
    ("offset", "fires"),
    [
        (timedelta(minutes=-21), False),
        (timedelta(minutes=-20), True),
        (timedelta(0), True),
        (timedelta(minutes=29), True),
        (timedelta(minutes=30), False),
    ],
)
def test_fires_from_the_lead_until_30_minutes_in(offset: timedelta, fires: bool) -> None:
    assert bool(window_plan(snap(START + offset, notice(HW3)))) is fires


def test_once_per_window() -> None:
    w = notice(HW3)
    [r] = window_plan(snap(START - timedelta(minutes=10), w))
    assert window_plan(snap(START - timedelta(minutes=5), w, frozenset(r.keys))) == []


def test_empty_window_sends_nothing() -> None:
    assert window_plan(snap(START - timedelta(minutes=10), notice())) == []
    assert window_plan(snap(START - timedelta(minutes=10), None)) == []


def test_built_entry_by_entry_to_fit_one_segment() -> None:
    many = [
        NoticeEntry(
            i, "CMSC351", f"A long assignment title number {i}", 1.5, END + timedelta(days=i)
        )
        for i in range(1, 9)
    ]
    [r] = window_plan(snap(START - timedelta(minutes=10), notice(*many)))
    assert gsm_length(r.sms) <= SEGMENT
    assert r.body.endswith("left, due " + r.body.rsplit("due ", 1)[1])  # ends on a whole entry
    assert not r.body.endswith("...")  # long titles are shortened, the message never cut
    assert r.item_ids == tuple(range(1, r.body.count("[") + 1))


def test_day_words() -> None:
    sunday = local_datetime(date(2026, 10, 25), 13)
    w = notice(HW3, start=sunday, end=sunday + timedelta(hours=5))
    [today] = window_plan(snap(sunday - timedelta(minutes=10), w))
    assert today.title == "This afternoon 1-6pm"
    late = notice(
        HW3, start=local_datetime(MON, 23, 30), end=local_datetime(MON + timedelta(days=1), 1)
    )
    [r] = window_plan(snap(local_datetime(MON, 23, 15), late))
    assert r.title == "Tonight 11:30pm-1am"


def ctx(day: date) -> GuardContext:
    lab = MeetingRecord("MATH240", "lab", (Weekday.MON,), time(18, 30), time(19, 30))
    return GuardContext(QUIET, meetings_on(day, [lab]), sent_today_capped=99, daily_cap=6)


def test_guards_class_defers_quiet_defers_cap_does_not_apply() -> None:
    [r] = window_plan(snap(START - timedelta(minutes=20), notice(HW3)))
    assert check(r, ctx(MON), START - timedelta(minutes=20)) == (Verdict.DEFER, "in class")
    assert check(r, ctx(MON + timedelta(days=1)), START)[0] is Verdict.SEND  # cap full, still sent
    assert check(r, ctx(MON), START + timedelta(minutes=31))[0] is Verdict.DROP  # expired


def test_a_window_that_starts_inside_quiet_hours_is_never_texted() -> None:
    early = notice(HW3, start=local_datetime(MON, 7, 30), end=local_datetime(MON, 9))
    for minutes in (10, 20, 40, 59):
        now = local_datetime(MON, 7, 10) + timedelta(minutes=minutes - 10)
        for r in window_plan(snap(now, early)):
            verdict, _ = check(r, ctx(date(2026, 10, 20)), now)
            assert verdict is not Verdict.SEND, now
    assert window_plan(snap(local_datetime(MON, 8), early)) == []  # 30 min in: expired


def test_snooze_defers_only_when_every_named_item_is_snoozed() -> None:
    [r] = window_plan(snap(START - timedelta(minutes=10), notice(HW3, PS6)))
    now = START - timedelta(minutes=10)
    later = now + timedelta(hours=3)
    base = ctx(MON + timedelta(days=1))
    one = GuardContext(QUIET, base.busy, 0, 6, snoozed={3: later})
    both = GuardContext(QUIET, base.busy, 0, 6, snoozed={3: later, 6: later})
    assert check(r, one, now)[0] is Verdict.SEND
    assert check(r, both, now) == (Verdict.DEFER, "snoozed")


def test_dst_fall_back_ids_differ_and_both_fire() -> None:
    sat = local_datetime(date(2026, 10, 31), 13)
    sun = local_datetime(date(2026, 11, 1), 13)
    a = notice(HW3, start=sat, end=sat + timedelta(hours=5))
    b = notice(HW3, start=sun, end=sun + timedelta(hours=5))
    assert a.window_id != b.window_id
    [ra] = window_plan(snap(sat - timedelta(minutes=20), a))
    [rb] = window_plan(snap(sun - timedelta(minutes=20), b))
    assert ra.keys != rb.keys
