"""Risk levels for items.

An item is AT RISK when the free hours left before its due time are fewer than its remaining p80
hours (the spec rule). Those inputs come from the planner (Phase 4). Without them, risk uses only
time and status: open and past due is OVERDUE, and due within 24 hours but not started is WATCH.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from argus.domain.models import Item, ItemStatus

WATCH_WINDOW = timedelta(hours=24)
WATCH_MARGIN = 1.5  # free time under 1.5x the remaining work: watch


class RiskLevel(Enum):
    OK = ("ok", "On track", 0)
    WATCH = ("watch", "Watch", 1)
    AT_RISK = ("at-risk", "At risk", 2)
    OVERDUE = ("overdue", "Overdue", 3)

    def __init__(self, slug: str, label: str, severity: int) -> None:
        self.slug = slug
        self.label = label
        self.severity = severity


def assess(
    item: Item,
    now: datetime,
    *,
    remaining_p80_hours: float | None = None,
    free_hours_before_due: float | None = None,
) -> RiskLevel:
    if item.status == ItemStatus.MISSED:
        return RiskLevel.OVERDUE
    if not item.status.is_open or item.due_at is None or not item.kind.takes_submission:
        return RiskLevel.OK
    if now >= item.due_at:
        return RiskLevel.OVERDUE
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
