from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from argus.clock import local_datetime, to_local
from argus.config import Commitment, TimeWindow
from argus.domain.models import MeetingRecord, Weekday
from argus.planner.freetime import Interval, free_hours_between, free_intervals, subtract
from argus.planner.scheduler import BREAK, GRID, MAX_CHUNK, MIN_CHUNK, PlanItem, schedule

WAKING = TimeWindow(start=time(8), end=time(23))
QUIET = TimeWindow(start=time(23), end=time(8))
LECTURE = MeetingRecord("CMSC351", "lecture", (Weekday.TUE, Weekday.THU), time(9, 30), time(10, 45))
SHIFT = Commitment(title="Shift", days=["Tue"], start=time(17), end=time(20))
MON = datetime(2026, 10, 19, 13, tzinfo=UTC)  # Mon 9:00 am EDT


def free(start: datetime, days: int = 7, buffer: int = 15) -> list[Interval]:
    return free_intervals(
        start,
        start + timedelta(days=days),
        waking=WAKING,
        quiet=QUIET,
        buffer_minutes=buffer,
        meetings=[LECTURE],
        commitments=[SHIFT],
    )


# ---------------------------------------------------------------- free time


def test_subtract_and_merge() -> None:
    a = [Interval(MON, MON + timedelta(hours=10))]
    holes = [
        Interval(MON + timedelta(hours=1), MON + timedelta(hours=2)),
        Interval(MON + timedelta(hours=1, minutes=30), MON + timedelta(hours=3)),
    ]
    assert subtract(a, holes) == [
        Interval(MON, MON + timedelta(hours=1)),
        Interval(MON + timedelta(hours=3), MON + timedelta(hours=10)),
    ]


def test_free_time_excludes_class_with_walking_buffer_and_commitments() -> None:
    tue = [i for i in free(MON) if to_local(i.start).date() == date(2026, 10, 20)]
    assert [(f"{to_local(i.start):%H:%M}", f"{to_local(i.end):%H:%M}") for i in tue] == [
        ("08:00", "09:15"),  # 15 min walk before the 9:30 lecture
        ("11:00", "17:00"),  # 15 min after it ends at 10:45; shift starts at 17:00
        ("20:00", "23:00"),
    ]


def test_free_time_starts_now_and_respects_waking_window() -> None:
    mon = free(MON, days=1)
    assert mon[0].start == MON  # starts at "now", 9:00 am
    assert all(WAKING.contains(to_local(i.start).time()) for i in mon)
    assert free_hours_between(mon, MON, MON + timedelta(hours=3)) == pytest.approx(3.0)


def test_free_time_across_fall_back() -> None:
    # Sat Oct 31 -> Sun Nov 1 (25-hour day). The waking window is still 8:00-23:00 local.
    sun = [
        i
        for i in free(datetime(2026, 10, 31, 4, tzinfo=UTC), days=3)
        if to_local(i.start).date() == date(2026, 11, 1)
    ]
    assert [(f"{to_local(i.start):%H:%M}", f"{to_local(i.end):%H:%M}") for i in sun] == [
        ("08:00", "23:00")
    ]
    assert sun[0].start == local_datetime(date(2026, 11, 1), 8)


def test_wrapping_waking_window() -> None:
    late = TimeWindow(start=time(10), end=time(1))
    out = free_intervals(
        MON,
        MON + timedelta(days=1),
        waking=late,
        quiet=TimeWindow(start=time(2), end=time(9)),
        buffer_minutes=0,
        meetings=[],
        commitments=[],
    )
    assert (f"{to_local(out[0].start):%H:%M}", f"{to_local(out[0].end):%a %H:%M}") == (
        "10:00",
        "Tue 01:00",
    )


# ---------------------------------------------------------------- scheduler examples


def item(i: int, due_h: float, hours: float) -> PlanItem:
    return PlanItem(
        item_id=i, due_at=MON + timedelta(hours=due_h), remaining=timedelta(hours=hours)
    )


def test_edf_and_chunking() -> None:
    plan = schedule([item(2, 72, 3), item(1, 30, 2.5)], free(MON))
    first = [b for b in plan.blocks if b.item_id == 1]
    assert first[0].start == MON  # earliest deadline gets the first slot
    assert all(b.end - b.start <= MAX_CHUNK for b in plan.blocks)
    assert sum(((b.end - b.start) for b in first), timedelta()) == timedelta(hours=2, minutes=30)
    assert plan.unscheduled == {}


