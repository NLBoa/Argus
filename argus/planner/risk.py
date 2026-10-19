"""Risk levels for items.

An item is AT RISK when the free hours left before its due time are fewer than its remaining p80
hours (the spec rule). Those inputs come from the planner (Phase 4). Without them, risk uses only
time and status: open and past due is OVERDUE, and due within 24 hours but not started is WATCH.

An item no enabled source can report a submission for (D056) is never OVERDUE: once its deadline
passes it is UNCONFIRMED ("Did you submit?") until the student marks it done.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from argus.domain.models import Item, ItemStatus
from argus.domain.status import awaiting_confirmation

WATCH_WINDOW = timedelta(hours=24)
WATCH_MARGIN = 1.5  # free time under 1.5x the remaining work: watch


class RiskLevel(Enum):
    OK = ("ok", "On track", 0)
    WATCH = ("watch", "Watch", 1)
    UNCONFIRMED = ("unconfirmed", "Did you submit?", 2)
    AT_RISK = ("at-risk", "At risk", 3)
    OVERDUE = ("overdue", "Overdue", 4)

    def __init__(self, slug: str, label: str, severity: int) -> None:
        self.slug = slug
        self.label = label
        self.severity = severity


def assess(
    item: Item,
    now: datetime,
    *,
    tracked: bool,
    remaining_p80_hours: float | None = None,
    free_hours_before_due: float | None = None,
) -> RiskLevel:
    """`tracked`: some enabled source reports this item's submission (D056). Required, so no
    caller can silently fall back to "tracked" and call untracked work overdue."""
    if awaiting_confirmation(item, now, tracked=tracked):
        return RiskLevel.UNCONFIRMED
    if item.status == ItemStatus.MISSED:
        return RiskLevel.OVERDUE
    if not item.status.is_open or item.due_at is None or not item.kind.takes_submission:
        return RiskLevel.OK
    if now >= item.due_at:
        # Untracked and inside a late window: still workable, but Argus can't say it's overdue.
        return RiskLevel.OVERDUE if tracked else RiskLevel.WATCH
    if remaining_p80_hours is not None and free_hours_before_due is not None:
        if remaining_p80_hours <= 0:
            return RiskLevel.OK
        if free_hours_before_due < remaining_p80_hours:
            return RiskLevel.AT_RISK
        if free_hours_before_due < WATCH_MARGIN * remaining_p80_hours:
            return RiskLevel.WATCH
        return RiskLevel.OK
    if item.due_at - now <= WATCH_WINDOW and item.status == ItemStatus.UPCOMING:
        return RiskLevel.WATCH
    return RiskLevel.OK
