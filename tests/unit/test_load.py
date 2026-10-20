"""Capacity and demand per item: the inputs to creeping and at risk (D059)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from argus.clock import local_datetime
from argus.planner.risk import Load, Work, loads
from argus.planner.windows import WorkWindow, window_spans

MON = date(2026, 10, 19)
NOW = local_datetime(MON, 9)
EVENINGS = WorkWindow(frozenset({0, 1, 2, 3}), time(19), time(23))
SPANS = window_spans(
    NOW, NOW + timedelta(days=30), [EVENINGS], meetings=[], commitments=[], buffer_minutes=0
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return local_datetime(MON + timedelta(days=day), hour, minute)


def test_capacity_is_window_time_before_due_and_demand_includes_earlier_work() -> None:
    hw = Work(1, at(3, 23, 59), 4.0)  # due Thu night: Mon-Thu windows = 16 h
    ps = Work(2, at(1, 23, 59), 3.0)  # due Tue night: Mon, Tue = 8 h
    out = loads([hw, ps], NOW, SPANS)
    assert out[2] == Load(capacity=8.0, demand=3.0, remaining=3.0)
    assert out[1] == Load(capacity=16.0, demand=7.0, remaining=4.0)  # PS6 is due first


def test_equal_due_dates_count_each_other() -> None:
    a, b = Work(1, at(1, 23), 4.0), Work(2, at(1, 23), 4.0)
    out = loads([a, b], NOW, SPANS)
    assert out[1].demand == out[2].demand == 8.0


def test_items_that_dont_count_are_left_out_of_others_demand() -> None:
    late = Work(1, at(4, 12), 2.0)
    unconfirmed = Work(2, at(0, 8), 5.0, counts=False)  # past due, no source to confirm
    undated = Work(3, None, 9.0)
    out = loads([late, unconfirmed, undated], NOW, SPANS)
    assert out[1].demand == 2.0
    assert 3 not in out and 2 in out  # undated: no load; uncounted still gets its own


def test_reserved_study_time_and_a_skipped_window_reduce_capacity() -> None:
    hw = Work(1, at(1, 23, 59), 2.0)
    mon = SPANS[0].window_id
    tue = SPANS[1].window_id
    assert loads([hw], NOW, SPANS, reserved={mon: 1.0})[1].capacity == 7.0
    assert loads([hw], NOW, SPANS, skips={1: tue})[1].capacity == 4.0
    # Reserved time larger than what is left of a window takes all of it, never more.
    assert loads([hw], at(0, 22, 30), SPANS, reserved={mon: 1.0})[1].capacity == 4.0


def test_inside_a_window_only_the_rest_counts_and_due_inside_a_window_cuts_it() -> None:
    hw = Work(1, at(0, 21), 1.0)
    assert loads([hw], at(0, 20), SPANS)[1].capacity == 1.0


def test_earlier_work_counts_only_up_to_its_own_window_time() -> None:
    """Two items due tonight need 10 h but the window has 4 h: both are short, and Sunday's item
    only competes with the 4 h that could actually be spent before tonight's deadline."""
    hw = Work(1, at(0, 23, 30), 6.0)
    ps = Work(2, at(0, 23, 59), 4.0)
    later = Work(3, at(3, 23, 59), 3.0)
    out = loads([hw, ps, later], NOW, SPANS)
    assert out[1] == Load(capacity=4.0, demand=6.0, remaining=6.0)
    assert out[2] == Load(capacity=4.0, demand=8.0, remaining=4.0)  # 4 h can go to HW first
    assert out[3] == Load(capacity=16.0, demand=7.0, remaining=3.0)  # not 13 h


def test_slack() -> None:
    assert Load(capacity=8.0, demand=3.0, remaining=3.0).slack == 5.0


@settings(max_examples=150)
@given(
    work=st.lists(
        st.tuples(st.integers(1, 20 * 24), st.floats(0, 12), st.booleans()),
        min_size=1,
        max_size=8,
    ),
    later=st.integers(0, 10 * 24),
    reserve=st.floats(0, 4),
)
def test_property_capacity_bounded_and_never_rises_with_time(
    work: list[tuple[int, float, bool]], later: int, reserve: float
) -> None:
    items = [Work(i, NOW + timedelta(hours=h), e, c) for i, (h, e, c) in enumerate(work)]
    reserved = {s.window_id: reserve for s in SPANS}
    before = loads(items, NOW, SPANS, reserved=reserved)
    after = loads(items, NOW + timedelta(hours=later), SPANS, reserved=reserved)
    for w in items:
        assert w.due is not None
        b = before[w.item_id]
        window_time = sum(s.hours_between(NOW, w.due) for s in SPANS)
        assert -1e-9 <= b.capacity <= window_time + 1e-9
        assert after[w.item_id].capacity <= b.capacity + 1e-9  # no new logs: never more room
        # Earlier work that can no longer fit before its own deadline stops counting, so demand
        # can only fall; it never rises without new work.
        assert after[w.item_id].demand <= b.demand + 1e-9
