"""Risk levels for items.

An item is AT RISK when the free hours left before its due time are fewer than its remaining p80
hours (the spec rule). Those inputs come from the planner (Phase 4). Without them, risk uses only
time and status: open and past due is OVERDUE, and due within 24 hours but not started is WATCH.

An item no enabled source can report a submission for (D056) is never OVERDUE: once its deadline
passes it is UNCONFIRMED ("Did you submit?") until the student marks it done.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from argus.domain.models import Item, ItemStatus
from argus.domain.status import awaiting_confirmation
from argus.planner.windows import WindowSpan

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


# ---------------------------------------------------------------- load on window time (D059)


@dataclass(frozen=True, slots=True)
class Work:
    """One open item for the load calculation."""

    item_id: int
    due: datetime | None
    remaining: float  # E: hours still needed (unfloored; 0 means nothing left)
    counts: bool = True  # part of later items' demand (False: past its deadline, unconfirmed)


@dataclass(frozen=True, slots=True)
class Load:
    capacity: float  # C: window hours before the due time, minus reserved study and skips
    demand: float  # D: this item's remaining hours plus everything due at or before it
    remaining: float  # E

    @property
    def slack(self) -> float:
        return self.capacity - self.demand


def loads(
    work: Sequence[Work],
    now: datetime,
    spans: Sequence[WindowSpan],
    *,
    reserved: Mapping[str, float] | None = None,
    skips: Mapping[int, str] | None = None,
) -> dict[int, Load]:
    """C and D for every dated item (D059).

    C = for each window not skipped for this item, the part before its due time (and after now)
    minus the study time reserved in that window, never below zero.
    D = its own remaining hours + the remaining hours of every counted item due at or before it.
    """
    reserved = reserved or {}
    skips = skips or {}
    dated = [w for w in work if w.due is not None]
    out: dict[int, Load] = {}
    for w in dated:
        assert w.due is not None
        capacity = sum(
            max(0.0, s.hours_between(now, w.due) - reserved.get(s.window_id, 0.0))
            for s in spans
            if s.window_id != skips.get(w.item_id)
        )
        demand = w.remaining + sum(
            o.remaining
            for o in dated
            if o.item_id != w.item_id and o.counts and o.due is not None and o.due <= w.due
        )
        out[w.item_id] = Load(capacity, demand, w.remaining)
    return out
