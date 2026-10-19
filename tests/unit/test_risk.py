from datetime import UTC, datetime, timedelta

import pytest

from argus.domain.models import Item, ItemKind, ItemStatus
from argus.planner.risk import RiskLevel, assess

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


@pytest.mark.parametrize(
    ("it", "expected"),
    [
        (item(timedelta(days=3)), RiskLevel.OK),
        (item(timedelta(hours=20)), RiskLevel.WATCH),
        (item(timedelta(hours=20), ItemStatus.IN_PROGRESS), RiskLevel.OK),
        (item(timedelta(hours=-1)), RiskLevel.OVERDUE),
        (item(timedelta(hours=-1), ItemStatus.MISSED), RiskLevel.OVERDUE),
        (item(timedelta(hours=-1), ItemStatus.SUBMITTED), RiskLevel.OK),
        (item(timedelta(hours=-1), ItemStatus.GRADED), RiskLevel.OK),
        (item(None), RiskLevel.OK),
        (item(timedelta(hours=2), kind=ItemKind.EXAM), RiskLevel.OK),
    ],
)
def test_assess_without_plan(it: Item, expected: RiskLevel) -> None:
    assert assess(it, NOW) == expected


@pytest.mark.parametrize(
    ("remaining", "free", "expected"),
    [
        (5.0, 4.0, RiskLevel.AT_RISK),
        (5.0, 7.0, RiskLevel.WATCH),
        (5.0, 7.5, RiskLevel.OK),
        (0.0, 0.0, RiskLevel.OK),
    ],
)
def test_assess_with_plan(remaining: float, free: float, expected: RiskLevel) -> None:
    it = item(timedelta(days=3))
    assert assess(it, NOW, remaining_p80_hours=remaining, free_hours_before_due=free) == expected


def test_overdue_beats_plan() -> None:
    assert (
        assess(item(timedelta(hours=-1)), NOW, remaining_p80_hours=1, free_hours_before_due=10)
        == RiskLevel.OVERDUE
    )


def test_levels_have_labels_and_order() -> None:
    assert [lvl.label for lvl in RiskLevel] == ["On track", "Watch", "At risk", "Overdue"]
    assert (
        RiskLevel.OVERDUE.severity
        > RiskLevel.AT_RISK.severity
        > RiskLevel.WATCH.severity
        > RiskLevel.OK.severity
    )
