"""Busy time and interval arithmetic. Busy: class meetings and fixed commitments on a local
day. Work windows (planner/windows.py) subtract busy time from the hours the student works.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from argus.clock import local_datetime
from argus.config import Commitment
from argus.domain.models import MeetingRecord

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True, slots=True)
class Busy:
    start: datetime
    end: datetime
    kind: str  # class | commitment
    title: str
    course_code: str | None = None
    where: str | None = None


def meetings_on(day: date, meetings: Iterable[MeetingRecord]) -> list[Busy]:
    out = []
    for m in meetings:
        if day.weekday() not in m.days:
            continue
        where = " ".join(x for x in (m.building, m.room) if x) or None
        out.append(
            Busy(
                start=local_datetime(day, m.start.hour, m.start.minute),
                end=local_datetime(day, m.end.hour, m.end.minute),
                kind="class",
                title=f"{m.course_code} {m.kind}",
                course_code=m.course_code,
                where=where,
            )
        )
    return sorted(out, key=lambda b: (b.start, b.title))


def busy_on(
    day: date, meetings: Iterable[MeetingRecord], commitments: Iterable[Commitment]
) -> list[Busy]:
    out = meetings_on(day, meetings)
    name = _DAY_NAMES[day.weekday()]
    for c in commitments:
        if name in c.days:
            out.append(
                Busy(
                    start=local_datetime(day, c.start.hour, c.start.minute),
                    end=local_datetime(day, c.end.hour, c.end.minute),
                    kind="commitment",
                    title=c.title,
                )
            )
    return sorted(out, key=lambda b: (b.start, b.title))


# ---------------------------------------------------------------- free time


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    start: datetime
    end: datetime

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for iv in sorted(i for i in intervals if i.end > i.start):
        if out and iv.start <= out[-1].end:
            out[-1] = Interval(out[-1].start, max(out[-1].end, iv.end))
        else:
            out.append(iv)
    return out


def subtract(base: Iterable[Interval], holes: Iterable[Interval]) -> list[Interval]:
    result = merge(base)
    for hole in merge(holes):
        nxt: list[Interval] = []
        for iv in result:
            if hole.end <= iv.start or hole.start >= iv.end:
                nxt.append(iv)
                continue
            if iv.start < hole.start:
                nxt.append(Interval(iv.start, hole.start))
            if hole.end < iv.end:
                nxt.append(Interval(hole.end, iv.end))
        result = nxt
    return result
