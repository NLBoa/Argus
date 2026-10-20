"""Lanes for the day strip and the kiosk rail (D063)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.clock import local_datetime, to_local
from argus.planner.lanes import assign_lanes, hour_ticks

T = datetime(2026, 10, 19, 12, tzinfo=UTC)


def span(a: int, b: int) -> tuple[datetime, datetime]:
    return T + timedelta(minutes=a), T + timedelta(minutes=b)


def test_disjoint_and_touching_blocks_share_one_lane() -> None:
    assert assign_lanes([span(0, 60), span(60, 90), span(120, 150)]) == ([0, 0, 0], 1)


def test_overlaps_get_the_lowest_free_lane_in_input_order() -> None:
    lanes, n = assign_lanes([span(30, 90), span(0, 60), span(60, 120)])
    assert n == 2
    assert lanes[1] == 0  # starts first
    assert lanes[0] == 1
    assert lanes[2] == 0  # lane 0 is free again at 60


def test_empty_and_invalid() -> None:
    assert assign_lanes([]) == ([], 0)
    with pytest.raises(ValueError, match="ends before"):
        assign_lanes([span(10, 0)])


def _max_overlap(spans: list[tuple[datetime, datetime]]) -> int:
    events = sorted([(s, 1) for s, e in spans if e > s] + [(e, -1) for s, e in spans if e > s])
    best = cur = 0
    for _, d in events:  # ends (-1) sort before starts (+1) at the same time: half-open
        cur += d
        best = max(best, cur)
    return best


@given(st.lists(st.tuples(st.integers(0, 600), st.integers(1, 180)), max_size=12))
def test_overlapping_blocks_never_share_a_lane_and_lanes_equal_max_overlap(
    raw: list[tuple[int, int]],
) -> None:
    spans = [span(a, a + d) for a, d in raw]
    lanes, n = assign_lanes(spans)
    for i, (s1, e1) in enumerate(spans):
        for j, (s2, e2) in enumerate(spans):
            if i < j and s1 < e2 and s2 < e1:
                assert lanes[i] != lanes[j]
    assert n == _max_overlap(spans)
    assert all(0 <= x < n for x in lanes)


def test_hour_ticks_are_real_local_hours_across_fall_back() -> None:
    day = date(2026, 11, 1)  # 25 hours: 1 am happens twice
    ticks = hour_ticks(local_datetime(day, 0), local_datetime(day + timedelta(days=1), 0))
    assert len(ticks) == 25
    assert [to_local(t).hour for t in ticks[:4]] == [0, 1, 1, 2]
    assert all(b - a == timedelta(hours=1) for a, b in pairwise(ticks))


def test_hour_ticks_start_at_the_first_whole_hour() -> None:
    start = local_datetime(date(2026, 10, 19), 18, 30)
    ticks = hour_ticks(start, start + timedelta(hours=2))
    assert [to_local(t).hour for t in ticks] == [19, 20]
