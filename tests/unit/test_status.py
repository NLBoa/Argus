from datetime import UTC, datetime, timedelta

import pytest

from argus.domain.models import ItemKind, ItemStatus
from argus.domain.status import StatusFacts, derive_status

DUE = datetime(2026, 10, 23, 3, 59, tzinfo=UTC)
BEFORE = DUE - timedelta(hours=1)
AFTER = DUE + timedelta(minutes=1)


def facts(**kw: object) -> StatusFacts:
    base: dict[str, object] = {"kind": ItemKind.ASSIGNMENT, "due_at": DUE}
    base.update(kw)
    return StatusFacts(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("f", "now", "expected"),
    [
        (facts(), BEFORE, ItemStatus.UPCOMING),
        (facts(started=True), BEFORE, ItemStatus.IN_PROGRESS),
        (facts(submitted=True), BEFORE, ItemStatus.SUBMITTED),
        (facts(submitted=True, graded=True), AFTER, ItemStatus.GRADED),
        (facts(graded=True, user_done=False), BEFORE, ItemStatus.GRADED),
        (facts(user_done=True), AFTER, ItemStatus.SUBMITTED),
        (facts(), DUE - timedelta(microseconds=1), ItemStatus.UPCOMING),
        (facts(), DUE, ItemStatus.MISSED),  # the due instant itself is too late
        (facts(submitted=False), AFTER, ItemStatus.MISSED),
        (facts(submitted=None), AFTER, ItemStatus.MISSED),
        (facts(started=True), AFTER, ItemStatus.MISSED),
        # Inside the late window it is still workable, not missed.
        (facts(late_due_at=DUE + timedelta(days=1)), AFTER, ItemStatus.UPCOMING),
        (facts(late_due_at=DUE + timedelta(days=1), started=True), AFTER, ItemStatus.IN_PROGRESS),
        (facts(late_due_at=DUE + timedelta(days=1)), DUE + timedelta(days=1), ItemStatus.MISSED),
        # A late due before the due date is ignored.
        (facts(late_due_at=DUE - timedelta(days=1)), AFTER, ItemStatus.MISSED),
        # No due date: never missed.
        (facts(due_at=None), AFTER, ItemStatus.UPCOMING),
        (facts(due_at=None, started=True), AFTER, ItemStatus.IN_PROGRESS),
        # Exams and events take no submission: done once they start.
        (facts(kind=ItemKind.EXAM), BEFORE, ItemStatus.UPCOMING),
        (facts(kind=ItemKind.EXAM, started=True), BEFORE, ItemStatus.UPCOMING),
        (facts(kind=ItemKind.EXAM), DUE, ItemStatus.SUBMITTED),
        (facts(kind=ItemKind.EVENT), AFTER, ItemStatus.SUBMITTED),
        (facts(kind=ItemKind.EXAM, graded=True), AFTER, ItemStatus.GRADED),
        (facts(kind=ItemKind.QUIZ), AFTER, ItemStatus.MISSED),
    ],
)
def test_derive_status(f: StatusFacts, now: datetime, expected: ItemStatus) -> None:
    assert derive_status(f, now) == expected


def test_is_open() -> None:
    assert ItemStatus.UPCOMING in {s for s in ItemStatus if s.is_open}
    assert {s for s in ItemStatus if s.is_open} == {ItemStatus.UPCOMING, ItemStatus.IN_PROGRESS}
