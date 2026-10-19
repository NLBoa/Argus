from datetime import UTC, datetime, time, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.collectors.schedule import (
    POLICIES,
    DailyLocal,
    Every,
    WeeklyLocal,
    due_sources,
    is_due,
    next_slot,
)
from argus.domain.models import Source, Weekday


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_every_slots_are_aligned_with_offset() -> None:
    p = Every(timedelta(minutes=30), timedelta(minutes=6))
    assert p.last_slot(utc(2026, 10, 19, 13, 5)) == utc(2026, 10, 19, 12, 36)
    assert p.last_slot(utc(2026, 10, 19, 13, 6)) == utc(2026, 10, 19, 13, 6)
    assert p.last_slot(utc(2026, 10, 19, 13, 35, 59)) == utc(2026, 10, 19, 13, 6)


def test_sources_are_staggered() -> None:
    now = utc(2026, 10, 19, 13, 5)
    last = {s: utc(2026, 10, 19, 12, 55) for s in POLICIES}
    # Only the iCal slot (13:00) has passed since 12:55; the others' slots were earlier.
    assert due_sources(now, last, POLICIES) == [Source.CANVAS_ICAL]
    later = utc(2026, 10, 19, 13, 13)
    assert set(due_sources(later, last, POLICIES)) == {
        Source.CANVAS_ICAL,
        Source.GRADESCOPE,
        Source.PIAZZA,
    }


def test_never_attempted_is_due_and_unknown_sources_ignored() -> None:
    now = utc(2026, 10, 19, 13)
    assert due_sources(now, {}, [Source.CANVAS_ICAL, Source.MANUAL]) == [Source.CANVAS_ICAL]


@given(st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2028, 1, 1)))  # noqa: DTZ001
def test_at_most_one_poll_per_slot(naive: datetime) -> None:
    now = naive.replace(tzinfo=UTC)
    for policy in POLICIES.values():
        slot = policy.last_slot(now)
        assert slot <= now
        assert not is_due(policy, now, slot)  # attempted in this slot: not due again
        assert is_due(policy, now, slot - timedelta(seconds=1))


def test_weekly_umdio_is_sunday_4am_local_across_fall_back() -> None:
    p = POLICIES[Source.UMDIO]
    # Sun Nov 1, 2026 is the fall-back day: 04:00 EST == 09:00 UTC.
    assert p.last_slot(utc(2026, 11, 1, 9, 0)) == utc(2026, 11, 1, 9, 0)
    assert p.last_slot(utc(2026, 11, 1, 8, 59)) == utc(2026, 10, 25, 8, 0)  # prev Sun, EDT


def test_weekly_across_spring_forward() -> None:
    p = WeeklyLocal(Weekday.SUN, time(4, 0))
    # Sun Mar 14, 2027: 04:00 EDT == 08:00 UTC; the week before was EST (09:00 UTC).
    assert p.last_slot(utc(2027, 3, 14, 8, 0)) == utc(2027, 3, 14, 8, 0)
    assert p.last_slot(utc(2027, 3, 14, 7, 59)) == utc(2027, 3, 7, 9, 0)


def test_daily_local_in_gap_hour() -> None:
    p = DailyLocal(time(2, 30))  # does not exist on Mar 14, 2027: maps to 03:30 EDT
    assert p.last_slot(utc(2027, 3, 14, 12)) == utc(2027, 3, 14, 7, 30)
    assert p.last_slot(utc(2027, 3, 14, 7, 0)) == utc(2027, 3, 13, 7, 30)


@pytest.mark.parametrize(
    ("policy", "now", "expected"),
    [
        (Every(timedelta(minutes=30)), utc(2026, 10, 19, 13, 5), utc(2026, 10, 19, 13, 30)),
        (DailyLocal(time(3)), utc(2026, 10, 19, 13), utc(2026, 10, 20, 7)),
    ],
)
def test_next_slot(policy: Every | DailyLocal, now: datetime, expected: datetime) -> None:
    assert next_slot(policy, now) == expected
