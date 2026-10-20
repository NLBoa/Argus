"""The /workload curve (D065): per local day from today, the work-window time left and the work due,
each also as a running total, so the chart can show where work due outruns window time."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.clock import local_datetime, to_local
from argus.planner.windows import WindowSpan


@dataclass(frozen=True)
class CurveDay:
    start: datetime  # local midnight, as UTC
    window_hours: float  # window time left on this day
    due_hours: float  # remaining work due on this day
    window_total: float  # running totals from now
    due_total: float

    @property
    def shortfall(self) -> float:
        """Work due by the end of this day that window time so far can't cover."""
        return max(0.0, self.due_total - self.window_total)


def curve(
    spans: Sequence[WindowSpan],
    dues: Sequence[tuple[datetime, float]],
    now: datetime,
    *,
    days: int = 14,
) -> list[CurveDay]:
    """`dues` are (when the work must be done by, hours left); work already past is left out."""
    today = to_local(now).date()
    out: list[CurveDay] = []
    window_total = due_total = 0.0
    for n in range(days):
        start = local_datetime(today + timedelta(days=n), 0)
        end = local_datetime(today + timedelta(days=n + 1), 0)
        window = sum(s.hours_between(max(now, start), end) for s in spans)
        due = sum(h for at, h in dues if max(now, start) <= at < end)
        window_total += window
        due_total += due
        out.append(CurveDay(start, window, due, window_total, due_total))
    return out
