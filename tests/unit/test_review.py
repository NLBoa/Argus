from datetime import UTC, datetime, timedelta

import pytest

from argus.planner.review import (
    Attempt,
    Readiness,
    ReviewConcept,
    readiness,
    review_order,
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
