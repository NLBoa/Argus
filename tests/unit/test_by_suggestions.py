"""The file adder's "done by" suggestions (D074): before the course's next class, and the due
times of its upcoming items. The default is the next class."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from argus.clock import local_datetime
from argus.domain.models import Item, ItemKind, ItemStatus, MeetingRecord, Weekday
from argus.planner.suggestions import BySuggestion, by_suggestions, default_by, next_class

MON, TUE, WED, THU, FRI = Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI
# Monday 2026-10-19 09:00 EDT
NOW = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)


def meeting(
    course: str, days: tuple[Weekday, ...], start: time, kind: str = "lecture"
) -> MeetingRecord:
    end = time(start.hour + 1, start.minute)
    return MeetingRecord(course_code=course, kind=kind, days=days, start=start, end=end)


def item(
    i: int,
    due: datetime | None,
    course: str = "STAT400",
    status: ItemStatus = ItemStatus.UPCOMING,
    title: str | None = None,
) -> Item:
    return Item(
        id=i,
        course_code=course,
        kind=ItemKind.ASSIGNMENT,
        title=title or f"HW{i}",
        due_at=due,
        status=status,
    )


def on(day: int, hh: int, mm: int = 0) -> datetime:
    """A local wall time in late October 2026 (EDT), as UTC."""
    return local_datetime(datetime(2026, 10, day).date(), hh, mm)  # noqa: DTZ001


# ---------------------------------------------------------------- next class


def test_later_today() -> None:
    m = [meeting("STAT400", (MON, WED, FRI), time(11, 0))]
    assert next_class("STAT400", m, NOW) == on(19, 11)


def test_a_class_in_progress_or_starting_now_is_not_next() -> None:
    started = [meeting("STAT400", (MON, WED), time(8, 30))]
    assert next_class("STAT400", started, NOW) == on(21, 8, 30)
    exactly_now = [meeting("STAT400", (MON, WED), time(9, 0))]
    assert next_class("STAT400", exactly_now, NOW) == on(21, 9)


def test_once_a_week_wraps_to_next_week() -> None:
    m = [meeting("STAT400", (MON,), time(8, 0))]
    assert next_class("STAT400", m, NOW) == on(26, 8)


def test_earliest_of_several_meetings() -> None:
    m = [
        meeting("STAT400", (MON, WED, FRI), time(14, 0)),
        meeting("STAT400", (TUE, THU), time(10, 0), kind="discussion"),
        meeting("STAT400", (MON,), time(12, 0), kind="lab"),
    ]
    assert next_class("STAT400", m, NOW) == on(19, 12)


def test_other_courses_are_ignored() -> None:
    m = [meeting("CMSC351", (MON,), time(10, 0)), meeting("STAT400", (TUE,), time(15, 0))]
    assert next_class("STAT400", m, NOW) == on(20, 15)
    assert next_class("MATH240", m, NOW) is None


def test_course_without_meetings_has_no_next_class() -> None:
    assert next_class("STAT400", [], NOW) is None
    no_days = [MeetingRecord("STAT400", "lecture", (), time(9), time(10))]
    assert next_class("STAT400", no_days, NOW) is None


def test_across_the_fall_back() -> None:
    # Friday 2026-10-30 noon EDT; the next Monday class is after the 11-01 change, in EST.
    now = datetime(2026, 10, 30, 16, 0, tzinfo=UTC)
    m = [meeting("STAT400", (MON,), time(9, 0))]
    assert next_class("STAT400", m, now) == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def test_across_the_spring_forward() -> None:
    # Friday 2027-03-12 noon EST; Monday 09:00 is in EDT (13:00 UTC).
    now = datetime(2027, 3, 12, 17, 0, tzinfo=UTC)
    m = [meeting("STAT400", (MON,), time(9, 0))]
    assert next_class("STAT400", m, now) == datetime(2027, 3, 15, 13, 0, tzinfo=UTC)


def test_late_evening_utc_is_still_the_local_day() -> None:
    # Monday 23:30 EDT is Tuesday 03:30 UTC: a Tuesday 08:00 class is the next one.
    now = on(19, 23, 30)
    m = [meeting("STAT400", (TUE,), time(8, 0)), meeting("STAT400", (MON,), time(22, 0))]
    assert next_class("STAT400", m, now) == on(20, 8)


# ---------------------------------------------------------------- suggestions


def test_class_first_then_items_by_due_time() -> None:
    m = [meeting("STAT400", (WED,), time(10, 0))]
    items = [item(2, on(23, 23, 59)), item(1, on(20, 23, 59))]
    out = by_suggestions("STAT400", m, items, NOW)
    assert out == [
        BySuggestion(kind="class", label="Before next STAT400 class", at=on(21, 10)),
        BySuggestion(kind="item", label="HW1 due", at=on(20, 23, 59), item_id=1),
        BySuggestion(kind="item", label="HW2 due", at=on(23, 23, 59), item_id=2),
    ]


def test_only_open_future_dated_items_of_the_course() -> None:
    items = [
        item(1, on(20, 12)),
        item(2, on(20, 12), course="CMSC351"),
        item(3, on(20, 12), status=ItemStatus.SUBMITTED),
        item(4, on(20, 12), status=ItemStatus.GRADED),
        item(5, NOW - timedelta(minutes=1)),
        item(6, NOW),
        item(7, None),
        item(8, on(21, 12), status=ItemStatus.IN_PROGRESS),
    ]
    out = by_suggestions("STAT400", [], items, NOW)
    assert [s.item_id for s in out] == [1, 8]


def test_items_are_capped_and_far_ones_dropped() -> None:
    items = [item(i, NOW + timedelta(days=i)) for i in range(1, 40)]
    out = by_suggestions("STAT400", [], items, NOW, limit=3)
    assert [s.item_id for s in out] == [1, 2, 3]
    far = by_suggestions("STAT400", [], [item(1, NOW + timedelta(days=45))], NOW)
    assert far == []


def test_no_class_and_no_items_suggests_nothing() -> None:
    assert by_suggestions("STAT400", [], [], NOW) == []


def test_default_is_the_next_class() -> None:
    m = [meeting("STAT400", (WED,), time(10, 0))]
    out = by_suggestions("STAT400", m, [item(1, on(20, 12))], NOW)
    assert default_by(out) == on(21, 10)


def test_default_without_classes_is_the_first_item_then_nothing() -> None:
    out = by_suggestions("STAT400", [], [item(1, on(22, 12)), item(2, on(20, 12))], NOW)
    assert default_by(out) == on(20, 12)
    assert default_by([]) is None
