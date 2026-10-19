"""Item status, derived purely from facts. It is recomputed every cycle, never stored by hand.

Order of precedence: graded > submitted (by a source or the user's DONE) > time-based.
An assignment or quiz is MISSED from its effective deadline on (the late-due time if later than
due, otherwise the due time; the due instant itself is too late). Unknown submission state counts
as not submitted, so deadline alerts stay safe. Exams and events take no submission: they count as
SUBMITTED ("done") once they start.

An item no configured source can report a submission for (D056: no ELMS API, no Gradescope) is
never MISSED: Argus cannot tell missed from submitted, so it stays open and, once its deadline
passes, is *awaiting confirmation* until the student marks it done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from argus.domain.models import Item, ItemKind, ItemStatus


@dataclass(frozen=True, slots=True)
class StatusFacts:
    kind: ItemKind
    due_at: datetime | None
    late_due_at: datetime | None = None
    submitted: bool | None = None
    graded: bool | None = None
    user_done: bool = False
    started: bool = False
    tracked: bool = True  # some enabled source reports this item's submission state (D056)

    @property
    def deadline(self) -> datetime | None:
        return effective_deadline(self.due_at, self.late_due_at)


def effective_deadline(due_at: datetime | None, late_due_at: datetime | None) -> datetime | None:
    """The late-due time if it is after the due time, otherwise the due time."""
    if due_at is None:
        return None
    if late_due_at is not None and late_due_at > due_at:
        return late_due_at
    return due_at


def derive_status(f: StatusFacts, now: datetime) -> ItemStatus:
    if f.graded:
        return ItemStatus.GRADED
    if f.submitted or f.user_done:
        return ItemStatus.SUBMITTED
    if not f.kind.takes_submission:
        if f.due_at is not None and now >= f.due_at:
            return ItemStatus.SUBMITTED
        return ItemStatus.UPCOMING
    deadline = f.deadline
    if f.tracked and deadline is not None and now >= deadline:
        return ItemStatus.MISSED
    return ItemStatus.IN_PROGRESS if f.started else ItemStatus.UPCOMING


def awaiting_confirmation(item: Item, now: datetime, *, tracked: bool) -> bool:
    """Past its deadline, still open, and no source can say whether it was submitted (D056)."""
    deadline = effective_deadline(item.due_at, item.late_due_at)
    return (
        not tracked
        and item.status.is_open
        and item.kind.takes_submission
        and deadline is not None
        and now >= deadline
    )
