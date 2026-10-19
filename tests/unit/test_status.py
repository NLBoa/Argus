from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.domain.models import Item, ItemKind, ItemLink, ItemStatus, Source
from argus.domain.status import StatusFacts, awaiting_confirmation, derive_status

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


# ---------------------------------------------------------------- untracked submissions (D056)
# Without a source that reports submissions (ELMS API, Gradescope), Argus cannot tell a
# submitted item from a missed one. It never claims "missed": the item stays open, awaiting
# the student's confirmation.


@pytest.mark.parametrize(
    ("f", "now", "expected"),
    [
        (facts(tracked=False), BEFORE, ItemStatus.UPCOMING),
        (facts(tracked=False), AFTER, ItemStatus.UPCOMING),
        (facts(tracked=False, started=True), AFTER, ItemStatus.IN_PROGRESS),
        (facts(tracked=False, user_done=True), AFTER, ItemStatus.SUBMITTED),
        (facts(tracked=False, submitted=False), AFTER, ItemStatus.UPCOMING),
        (facts(tracked=False, kind=ItemKind.EXAM), DUE, ItemStatus.SUBMITTED),
        (facts(tracked=True), AFTER, ItemStatus.MISSED),
    ],
)
def test_untracked_items_are_never_missed(
    f: StatusFacts, now: datetime, expected: ItemStatus
) -> None:
    assert derive_status(f, now) == expected


@given(
    kind=st.sampled_from(list(ItemKind)),
    due=st.one_of(st.none(), st.integers(-500, 500)),
    late=st.one_of(st.none(), st.integers(-500, 500)),
    submitted=st.sampled_from([None, False, True]),
    graded=st.sampled_from([None, False, True]),
    user_done=st.booleans(),
    started=st.booleans(),
    offset=st.integers(-1000, 1000),
)
def test_property_never_missed_without_tracking(
    kind: ItemKind,
    due: int | None,
    late: int | None,
    submitted: bool | None,
    graded: bool | None,
    user_done: bool,
    started: bool,
    offset: int,
) -> None:
    f = StatusFacts(
        kind=kind,
        due_at=DUE + timedelta(hours=due) if due is not None else None,
        late_due_at=DUE + timedelta(hours=late) if late is not None else None,
        submitted=submitted,
        graded=graded,
        user_done=user_done,
        started=started,
        tracked=False,
    )
    assert derive_status(f, DUE + timedelta(hours=offset)) != ItemStatus.MISSED


def item_(due: datetime | None, **kw: object) -> Item:
    base: dict[str, object] = {
        "id": 1,
        "course_code": "CMSC351",
        "kind": ItemKind.ASSIGNMENT,
        "title": "HW",
        "due_at": due,
        "status": ItemStatus.UPCOMING,
    }
    base.update(kw)
    return Item(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("it", "now", "tracked", "expected"),
    [
        (item_(DUE), AFTER, False, True),
        (item_(DUE), DUE, False, True),  # the due instant itself has passed
        (item_(DUE), BEFORE, False, False),
        (item_(DUE), AFTER, True, False),  # tracked: MISSED handles it
        (item_(DUE, status=ItemStatus.SUBMITTED), AFTER, False, False),
        (item_(DUE, status=ItemStatus.IN_PROGRESS), AFTER, False, True),
        (item_(DUE, kind=ItemKind.EXAM), AFTER, False, False),
        (item_(None), AFTER, False, False),
        (item_(DUE, late_due_at=DUE + timedelta(days=1)), AFTER, False, False),  # still workable
        (item_(DUE, late_due_at=DUE + timedelta(days=1)), DUE + timedelta(days=1), False, True),
    ],
)
def test_awaiting_confirmation(it: Item, now: datetime, tracked: bool, expected: bool) -> None:
    assert awaiting_confirmation(it, now, tracked=tracked) is expected


def test_tracked_by_present_sources() -> None:
    ical = ItemLink(Source.CANVAS_ICAL, "https://example.test/a")
    gs = ItemLink(Source.GRADESCOPE, None)
    assert not item_(DUE, links=(ical,)).tracked_by(frozenset({"gradescope"}))
    assert item_(DUE, links=(ical, gs)).tracked_by(frozenset({"gradescope"}))
    assert not item_(DUE, links=(ical, gs)).tracked_by(frozenset())
    assert not item_(DUE).tracked_by(frozenset({"canvas_api", "gradescope"}))
