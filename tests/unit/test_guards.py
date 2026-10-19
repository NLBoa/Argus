"""Guards across quiet hours, class time, the daily cap, and both DST changes."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from argus.config import TimeWindow
from argus.domain.models import MeetingRecord, Weekday
from argus.planner.freetime import meetings_on
from argus.reminders.guards import GuardContext, Verdict, check
from argus.reminders.rules import Reminder, Rule

QUIET = TimeWindow(start=time(23), end=time(8))
LECTURE = MeetingRecord("CMSC351", "lecture", tuple(Weekday), time(9, 30), time(10, 45))


def reminder(rule: Rule = Rule.DUE_24H, **kw: object) -> Reminder:
    urgent = rule == Rule.DUE_3H
    base: dict[str, object] = {
        "rule": rule,
        "keys": ("k",),
        "title": "t",
        "body": "b",
        "bypass_quiet": urgent,
        "counts_toward_cap": not urgent,
    }
    base.update(kw)
    return Reminder(**base)  # type: ignore[arg-type]


def ctx(
    day: date, sent: int = 0, cap: int = 6, muted: frozenset[str] = frozenset()
) -> GuardContext:
    return GuardContext(QUIET, meetings_on(day, [LECTURE]), sent, cap, muted)


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    from argus.clock import local_datetime

    return local_datetime(date(y, mo, d), h, mi)


@pytest.mark.parametrize(
    ("when", "rule", "expected"),
    [
        (local(2026, 10, 19, 12), Rule.DUE_24H, Verdict.SEND),
        (local(2026, 10, 19, 23, 30), Rule.DUE_24H, Verdict.DEFER),  # quiet
        (local(2026, 10, 19, 7, 59), Rule.DUE_24H, Verdict.DEFER),  # quiet, before 8
        (local(2026, 10, 19, 8, 0), Rule.DUE_24H, Verdict.SEND),  # quiet ended
        (local(2026, 10, 19, 23, 30), Rule.DUE_3H, Verdict.SEND),  # 3h bypasses quiet
        (local(2026, 10, 19, 10), Rule.DUE_24H, Verdict.DEFER),  # in class
        (local(2026, 10, 19, 10), Rule.DUE_3H, Verdict.SEND),  # 3h bypasses class
        (local(2026, 10, 19, 10, 45), Rule.DUE_24H, Verdict.SEND),  # class just ended
        (local(2026, 10, 19, 0, 0), Rule.SYSTEM_ALERT, Verdict.DEFER),  # midnight is quiet
    ],
)
def test_quiet_hours_and_class(when: datetime, rule: Rule, expected: Verdict) -> None:
    assert check(reminder(rule), ctx(_local_day(when)), when)[0] == expected


def _local_day(dt: datetime) -> date:
    from argus.clock import to_local

    return to_local(dt).date()


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        # Nov 1, 2026 (fall back): 07:30 EST is still quiet; 08:00 EST is not.
        (local(2026, 11, 1, 7, 30), Verdict.DEFER),
        (local(2026, 11, 1, 8, 0), Verdict.SEND),
        (datetime(2026, 11, 1, 12, 59, tzinfo=UTC), Verdict.DEFER),  # 07:59 EST
        # Mar 14, 2027 (spring forward): 08:00 EDT == 12:00 UTC.
        (datetime(2027, 3, 14, 11, 59, tzinfo=UTC), Verdict.DEFER),
        (datetime(2027, 3, 14, 12, 0, tzinfo=UTC), Verdict.SEND),
    ],
)
def test_quiet_hours_on_dst_days(when: datetime, expected: Verdict) -> None:
    c = GuardContext(QUIET, [], 0, 6)
    assert check(reminder(), c, when)[0] == expected


def test_daily_cap_excludes_three_hour_and_at_risk() -> None:
    noon = local(2026, 10, 19, 12)
    full = ctx(_local_day(noon), sent=6)
    assert check(reminder(), full, noon) == (Verdict.DEFER, "daily cap reached")
    assert check(reminder(Rule.DUE_3H), full, noon)[0] == Verdict.SEND
    at_risk = reminder(Rule.AT_RISK, counts_toward_cap=False)
    assert check(at_risk, full, noon)[0] == Verdict.SEND
    assert check(reminder(), ctx(_local_day(noon), sent=5), noon)[0] == Verdict.SEND


def test_muted_and_expired_are_dropped() -> None:
    noon = local(2026, 10, 19, 12)
    assert check(reminder(), ctx(_local_day(noon), muted=frozenset({"due_24h"})), noon) == (
        Verdict.DROP,
        "rule muted",
    )
    expired = reminder(expires_at=noon - timedelta(minutes=1))
    assert check(expired, ctx(_local_day(noon)), noon) == (Verdict.DROP, "expired")
    # Expiry is checked before quiet hours: a stale 24h reminder never waits until morning.
    night = local(2026, 10, 19, 23, 30)
    assert check(reminder(expires_at=night), ctx(_local_day(night)), night)[0] == Verdict.DROP


def test_due_during_class_three_hour_warning_still_sends() -> None:
    in_class = local(2026, 10, 19, 10)
    r = reminder(Rule.DUE_3H, expires_at=in_class + timedelta(hours=1))
    assert check(r, ctx(_local_day(in_class)), in_class)[0] == Verdict.SEND