def test_break_between_chunks_and_grid_alignment() -> None:
    plan = schedule(
        [item(1, 72, 4)], [Interval(MON + timedelta(minutes=7), MON + timedelta(hours=8))]
    )
    b1, b2 = plan.blocks
    assert b1.start == MON + timedelta(minutes=15)  # rounded up to the grid
    assert b2.start - b1.end == BREAK


def test_unschedulable_work_is_reported() -> None:
    plan = schedule([item(1, 2, 5)], [Interval(MON, MON + timedelta(hours=8))])
    assert plan.unscheduled == {1: pytest.approx(3.0)}  # only 2 hours exist before the due time


def test_tiny_gaps_are_not_used_for_large_work() -> None:
    gaps = [
        Interval(MON, MON + timedelta(minutes=20)),
        Interval(MON + timedelta(hours=1), MON + timedelta(hours=3)),
    ]
    plan = schedule([item(1, 48, 1)], gaps)
    assert [b.start for b in plan.blocks] == [MON + timedelta(hours=1)]


def test_small_remainders_fit_small_gaps() -> None:
    plan = schedule([item(1, 48, 0.25)], [Interval(MON, MON + timedelta(minutes=20))])
    assert len(plan.blocks) == 1 and plan.blocks[0].end - plan.blocks[0].start == GRID


def test_zero_remaining_and_undated() -> None:
    plan = schedule(
        [PlanItem(1, MON + timedelta(days=1), timedelta(0)), PlanItem(2, None, timedelta(hours=1))],
        free(MON),
    )
    assert [b.item_id for b in plan.blocks] == [2]


# ---------------------------------------------------------------- properties

items_st = st.lists(
    st.builds(item, i=st.integers(1, 30), due_h=st.floats(1, 24 * 9), hours=st.floats(0, 9)),
    max_size=12,
    unique_by=lambda p: p.item_id,
)
start_st = st.integers(0, 24 * 60).map(
    lambda m: datetime(2026, 10, 26, 4, tzinfo=UTC) + timedelta(minutes=m)
)


@settings(max_examples=150, deadline=None)
@given(items_st, start_st, st.randoms(use_true_random=False))
def test_schedule_properties(items: list[PlanItem], start: datetime, rnd: object) -> None:
    windows = free(start, days=8)
    plan = schedule(items, windows)
    blocks = sorted(plan.blocks, key=lambda b: b.start)
    busy_class = [
        Interval(
            local_datetime(to_local(start).date() + timedelta(days=d), 9, 30),
            local_datetime(to_local(start).date() + timedelta(days=d), 10, 45),
        )
        for d in range(10)
        if (to_local(start).date() + timedelta(days=d)).weekday() in LECTURE.days
    ]
    for a, b in pairwise(blocks):
        assert a.end <= b.start, "double-booked"
    by_id = {i.item_id: i for i in items}
    for b in blocks:
        assert any(w.start <= b.start and b.end <= w.end for w in windows), "outside free time"
        assert not any(b.start < c.end and c.start < b.end for c in busy_class), "during class"
        assert not QUIET.contains(to_local(b.start).time()), "in quiet hours"
        assert GRID <= b.end - b.start <= MAX_CHUNK
        assert (b.start - datetime(2026, 1, 1, tzinfo=UTC)) % GRID == timedelta(0)
        due = by_id[b.item_id].due_at
        assert due is None or b.end <= due
    for it in items:
        scheduled = sum(((b.end - b.start) for b in blocks if b.item_id == it.item_id), timedelta())
        need = -(-it.remaining // GRID) * GRID
        mine = [b for b in blocks if b.item_id == it.item_id]
        if need >= MIN_CHUNK:
            assert all(b.end - b.start >= MIN_CHUNK for b in mine), "sliver session"
        total = scheduled + timedelta(hours=plan.unscheduled.get(it.item_id, 0.0))
        assert abs(total - need) < timedelta(seconds=1)
    shuffled = list(items)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert schedule(shuffled, windows) == plan


def test_no_sliver_remainders() -> None:
    # 2h15m of work in a 2h window then a later window: 1h45 + 30 min, not 2h + 15 min.
    windows = [
        Interval(MON, MON + timedelta(hours=2)),
        Interval(MON + timedelta(hours=4), MON + timedelta(hours=8)),
    ]
    plan = schedule([item(1, 48, 2.25)], windows)
    assert [b.end - b.start for b in plan.blocks] == [
        timedelta(hours=1, minutes=45),
        timedelta(minutes=30),
    ]
