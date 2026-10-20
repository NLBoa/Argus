"""The /workload curve: cumulative window time against cumulative work due, per local day (D065)."""

from __future__ import annotations

import itertools
from datetime import date, datetime, time, timedelta

from hypothesis import given
from hypothesis import strategies as st

from argus.clock import local_datetime
from argus.planner.windows import WorkWindow, window_spans
from argus.planner.workload import curve

MON = date(2026, 10, 19)
NOW = local_datetime(MON, 9)
EVENINGS = WorkWindow(frozenset({0, 1, 2, 3}), time(19), time(23))  # Mon-Thu, 4 h
SPANS = window_spans(
    NOW - timedelta(days=1),
    NOW + timedelta(days=30),
    [EVENINGS],
    meetings=[],
    commitments=[],
    buffer_minutes=0,
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return local_datetime(MON + timedelta(days=day), hour, minute)


def test_days_start_at_local_midnight_from_today() -> None:
    days = curve(SPANS, [], NOW, days=3)
    assert [d.start for d in days] == [at(0, 0), at(1, 0), at(2, 0)]


def test_window_time_counts_only_what_is_left_and_accumulates() -> None:
    days = curve(SPANS, [], at(0, 21), days=5)  # Monday's window is half gone
    assert [d.window_hours for d in days] == [2.0, 4.0, 4.0, 4.0, 0.0]
    assert [d.window_total for d in days] == [2.0, 6.0, 10.0, 14.0, 14.0]


def test_work_lands_on_the_day_it_is_due() -> None:
    dues = [(at(1, 23, 59), 3.0), (at(1, 12), 1.5), (at(3, 9), 2.0)]
    days = curve(SPANS, dues, NOW, days=4)
    assert [d.due_hours for d in days] == [0.0, 4.5, 0.0, 2.0]
    assert [d.due_total for d in days] == [0.0, 4.5, 4.5, 6.5]


def test_work_due_past_the_horizon_or_already_past_is_left_out() -> None:
    dues = [(at(20, 12), 9.0), (at(0, 8), 2.0)]
    days = curve(SPANS, dues, NOW, days=14)
    assert days[-1].due_total == 0.0


def test_shortfall_is_work_due_beyond_window_time_so_far() -> None:
    days = curve(SPANS, [(at(0, 23), 6.0), (at(2, 23), 3.0)], NOW, days=3)
    assert [d.shortfall for d in days] == [2.0, 0.0, 0.0]


def test_dst_fall_back_day_is_one_day() -> None:
    nov = local_datetime(date(2026, 10, 30), 9)  # DST ends Sun Nov 1 in New York
    days = curve([], [], nov, days=4)
    assert [d.start for d in days] == [
        local_datetime(date(2026, 10, 30) + timedelta(days=n), 0) for n in range(4)
    ]


@given(st.lists(st.tuples(st.integers(0, 15), st.floats(0, 20)), max_size=12))
def test_totals_never_decrease(raw: list[tuple[int, float]]) -> None:
    days = curve(SPANS, [(at(d, 18), h) for d, h in raw], NOW, days=14)
    for a, b in itertools.pairwise(days):
        assert b.window_total >= a.window_total and b.due_total >= a.due_total
