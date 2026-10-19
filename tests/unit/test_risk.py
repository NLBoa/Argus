"""Risk levels (D059, D056)."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.domain.models import Item, ItemKind, ItemStatus
from argus.planner.risk import Load, RiskLevel, assess

NOW = datetime(2026, 10, 19, 13, tzinfo=UTC)


def item(due: timedelta | None, status: ItemStatus = ItemStatus.UPCOMING, **kw: object) -> Item:
    base: dict[str, object] = {
        "id": 1,
        "course_code": "CMSC351",
        "kind": ItemKind.ASSIGNMENT,
        "title": "HW",
        "due_at": NOW + due if due is not None else None,
        "status": status,
    }
    base.update(kw)
    return Item(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- without a load (no windows)


@pytest.mark.parametrize(
    ("it", "expected"),
    [
        (item(timedelta(days=3)), RiskLevel.OK),
        (item(timedelta(hours=20)), RiskLevel.CREEPING),
        (item(timedelta(hours=20), ItemStatus.IN_PROGRESS), RiskLevel.OK),
        (item(timedelta(hours=-1)), RiskLevel.OVERDUE),
        (item(timedelta(hours=-1), ItemStatus.MISSED), RiskLevel.OVERDUE),
        (item(timedelta(hours=-1), ItemStatus.SUBMITTED), RiskLevel.OK),
        (item(timedelta(hours=-1), ItemStatus.GRADED), RiskLevel.OK),
        (item(None), RiskLevel.OK),
        (item(timedelta(hours=2), kind=ItemKind.EXAM), RiskLevel.OK),
        (item(timedelta(hours=2), kind=ItemKind.EVENT), RiskLevel.OK),
    ],
)
def test_time_only_rule(it: Item, expected: RiskLevel) -> None:
    assert assess(it, NOW, tracked=True) == expected


# ---------------------------------------------------------------- on window time


@pytest.mark.parametrize(
    ("capacity", "demand", "remaining", "expected"),
    [
        (3.9, 4.0, 2.0, RiskLevel.AT_RISK),  # C < D
        (4.0, 4.0, 2.0, RiskLevel.CREEPING),  # C == D is not at risk (strict)
        (5.9, 4.0, 2.0, RiskLevel.CREEPING),  # C < 1.5 D
        (6.0, 4.0, 2.0, RiskLevel.OK),  # C == 1.5 D is not creeping (strict)
        (20.0, 4.0, 2.0, RiskLevel.OK),
        (0.0, 3.0, 0.0, RiskLevel.OK),  # nothing left: OK even with no window time left
        (0.0, 0.5, 0.5, RiskLevel.AT_RISK),  # no work window before it's due
    ],
)
def test_load_rule(capacity: float, demand: float, remaining: float, expected: RiskLevel) -> None:
    load = Load(capacity=capacity, demand=demand, remaining=remaining)
    assert assess(item(timedelta(days=3)), NOW, tracked=True, load=load) == expected


def test_load_never_hides_overdue_or_closed() -> None:
    tight = Load(capacity=0.0, demand=5.0, remaining=5.0)
    assert assess(item(timedelta(hours=-1)), NOW, tracked=True, load=tight) == RiskLevel.OVERDUE
    done = item(timedelta(days=1), ItemStatus.SUBMITTED)
    assert assess(done, NOW, tracked=True, load=tight) == RiskLevel.OK


def test_the_time_only_rule_does_not_apply_with_a_load() -> None:
    roomy = Load(capacity=10.0, demand=1.0, remaining=1.0)
    assert assess(item(timedelta(hours=20)), NOW, tracked=True, load=roomy) == RiskLevel.OK


@given(
    capacity=st.floats(0, 50),
    demand=st.floats(0.01, 50),
    remaining=st.floats(0.01, 20),
)
def test_property_levels_are_ordered_by_room(
    capacity: float, demand: float, remaining: float
) -> None:
    level = assess(
        item(timedelta(days=3)),
        NOW,
        tracked=True,
        load=Load(capacity=capacity, demand=demand, remaining=remaining),
    )
    if level is RiskLevel.AT_RISK:
        assert capacity < demand
    elif level is RiskLevel.CREEPING:
        assert demand <= capacity < 1.5 * demand
    else:
        assert level is RiskLevel.OK and capacity >= 1.5 * demand


# ---------------------------------------------------------------- untracked submissions (D056)


def test_untracked_past_due_is_unconfirmed_not_overdue() -> None:
    it = item(timedelta(hours=-1))
    assert assess(it, NOW, tracked=False) == RiskLevel.UNCONFIRMED
    assert assess(it, NOW, tracked=True) == RiskLevel.OVERDUE
    assert assess(item(timedelta(hours=20)), NOW, tracked=False) == RiskLevel.CREEPING
    submitted = item(timedelta(hours=-1), ItemStatus.SUBMITTED)
    assert assess(submitted, NOW, tracked=False) == RiskLevel.OK


def test_untracked_in_a_late_window_is_never_overdue() -> None:
    late = item(timedelta(hours=-1), late_due_at=NOW + timedelta(days=1))
    assert assess(late, NOW, tracked=False) == RiskLevel.CREEPING
    assert assess(late, NOW, tracked=True) == RiskLevel.OVERDUE


def test_levels_have_labels_and_order() -> None:
    assert [lvl.label for lvl in RiskLevel] == [
        "On track",
        "Creeping",
        "Did you submit?",
        "At risk",
        "Overdue",
    ]
    assert [lvl.slug for lvl in RiskLevel] == [
        "ok",
        "creeping",
        "unconfirmed",
        "at-risk",
        "overdue",
    ]
    severities = [lvl.severity for lvl in RiskLevel]
    assert severities == sorted(severities)
