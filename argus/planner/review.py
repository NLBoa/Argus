"""Exam review (pure): readiness per concept and a spaced, interleaved review schedule.

Practice testing and distributed practice are the most effective study strategies
(Dunlosky et al., 2013), and interleaving helps too. So review sessions are spread over
the days before an exam (7, 5, 3, 2, 1 days out), each covers several concepts mixed across
modules, and the weakest concepts come first and come back most often.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from itertools import zip_longest

from argus.clock import local_datetime, to_local
from argus.planner.freetime import Interval
from argus.planner.scheduler import Block, ceil_to_grid

REVIEW_DAYS = (7, 5, 3, 2, 1)
SESSION = timedelta(minutes=45)
PER_SESSION = 3


class Readiness(StrEnum):
    UNTESTED = "untested"
    SHAKY = "shaky"
    SOLID = "solid"


@dataclass(frozen=True, slots=True)
class Attempt:
    item_id: int
    result: str  # correct | shaky | incorrect
    at: datetime


def readiness(item_ids: Iterable[int], attempts: Iterable[Attempt]) -> Readiness:
    """Solid: the latest attempt on every attempted item was correct, and at least half the
    items were attempted. Shaky: any latest attempt was wrong or unsure, or too few tried."""
    items = set(item_ids)
    latest: dict[int, Attempt] = {}
    for a in sorted(attempts, key=lambda a: a.at):
        if a.item_id in items:
            latest[a.item_id] = a
    if not latest:
        return Readiness.UNTESTED
    if any(a.result != "correct" for a in latest.values()):
        return Readiness.SHAKY
    if len(latest) * 2 < len(items):
        return Readiness.SHAKY
    return Readiness.SOLID


@dataclass(frozen=True, slots=True)
class ReviewConcept:
    id: int
    name: str
    module_id: int | None
    readiness: Readiness
    priority: float  # raised by confusion inbox entries


_RANK = {Readiness.SHAKY: 0, Readiness.UNTESTED: 1, Readiness.SOLID: 2}


def review_order(concepts: Sequence[ReviewConcept]) -> list[ReviewConcept]:
    """Weakest first, then interleaved: round-robin across modules."""
    ranked = sorted(concepts, key=lambda c: (_RANK[c.readiness], -c.priority, c.name, c.id))
    by_module: dict[int | None, list[ReviewConcept]] = {}
    for c in ranked:
        by_module.setdefault(c.module_id, []).append(c)
    lanes = sorted(
        by_module.values(),
        key=lambda lane: (_RANK[lane[0].readiness], -lane[0].priority, lane[0].name),
    )
    out: list[ReviewConcept] = []
    for tier in sorted({_RANK[c.readiness] for c in ranked}):
        tier_lanes = [[c for c in lane if _RANK[c.readiness] == tier] for lane in lanes]
        for row in zip_longest(*tier_lanes):
            out.extend(c for c in row if c is not None)
    return out


@dataclass(frozen=True, slots=True)
class ReviewSession:
    day: date
    concepts: tuple[ReviewConcept, ...]


def review_sessions(
    exam_at: datetime, now: datetime, concepts: Sequence[ReviewConcept]
) -> list[ReviewSession]:
    ordered = review_order(concepts)
    if not ordered:
        return []
    exam_day, today = to_local(exam_at).date(), to_local(now).date()
    days = [
        exam_day - timedelta(days=d) for d in REVIEW_DAYS if exam_day - timedelta(days=d) >= today
    ]
    sessions = []
    for k, day in enumerate(days):
        picks = tuple(
            ordered[(k * PER_SESSION + j) % len(ordered)]
            for j in range(min(PER_SESSION, len(ordered)))
        )
        sessions.append(ReviewSession(day, picks))
    return sessions


def place_reviews(
    sessions: Sequence[ReviewSession], free: Sequence[Interval], length: timedelta = SESSION
) -> list[Block]:
    """Each session goes in the first free slot of its day that fits."""
    blocks = []
    for s in sessions:
        start_of_day, end_of_day = (
            local_datetime(s.day, 0),
            local_datetime(s.day + timedelta(days=1), 0),
        )
        for iv in sorted(free):
            lo = ceil_to_grid(max(iv.start, start_of_day))
            hi = min(iv.end, end_of_day)
            if hi - lo >= length:
                blocks.append(Block(-1, lo, lo + length))
                break
    return blocks
