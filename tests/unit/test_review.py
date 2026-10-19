from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.clock import local_datetime, to_local
from argus.planner.freetime import Interval
from argus.planner.review import (
    REVIEW_DAYS,
    SESSION,
    Attempt,
    Readiness,
    ReviewConcept,
    place_reviews,
    readiness,
    review_order,
    review_sessions,
)

T = datetime(2026, 10, 19, 13, tzinfo=UTC)


def att(item: int, result: str, minutes: int = 0) -> Attempt:
    return Attempt(item, result, T + timedelta(minutes=minutes))


@pytest.mark.parametrize(
    ("items", "attempts", "expected"),
    [
        ([1, 2], [], Readiness.UNTESTED),
        ([], [], Readiness.UNTESTED),
        ([1, 2], [att(1, "correct"), att(2, "correct")], Readiness.SOLID),
        ([1, 2], [att(1, "correct"), att(2, "incorrect")], Readiness.SHAKY),
        (
            [1, 2],
            [att(1, "incorrect"), att(1, "correct", 5), att(2, "correct")],
            Readiness.SOLID,
        ),  # latest counts
        ([1, 2], [att(1, "correct"), att(1, "shaky", 5)], Readiness.SHAKY),
        ([1, 2, 3, 4], [att(1, "correct")], Readiness.SHAKY),  # under half attempted: not yet solid
        ([1, 2, 3], [att(1, "correct"), att(2, "correct")], Readiness.SOLID),
        ([1], [att(9, "correct")], Readiness.UNTESTED),  # attempts on other items do not count
    ],
)
def test_readiness(items: list[int], attempts: list[Attempt], expected: Readiness) -> None:
    assert readiness(items, attempts) is expected


def rc(i: int, r: Readiness, module: int, priority: float = 0) -> ReviewConcept:
    return ReviewConcept(i, f"c{i}", module, r, priority)


def test_weakest_first_then_interleaved_by_module() -> None:
    concepts = [
        rc(1, Readiness.SOLID, 1),
        rc(2, Readiness.SHAKY, 1),
        rc(3, Readiness.UNTESTED, 1),
        rc(4, Readiness.SHAKY, 2),
        rc(5, Readiness.SOLID, 2),
        rc(6, Readiness.UNTESTED, 2, priority=2),
    ]
    order = [c.id for c in review_order(concepts)]
    assert order[:2] in ([2, 4], [4, 2])  # shaky ones lead, alternating modules
    assert order == [2, 4, 3, 6, 1, 5] or order == [4, 2, 6, 3, 5, 1]
    assert review_order([]) == []


def test_priority_breaks_ties_within_readiness() -> None:
    order = review_order([rc(1, Readiness.UNTESTED, 1), rc(2, Readiness.UNTESTED, 1, priority=3)])
    assert [c.id for c in order] == [2, 1]


def test_sessions_counted_backward_from_exam() -> None:
    exam = local_datetime(date(2026, 10, 28), 9, 30)
    concepts = [rc(i, Readiness.UNTESTED, i % 2) for i in range(1, 8)]
    sessions = review_sessions(exam, T, concepts)
    assert [s.day for s in sessions] == [
        date(2026, 10, 21),
        date(2026, 10, 23),
        date(2026, 10, 25),
        date(2026, 10, 26),
        date(2026, 10, 27),
    ]
    assert all(len(s.concepts) == 3 for s in sessions)
    covered = {c.id for s in sessions for c in s.concepts}
    assert covered == set(range(1, 8))  # every concept reviewed at least once
    late = review_sessions(exam, local_datetime(date(2026, 10, 26), 12), concepts)
    assert [s.day for s in late] == [date(2026, 10, 26), date(2026, 10, 27)]
    assert review_sessions(exam, T, []) == []
    assert REVIEW_DAYS == (7, 5, 3, 2, 1)


def test_place_reviews_in_free_time_of_that_day() -> None:
    day = date(2026, 10, 21)
    sessions = review_sessions(
        local_datetime(date(2026, 10, 28), 9), T, [rc(1, Readiness.SHAKY, 1)]
    )[:1]
    free = [
        Interval(local_datetime(day, 8), local_datetime(day, 8, 30)),  # too short
        Interval(local_datetime(day, 14, 7), local_datetime(day, 16)),
    ]
    [block] = place_reviews(sessions, free)
    assert block.start == local_datetime(day, 14, 15)  # grid-aligned
    assert block.end - block.start == SESSION
    assert place_reviews(sessions, []) == []


@given(st.integers(0, 60), st.integers(1, 12))
def test_reviews_never_after_the_exam(days_out: int, n: int) -> None:
    exam = T + timedelta(days=days_out, hours=3)
    sessions = review_sessions(exam, T, [rc(i, Readiness.UNTESTED, 1) for i in range(n)])
    assert all(s.day < to_local(exam).date() for s in sessions)
    assert all(s.day >= to_local(T).date() for s in sessions)
