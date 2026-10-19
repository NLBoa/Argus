"""Item status, derived purely from facts. It is recomputed every cycle, never stored by hand.

Order of precedence: graded > submitted (by a source or the user's DONE) > time-based.
An assignment or quiz is MISSED from its effective deadline on (the late-due time if later than
due, otherwise the due time; the due instant itself is too late). Unknown submission state counts
as not submitted, so deadline alerts stay safe. Exams and events take no submission: they count as
SUBMITTED ("done") once they start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from argus.domain.models import ItemKind, ItemStatus


@dataclass(frozen=True, slots=True)
class StatusFacts:
    kind: ItemKind
    due_at: datetime | None
    late_due_at: datetime | None = None
    submitted: bool | None = None
    graded: bool | None = None
    user_done: bool = False
    started: bool = False

    @property
    def deadline(self) -> datetime | None:
        if self.due_at is None:
            return None
        if self.late_due_at is not None and self.late_due_at > self.due_at:
            return self.late_due_at
        return self.due_at


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
    if deadline is not None and now >= deadline:
        return ItemStatus.MISSED
    return ItemStatus.IN_PROGRESS if f.started else ItemStatus.UPCOMING
