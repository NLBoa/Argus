from datetime import UTC, date, datetime, time

from argus.config import Commitment
from argus.domain.models import MeetingRecord, Weekday
from argus.planner.freetime import Busy, busy_on, meetings_on

LEC = MeetingRecord(
    "CMSC351", "lecture", (Weekday.TUE, Weekday.THU), time(9, 30), time(10, 45), "IRB", "1101"
)
LAB = MeetingRecord("MATH240", "lab", (Weekday.TUE,), time(8, 0), time(8, 50))


def test_meetings_on_weekday_in_utc_sorted() -> None:
    tue = date(2026, 10, 20)
    busy = meetings_on(tue, [LEC, LAB])
    assert [(b.start, b.end, b.title) for b in busy] == [
        (
            datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
            datetime(2026, 10, 20, 12, 50, tzinfo=UTC),
            "MATH240 lab",
        ),
        (
            datetime(2026, 10, 20, 13, 30, tzinfo=UTC),
            datetime(2026, 10, 20, 14, 45, tzinfo=UTC),
            "CMSC351 lecture",
        ),
    ]
    assert busy[1].course_code == "CMSC351"
    assert busy[1].where == "IRB 1101"
    assert busy[0].where is None
    assert meetings_on(date(2026, 10, 19), [LEC, LAB]) == []  # Monday


def test_meetings_follow_wall_clock_across_dst() -> None:
    # Thu Oct 29 (EDT) vs Thu Nov 5 (EST): same 9:30 local, different UTC.
    assert meetings_on(date(2026, 10, 29), [LEC])[0].start == datetime(
        2026, 10, 29, 13, 30, tzinfo=UTC
    )
    assert meetings_on(date(2026, 11, 5), [LEC])[0].start == datetime(
        2026, 11, 5, 14, 30, tzinfo=UTC
    )


def test_busy_on_includes_commitments() -> None:
    shift = Commitment(title="Dining hall shift", days=["Tue", "Thu"], start=time(17), end=time(20))
    busy = busy_on(date(2026, 10, 20), [LEC], [shift])
    assert [b.kind for b in busy] == ["class", "commitment"]
    assert busy[1] == Busy(
        start=datetime(2026, 10, 20, 21, tzinfo=UTC),
        end=datetime(2026, 10, 21, 0, tzinfo=UTC),
        kind="commitment",
        title="Dining hall shift",
    )
    assert busy_on(date(2026, 10, 21), [LEC], [shift]) == []
