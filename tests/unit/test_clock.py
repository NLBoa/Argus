from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from argus.clock import (
    LOCAL_TZ,
    FakeClock,
    SystemClock,
    ensure_utc,
    local_datetime,
    to_local,
    today_local,
)


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC


def test_fake_clock_advance_and_set() -> None:
    c = FakeClock(datetime(2026, 10, 19, 13, tzinfo=UTC))
    c.advance(timedelta(hours=2))
    assert c.now() == datetime(2026, 10, 19, 15, tzinfo=UTC)
    c.set(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-5))))
    assert c.now() == datetime(2026, 1, 1, 5, tzinfo=UTC)


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="naive"):
        FakeClock(datetime(2026, 1, 1))  # noqa: DTZ001


def test_to_local_and_today_local_around_midnight() -> None:
    # 03:30 UTC on Oct 20 is still Oct 19 in New York (EDT, UTC-4).
    c = FakeClock(datetime(2026, 10, 20, 3, 30, tzinfo=UTC))
    assert today_local(c) == date(2026, 10, 19)
    assert to_local(c.now()).tzinfo == LOCAL_TZ


def test_local_datetime_standard_and_daylight() -> None:
    assert local_datetime(date(2026, 10, 19), 9) == datetime(2026, 10, 19, 13, tzinfo=UTC)
    assert local_datetime(date(2026, 12, 1), 9) == datetime(2026, 12, 1, 14, tzinfo=UTC)


def test_local_datetime_fall_back_ambiguous_hour_uses_first_occurrence() -> None:
    # Nov 1, 2026: 01:30 happens twice; the first is EDT (UTC-4).
    assert local_datetime(date(2026, 11, 1), 1, 30) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_local_datetime_spring_forward_gap_moves_forward() -> None:
    # Mar 14, 2027: 02:30 does not exist; it maps to 03:30 EDT == 07:30 UTC.
    result = local_datetime(date(2027, 3, 14), 2, 30)
    assert result == datetime(2027, 3, 14, 7, 30, tzinfo=UTC)
    assert to_local(result).hour == 3
