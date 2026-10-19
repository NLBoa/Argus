"""Work windows (pure): when the student works, turned into real time (D057).

A window is a weekly wall-clock range on its start days (Mon-Thu 19:00-23:00). Each occurrence
gets a stable id, its local start date and time ("2026-10-19T19:00"), so reminders and progress
keyed by it survive class and commitment changes. Its *capacity* is the occurrence minus classes
(with the walking buffer) and fixed commitments. Quiet hours do not cut windows: the student
chose them. Times are real elapsed hours, so a window across a DST change is 5 or 3 hours long.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from argus.clock import local_datetime, to_local
from argus.config import Commitment, WorkWindowConfig
from argus.domain.models import MeetingRecord
from argus.planner.freetime import Interval, busy_on, merge, subtract

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True, slots=True)
class WorkWindow:
    days: frozenset[int]  # the days it starts on, Mon=0
    start: time
    end: time  # at or before start: runs past midnight

    @property
    def wraps(self) -> bool:
        return self.end <= self.start

    def describe(self, day: int) -> str:
        return f"{DAYS[day]} {self.start:%H:%M}-{self.end:%H:%M}"


@dataclass(frozen=True, slots=True)
class WindowSpan:
    """One occurrence of a work window."""

    window_id: str
    start: datetime
    end: datetime
    intervals: tuple[Interval, ...]  # the occurrence minus classes and commitments

    @property
    def capacity(self) -> float:
        return sum(i.hours for i in self.intervals)

    def hours_between(self, a: datetime, b: datetime) -> float:
        total = 0.0
        for iv in self.intervals:
            lo, hi = max(iv.start, a), min(iv.end, b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        return total


def window_id(day: date, start: time) -> str:
    return f"{day.isoformat()}T{start:%H:%M}"


def from_config(windows: Iterable[WorkWindowConfig]) -> list[WorkWindow]:
    return [WorkWindow(frozenset(DAYS.index(d) for d in w.days), w.start, w.end) for w in windows]


def overlapping_windows(windows: Sequence[WorkWindow]) -> list[str]:
    """Pairs that overlap within a week (they are merged; `check-config` warns)."""
    monday = date(2026, 1, 5)
    occurrences = []
    for w in windows:
        for day in sorted(w.days):
            d = monday + timedelta(days=day)
            s = datetime.combine(d, w.start)
            e = datetime.combine(d + timedelta(days=1) if w.wraps else d, w.end)
            occurrences.append((s, e, w.describe(day)))
    occurrences.sort()
    found = []
    for i, (_s1, e1, name1) in enumerate(occurrences):
        for s2, _e2, name2 in occurrences[i + 1 :]:
            if s2 >= e1:
                break
            found.append(f"{name1} overlaps {name2}")
    return found


def window_spans(
    start: datetime,
    end: datetime,
    windows: Sequence[WorkWindow],
    *,
    meetings: Iterable[MeetingRecord],
    commitments: Iterable[Commitment],
    buffer_minutes: int,
) -> list[WindowSpan]:
    """Every window occurrence that overlaps [start, end), in time order. Overlapping
    occurrences merge under the earlier one's id. Spans are not clipped to [start, end):
    use `hours_between` for the part inside a range."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    meetings, commitments = list(meetings), list(commitments)
    raw: list[tuple[datetime, datetime, str]] = []
    day = to_local(start).date() - timedelta(days=1)  # yesterday's window may run past midnight
    last = to_local(end).date()
    while day <= last:
        for w in sorted(windows, key=lambda w: w.start):
            if day.weekday() not in w.days:
                continue
            s = local_datetime(day, w.start.hour, w.start.minute)
            end_day = day + timedelta(days=1) if w.wraps else day
            e = local_datetime(end_day, w.end.hour, w.end.minute)
            if e > s:
                raw.append((s, e, window_id(day, w.start)))
        day += timedelta(days=1)
    raw.sort()
    merged: list[tuple[datetime, datetime, str]] = []
    for s, e, wid in raw:
        if merged and s < merged[-1][1]:
            ms, me, mid = merged[-1]
            merged[-1] = (ms, max(me, e), mid)
        else:
            merged.append((s, e, wid))
    pad = timedelta(minutes=buffer_minutes)
    out = []
    for s, e, wid in merged:
        if e <= start or s >= end:
            continue
        busy = []
        d = to_local(s).date()
        while d <= to_local(e).date():
            for b in busy_on(d, meetings, commitments):
                extra = pad if b.kind == "class" else timedelta(0)
                busy.append(Interval(b.start - extra, b.end + extra))
            d += timedelta(days=1)
        out.append(WindowSpan(wid, s, e, tuple(subtract([Interval(s, e)], merge(busy)))))
    return out


def hours_between(spans: Iterable[WindowSpan], a: datetime, b: datetime) -> float:
    """Window time in [a, b), in real hours: W(a, b)."""
    return sum(s.hours_between(a, b) for s in spans) if b > a else 0.0


def current_or_next(
    spans: Sequence[WindowSpan], now: datetime, *, within: timedelta
) -> WindowSpan | None:
    """The window now is inside, or else the next one starting within `within`."""
    for s in spans:
        if s.start <= now < s.end:
            return s
        if now < s.start <= now + within:
            return s
    return None
