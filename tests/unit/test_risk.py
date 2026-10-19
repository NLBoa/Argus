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
    assert assess(it, NOW, tracked=True) == expected


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
    assert (
        assess(it, NOW, tracked=True, remaining_p80_hours=remaining, free_hours_before_due=free)
        == expected
    )


def test_overdue_beats_plan() -> None:
    assert (
        assess(
            item(timedelta(hours=-1)),
            NOW,
            tracked=True,
            remaining_p80_hours=1,
            free_hours_before_due=10,
        )
        == RiskLevel.OVERDUE
    )


def test_levels_have_labels_and_order() -> None:
    assert [lvl.label for lvl in RiskLevel] == [
        "On track",
        "Watch",
        "Did you submit?",
        "At risk",
        "Overdue",
    ]
    assert (
        RiskLevel.OVERDUE.severity
        > RiskLevel.AT_RISK.severity
        > RiskLevel.WATCH.severity
        > RiskLevel.OK.severity
    )


# ---------------------------------------------------------------- untracked submissions (D056)


def test_untracked_past_due_is_unconfirmed_not_overdue() -> None:
    it = item(timedelta(hours=-1))
    assert assess(it, NOW, tracked=False) == RiskLevel.UNCONFIRMED
    assert assess(it, NOW, tracked=True) == RiskLevel.OVERDUE
    # Not yet due: the usual rules apply.
    assert assess(item(timedelta(hours=20)), NOW, tracked=False) == RiskLevel.WATCH
    assert assess(item(timedelta(hours=-1), ItemStatus.SUBMITTED), NOW, tracked=False) == (
        RiskLevel.OK
    )


def test_unconfirmed_label() -> None:
    assert RiskLevel.UNCONFIRMED.label == "Did you submit?"
    assert RiskLevel.UNCONFIRMED.slug == "unconfirmed"
    assert RiskLevel.OK.severity < RiskLevel.UNCONFIRMED.severity < RiskLevel.AT_RISK.severity


def test_untracked_in_a_late_window_is_never_overdue() -> None:
    """Past due but before the late deadline: still workable, and Argus can't tell it was
    missed, so the strongest marker is Watch (D056)."""
    late = item(timedelta(hours=-1), late_due_at=NOW + timedelta(days=1))
    assert assess(late, NOW, tracked=False) == RiskLevel.WATCH
    assert assess(late, NOW, tracked=True) == RiskLevel.OVERDUE
