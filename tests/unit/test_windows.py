"""Work windows: when the student works, as real time (D057)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.clock import local_datetime
from argus.config import Commitment, ScheduleConfig, WorkWindowConfig
from argus.demo import DEMO_CONFIG
from argus.domain.models import MeetingRecord, Weekday
from argus.planner.windows import (
    WorkWindow,
    current_or_next,
    from_config,
    hours_between,
    overlapping_windows,
    window_id,
    window_spans,
)

MON = date(2026, 10, 19)
NOW = local_datetime(MON, 9)  # Mon 09:00 EDT
EVENINGS = WorkWindow(frozenset({0, 1, 2, 3}), time(19), time(23))  # Mon-Thu 7-11 pm
SUNDAY = WorkWindow(frozenset({6}), time(13), time(18))


def spans(*windows: WorkWindow, start: datetime = NOW, days: int = 7, **busy: object):  # type: ignore[no-untyped-def]
    return window_spans(
        start,
        start + timedelta(days=days),
        list(windows),
        meetings=busy.get("meetings", ()),  # type: ignore[arg-type]
        commitments=busy.get("commitments", ()),  # type: ignore[arg-type]
        buffer_minutes=15,
    )


def test_from_config_and_ids() -> None:
    cfg = [WorkWindowConfig(days=["Mon", "Thu"], start=time(19), end=time(23))]
    assert from_config(cfg) == [WorkWindow(frozenset({0, 3}), time(19), time(23))]
    assert window_id(MON, time(19)) == "2026-10-19T19:00"


def test_week_of_evenings_and_a_sunday() -> None:
    week = spans(EVENINGS, SUNDAY)
    assert [s.window_id for s in week] == [
        "2026-10-19T19:00",
        "2026-10-20T19:00",
        "2026-10-21T19:00",
        "2026-10-22T19:00",
        "2026-10-25T13:00",
    ]
    assert all(s.capacity == pytest.approx(4.0) for s in week[:4])
    assert week[4].capacity == pytest.approx(5.0)
    assert hours_between(week, NOW, NOW + timedelta(days=7)) == pytest.approx(21.0)


def test_classes_with_a_walking_buffer_and_commitments_are_cut_out() -> None:
    night_class = MeetingRecord("CMSC351", "lecture", (Weekday.MON,), time(20), time(21))
    shift = Commitment(title="Shift", days=["Tue"], start=time(17), end=time(20))
    week = spans(EVENINGS, meetings=[night_class], commitments=[shift], days=2)
    mon, tue = week[0], week[1]
    assert mon.capacity == pytest.approx(4.0 - 1.5)  # 7:45-9:15 with the buffer
    assert tue.capacity == pytest.approx(3.0)  # 8-11: the shift ends at 8
    assert mon.start == local_datetime(MON, 19) and mon.end == local_datetime(MON, 23)


def test_window_past_midnight_belongs_to_its_start_day() -> None:
    late = WorkWindow(frozenset({4}), time(22), time(2))  # Fri 10 pm - 2 am
    [fri] = spans(late)
    assert fri.window_id == "2026-10-23T22:00"
    assert fri.end == local_datetime(date(2026, 10, 24), 2)
    assert fri.capacity == pytest.approx(4.0)


def test_dst_fall_back_counts_real_hours() -> None:
    late = WorkWindow(frozenset({5}), time(22), time(2))  # Sat Oct 31 10 pm - Sun 2 am
    [sat] = spans(late, start=local_datetime(date(2026, 10, 31), 12), days=1)
    assert sat.capacity == pytest.approx(5.0)  # 1-2 am happens twice


def test_dst_spring_forward_counts_real_hours() -> None:
    late = WorkWindow(frozenset({5}), time(22), time(4))  # Sat Mar 13 10 pm - Sun 4 am
    [sat] = spans(late, start=local_datetime(date(2027, 3, 13), 12), days=1)
    assert sat.capacity == pytest.approx(5.0)  # 2-3 am does not exist


def test_overlapping_windows_merge_under_the_first_id() -> None:
    a = WorkWindow(frozenset({0}), time(18), time(21))
    b = WorkWindow(frozenset({0}), time(20), time(23))
    [mon] = spans(a, b, days=1)
    assert mon.window_id == "2026-10-19T18:00" and mon.capacity == pytest.approx(5.0)
    assert overlapping_windows([a, b]) == ["Mon 18:00-21:00 overlaps Mon 20:00-23:00"]
    assert overlapping_windows([EVENINGS, SUNDAY]) == []


def test_ids_do_not_move_when_a_class_appears() -> None:
    plain = [s.window_id for s in spans(EVENINGS)]
    lab = MeetingRecord("MATH240", "lab", (Weekday.MON,), time(19), time(20))
    assert [s.window_id for s in spans(EVENINGS, meetings=[lab])] == plain


def test_inside_a_window_only_the_rest_counts() -> None:
    week = spans(EVENINGS, days=1)
    at_8pm = local_datetime(MON, 20)
    assert hours_between(week, at_8pm, at_8pm + timedelta(hours=10)) == pytest.approx(3.0)
    due_at_10 = local_datetime(MON, 22)
    assert hours_between(week, at_8pm, due_at_10) == pytest.approx(2.0)


def test_current_or_next_window() -> None:
    week = spans(EVENINGS, SUNDAY)
    assert current_or_next(week, NOW, within=timedelta(hours=24)) == week[0]
    assert current_or_next(week, local_datetime(MON, 20), within=timedelta(0)) == week[0]
    thu_late = local_datetime(date(2026, 10, 22), 23, 30)
    assert current_or_next(week, thu_late, within=timedelta(hours=24)) is None  # Sun is >24h
    assert current_or_next(week, thu_late, within=timedelta(days=3)) == week[4]


def test_no_windows_no_time() -> None:
    assert spans() == []
    assert hours_between([], NOW, NOW + timedelta(days=30)) == 0.0


def test_demo_has_38_hours_in_14_days() -> None:
    """8 weekday evenings x 4 h + 2 Sundays x 5 h - 4 h of Tue/Thu dining shift (5-8 pm)."""
    cfg: ScheduleConfig = DEMO_CONFIG.schedule
    demo = window_spans(
        NOW,
        NOW + timedelta(days=14),
        from_config(cfg.work_windows),
        meetings=[],
        commitments=cfg.commitments,
        buffer_minutes=cfg.walking_buffer_minutes,
    )
    assert hours_between(demo, NOW, NOW + timedelta(days=14)) == pytest.approx(38.0)


@given(
    a=st.integers(0, 14 * 24),
    b=st.integers(0, 14 * 24),
    c=st.integers(0, 14 * 24),
)
def test_property_hours_are_additive_and_bounded(a: int, b: int, c: int) -> None:
    t0, t1, t2 = sorted(NOW + timedelta(hours=x) for x in (a, b, c))
    week = spans(EVENINGS, SUNDAY, days=15)
    whole = hours_between(week, t0, t2)
    assert whole == pytest.approx(hours_between(week, t0, t1) + hours_between(week, t1, t2))
    assert 0 <= whole <= (t2 - t0).total_seconds() / 3600 + 1e-9


def test_naive_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        window_spans(
            datetime(2026, 10, 19),  # noqa: DTZ001 - the point of the test
            NOW,
            [EVENINGS],
            meetings=[],
            commitments=[],
            buffer_minutes=0,
        )
    assert NOW.tzinfo is UTC
