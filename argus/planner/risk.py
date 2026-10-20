"""Risk levels for items (D059).

With work windows, an open item that takes a submission is judged on its load: C, the work-window
hours before it is due (minus reserved exam study and a skipped window), against D, its own
remaining hours plus the remaining hours of everything due at or before it.

- AT RISK: C < D (there is not enough window time for what is due by then).
- CREEPING: C < 1.5 x D (it fits, without much room).
- An item with nothing left (E = 0) is OK. Comparisons are strict.

Without work windows (or without an estimate), only time and status count: open and due within
24 hours but not started is CREEPING. Past due is OVERDUE, except that an item no enabled source
can report a submission for (D056) is never OVERDUE: once its deadline passes it is UNCONFIRMED
("Did you submit?"), and inside a late window it is CREEPING.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from argus.domain.models import Item, ItemStatus
from argus.domain.status import awaiting_confirmation
from argus.planner.windows import WindowSpan

SOON = timedelta(hours=24)  # time-only rule: due this soon and not started is creeping
MARGIN = 1.5  # window time under 1.5x the work due by then: creeping


class RiskLevel(Enum):
    OK = ("ok", "On track", 0)
    CREEPING = ("creeping", "Creeping", 1)
    UNCONFIRMED = ("unconfirmed", "Did you submit?", 2)
    AT_RISK = ("at-risk", "At risk", 3)
    OVERDUE = ("overdue", "Overdue", 4)

    def __init__(self, slug: str, label: str, severity: int) -> None:
        self.slug = slug
        self.label = label
        self.severity = severity


def assess(item: Item, now: datetime, *, tracked: bool, load: Load | None = None) -> RiskLevel:
    """`tracked`: some enabled source reports this item's submission (D056). Required, so no
    caller can silently fall back to "tracked" and call untracked work overdue. `load` is C and D
    from the work windows; None without windows or an estimate (the time-only rule)."""
    if awaiting_confirmation(item, now, tracked=tracked):
        return RiskLevel.UNCONFIRMED
    if item.status == ItemStatus.MISSED:
        return RiskLevel.OVERDUE
    if not item.status.is_open or item.due_at is None or not item.kind.takes_submission:
        return RiskLevel.OK
    if now >= item.due_at:
        # Untracked and inside a late window: still workable, but Argus can't say it's overdue.
        return RiskLevel.OVERDUE if tracked else RiskLevel.CREEPING
    if load is not None:
        if load.remaining <= 0:
            return RiskLevel.OK
        if load.capacity < load.demand:
            return RiskLevel.AT_RISK
        if load.capacity < MARGIN * load.demand:
            return RiskLevel.CREEPING
        return RiskLevel.OK
    if item.due_at - now <= SOON and item.status == ItemStatus.UPCOMING:
        return RiskLevel.CREEPING
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

    D = the earlier work that can still be done before this item's due time, plus this item's
    remaining hours and those of other counted items due at the same time. Walking the due dates
    in order, earlier work counts only up to the window time before its own deadline: work that
    can't fit by then is missed, and doesn't take time from later items (the deadline-
    feasibility test). Items due at the same time count each other.
    """
    reserved = reserved or {}
    skips = skips or {}
    dated = [w for w in work if w.due is not None]

    def window_time(due: datetime, skip: str | None = None) -> float:
        return sum(
            max(0.0, s.hours_between(now, due) - reserved.get(s.window_id, 0.0))
            for s in spans
            if s.window_id != skip
        )

    done_before: dict[datetime, float] = {}
    running = 0.0
    for due in sorted({w.due for w in dated if w.due is not None}):
        done_before[due] = running
        same = sum(w.remaining for w in dated if w.counts and w.due == due)
        running = min(running + same, window_time(due))
    out: dict[int, Load] = {}
    for w in dated:
        assert w.due is not None
        same = sum(
            o.remaining for o in dated if o.item_id != w.item_id and o.counts and o.due == w.due
        )
        demand = done_before[w.due] + same + w.remaining
        out[w.item_id] = Load(window_time(w.due, skips.get(w.item_id)), demand, w.remaining)
    return out
