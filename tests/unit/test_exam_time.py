"""Exam study time: guaranteed, spread over the week before the exam (D062)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from argus.clock import local_datetime
from argus.planner.exam_time import ExamStudy, study_plan
from argus.planner.windows import WorkWindow, window_spans

MON = date(2026, 10, 19)
NOW = local_datetime(MON, 9)
NIGHTLY = WorkWindow(frozenset(range(7)), time(19), time(23))  # every evening, 4 h


def at(day: int, hour: int) -> datetime:
    return local_datetime(MON + timedelta(days=day), hour)


def spans(now: datetime = NOW, window: WorkWindow = NIGHTLY):  # type: ignore[no-untyped-def]
    return window_spans(
        now - timedelta(days=1),
        now + timedelta(days=30),
        [window],
        meetings=[],
        commitments=[],
        buffer_minutes=0,
    )


def test_hours_are_the_estimate_clamped_to_1_to_6() -> None:
    exam = at(9, 9)  # Wed next week, 9 am: 7 evenings before it from Wed to Tue
    for p80, hours in ((0.25, 1.0), (3.0, 3.0), (9.0, 6.0)):
        plan = study_plan([ExamStudy(5, exam, p80=p80, logged=0.0)], spans(), NOW)
        assert plan.total[5] == pytest.approx(hours)
        assert sum(q.get(5, 0) for q in plan.quota.values()) == pytest.approx(hours)


def test_spread_over_the_week_before_and_never_before_it() -> None:
    exam = at(9, 9)
    plan = study_plan([ExamStudy(5, exam, p80=3.5, logged=0.0)], spans(), NOW)
    days = sorted(plan.quota)
    assert days[0] == "2026-10-21T19:00" and days[-1] == "2026-10-27T19:00"  # T-7d .. T-1
    assert all(q[5] == pytest.approx(0.5) for q in plan.quota.values())  # 3.5 h / 7 = 30 min


def test_minimum_20_minutes_front_loaded() -> None:
    plan = study_plan([ExamStudy(5, at(9, 9), p80=1.0, logged=0.0)], spans(), NOW)
    amounts = [plan.quota[w][5] for w in sorted(plan.quota)]
    assert amounts == pytest.approx([1 / 3, 1 / 3, 1 / 3])  # 20 min each until the hour is used


def test_capped_at_half_a_window_and_three_quarters_in_the_last_48h() -> None:
    mon_tue = WorkWindow(frozenset({0, 1}), time(19), time(23))
    # Fri 9 am exam: Mon and Tue evenings start more than 48 h before it, so 50% of 4 h each.
    far = study_plan([ExamStudy(5, at(4, 9), p80=6.0, logged=0.0)], spans(window=mon_tue), NOW)
    assert [far.quota[w][5] for w in sorted(far.quota)] == pytest.approx([2.0, 2.0])
    assert far.shortfall[5] == pytest.approx(2.0)
    # Wed 9 am exam: both evenings are in the last 48 h, so 75% of 4 h each.
    near = study_plan([ExamStudy(5, at(2, 9), p80=6.0, logged=0.0)], spans(window=mon_tue), NOW)
    assert [near.quota[w][5] for w in sorted(near.quota)] == pytest.approx([3.0, 3.0])
    assert near.shortfall == {}


def test_logged_time_on_the_exam_is_progress() -> None:
    plan = study_plan([ExamStudy(5, at(9, 9), p80=3.5, logged=2.0)], spans(), NOW)
    assert sum(q[5] for q in plan.quota.values()) == pytest.approx(1.5)
    done = study_plan([ExamStudy(5, at(9, 9), p80=3.5, logged=5.0)], spans(), NOW)
    assert done.quota == {} and done.shortfall == {}


def test_short_window_fragments_are_skipped() -> None:
    sliver = WorkWindow(frozenset(range(7)), time(19), time(19, 20))
    plan = study_plan([ExamStudy(5, at(9, 9), p80=2.0, logged=0.0)], spans(window=sliver), NOW)
    assert plan.quota == {} and plan.shortfall[5] == pytest.approx(2.0)


def test_exam_far_away_reserves_only_its_last_week() -> None:
    plan = study_plan([ExamStudy(5, at(20, 9), p80=3.0, logged=0.0)], spans(), NOW)
    assert min(plan.quota) == "2026-11-01T19:00"  # nothing before T - 7 days
    assert plan.shortfall == {}


def test_two_exams_never_take_more_than_three_quarters_of_a_window() -> None:
    a = ExamStudy(5, at(2, 9), p80=6.0, logged=0.0)
    b = ExamStudy(6, at(2, 10), p80=6.0, logged=0.0)
    plan = study_plan([a, b], spans(), NOW)
    for w, q in plan.quota.items():
        assert sum(q.values()) <= 0.75 * 4.0 + 1e-9, w


@settings(max_examples=150)
@given(
    hours=st.lists(
        st.tuples(st.integers(1, 12 * 24), st.floats(0, 12), st.floats(0, 8)), max_size=4
    ),
    later=st.integers(0, 6 * 24),
)
def test_property_reserved_time_fits_windows(
    hours: list[tuple[int, float, float]], later: int
) -> None:
    exams = [ExamStudy(i, NOW + timedelta(hours=h), p, lg) for i, (h, p, lg) in enumerate(hours)]
    now = NOW + timedelta(hours=later)
    windows = {s.window_id: s for s in spans()}
    plan = study_plan(exams, list(windows.values()), now)
    for wid, q in plan.quota.items():
        assert sum(q.values()) <= windows[wid].capacity * 0.75 + 1e-9
        assert all(v > 0 for v in q.values())
    for e in exams:
        total = sum(q.get(e.exam_id, 0.0) for q in plan.quota.values())
        assert total + plan.shortfall.get(e.exam_id, 0.0) <= plan.total[e.exam_id] + 1e-9
