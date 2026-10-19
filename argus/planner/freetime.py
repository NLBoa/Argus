"""Busy time on a given local day: class meetings and fixed commitments, as UTC intervals.

Phase 4 extends this with walking buffers and free-time computation.
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
