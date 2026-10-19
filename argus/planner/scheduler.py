"""Work-session scheduler (pure, deterministic): earliest deadline first.

Each item's remaining work (calibrated p80 minus time already logged, rounded up to the
15-minute grid) is placed into the earliest free time before its due time, in chunks of
30 minutes to 2 hours with a 15-minute break after each chunk. Chunks are trimmed so no
remainder under 30 minutes is left over; only items needing under 30 minutes in total get
shorter sessions. Work that cannot fit is reported as unscheduled, which the risk rule uses.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argus.planner.freetime import Interval

GRID = timedelta(minutes=15)
MIN_CHUNK = timedelta(minutes=30)
MAX_CHUNK = timedelta(minutes=120)
BREAK = timedelta(minutes=15)
_ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PlanItem:
    item_id: int
    due_at: datetime | None
    remaining: timedelta


@dataclass(frozen=True, slots=True)
class Block:
    item_id: int
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Plan:
    blocks: tuple[Block, ...]
    unscheduled: dict[int, float]  # item_id -> hours that did not fit before the due time


def ceil_to_grid(dt: datetime) -> datetime:
    rem = (dt - _ANCHOR) % GRID
    return dt if rem == timedelta(0) else dt + (GRID - rem)


def _floor(dt: datetime) -> datetime:
    return dt - (dt - _ANCHOR) % GRID


def _round_up(td: timedelta) -> timedelta:
    return -(-td // GRID) * GRID


def schedule(
    items: Iterable[PlanItem], free: Sequence[Interval], *, max_chunk: timedelta = MAX_CHUNK
) -> Plan:
    segments = [
        [ceil_to_grid(i.start), _floor(i.end)]
        for i in sorted(free)
        if _floor(i.end) - ceil_to_grid(i.start) >= GRID
    ]
    order = sorted(items, key=lambda p: (p.due_at is None, p.due_at or _ANCHOR, p.item_id))
    blocks: list[Block] = []
    unscheduled: dict[int, float] = {}
    for it in order:
        need = _round_up(max(it.remaining, timedelta(0)))
        for seg in segments:
            while need > timedelta(0):
                limit = seg[1] if it.due_at is None else min(seg[1], _floor(it.due_at))
                length = limit - seg[0]
                if length < GRID:
                    break
                chunk = min(need, max_chunk, length)
                leftover = need - chunk
                if timedelta(0) < leftover < MIN_CHUNK:
                    if chunk - (MIN_CHUNK - leftover) < MIN_CHUNK:
                        break  # this gap would leave a sliver: look for a longer one
                    chunk -= MIN_CHUNK - leftover  # keep the remainder >= 30 min
                if chunk < MIN_CHUNK and need >= MIN_CHUNK:
                    break  # too small for real work: keep it for something shorter
                blocks.append(Block(it.item_id, seg[0], seg[0] + chunk))
                need -= chunk
                seg[0] = min(seg[1], seg[0] + chunk + BREAK)
            if need <= timedelta(0):
                break
        if need > timedelta(0):
            unscheduled[it.item_id] = need.total_seconds() / 3600
    return Plan(tuple(sorted(blocks, key=lambda b: (b.start, b.item_id))), unscheduled)
