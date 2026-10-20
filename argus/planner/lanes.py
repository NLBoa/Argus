"""Layout for the day strip (Today) and the rail (kiosk), D063. Pure.

Blocks that overlap in time go side by side: columns on the kiosk rail, rows on the strip.
`assign_lanes` is the greedy coloring of an interval graph: taken in start order, each block gets
the lowest lane that is free, which uses exactly as many lanes as the most blocks overlapping at
one instant. Intervals are half-open, so a block ending at 10:00 and one starting then share a lane.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from argus.clock import ensure_utc, to_local


def assign_lanes(spans: Sequence[tuple[datetime, datetime]]) -> tuple[list[int], int]:
    """The lane of each span (in input order) and the number of lanes used."""
    for start, end in spans:
        if end < start:
            raise ValueError(f"span ends before it starts: {start} > {end}")
    lanes = [0] * len(spans)
    free_at: list[datetime] = []  # per lane: when its last block ends
    for i in sorted(range(len(spans)), key=lambda k: (spans[k][0], spans[k][1], k)):
        start, end = spans[i]
        lane = next((n for n, t in enumerate(free_at) if t <= start), len(free_at))
        if lane == len(free_at):
            free_at.append(end)
        else:
            free_at[lane] = end
        lanes[i] = lane
    return lanes, len(free_at)


def hour_ticks(start: datetime, end: datetime) -> list[datetime]:
    """Each whole local hour in [start, end), as UTC instants an hour apart. On a DST day the
    labels repeat or skip an hour, as the clock on the wall does."""
    local = to_local(ensure_utc(start))
    first = local.replace(minute=0, second=0, microsecond=0)
    t = first.astimezone(UTC)
    if t < start:
        t += timedelta(hours=1)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(hours=1)
    return out
