from datetime import UTC, date, datetime, timedelta

import pytest

from argus.web.format import clock_time, day_label, due_text, elapsed, hours, long_date, relative

NOW = datetime(2026, 10, 19, 13, tzinfo=UTC)  # Mon Oct 19, 9:00 am EDT


@pytest.mark.parametrize(
    ("dt", "expected"),
    [
        (datetime(2026, 10, 19, 13, tzinfo=UTC), "9 am"),
        (datetime(2026, 10, 23, 3, 30, tzinfo=UTC), "11:30 pm"),
        (datetime(2026, 10, 19, 16, 0, tzinfo=UTC), "12 pm"),
        (datetime(2026, 10, 20, 4, 5, tzinfo=UTC), "12:05 am"),
    ],
)
def test_clock_time(dt: datetime, expected: str) -> None:
    assert clock_time(dt) == expected


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(hours=2), "Today 11 am"),
        (timedelta(hours=26, minutes=30), "Tomorrow 11:30 am"),
        (timedelta(days=3, hours=14, minutes=30), "Thu 11:30 pm"),
        (timedelta(days=11, hours=14, minutes=59), "Oct 30, 11:59 pm"),
    ],
)
def test_due_text(offset: timedelta, expected: str) -> None:
    assert due_text(NOW + offset, NOW) == expected


def test_day_labels() -> None:
    today = date(2026, 10, 19)
    assert [day_label(today + timedelta(days=d), today) for d in (-1, 0, 1, 2, 9)] == [
        "Yesterday",
        "Today",
        "Tomorrow",
        "Wednesday",
        "Wed, Oct 28",
    ]
    assert long_date(today) == "Monday, October 19"


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(seconds=10), "just now"),
        (timedelta(minutes=5), "in 5 min"),
        (timedelta(minutes=-1), "1 min ago"),
        (timedelta(hours=-3), "3h ago"),
        (timedelta(hours=14, minutes=29), "in 14h"),
        (timedelta(days=3), "in 3 days"),
    ],
)
def test_relative(offset: timedelta, expected: str) -> None:
    assert relative(NOW + offset, NOW) == expected


@pytest.mark.parametrize(
    ("h", "expected"),
    [
        (None, "-"),
        (0.25, "15 min"),
        (0.01, "5 min"),
        (1.0, "1 h"),
        (2.5, "2.5 h"),
        (12.25, "12.2 h"),
    ],
)
def test_hours(h: float | None, expected: str) -> None:
    assert hours(h) == expected


def test_elapsed() -> None:
    assert elapsed(42) == "0:42"
    assert elapsed(3909) == "1:05:09"
    assert elapsed(-5) == "0:00"
