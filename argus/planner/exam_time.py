"""Exam study time (pure): guaranteed time in the work windows before each exam (D062).

Each exam gets one "Study for <exam>" card. Its hours are the exam's calibrated p80, clamped to
1-6 h; progress is time logged on the exam item. What is left is spread evenly over the windows
that start in the week before the exam (and still have at least 30 minutes before it), at least
20 minutes per window, capped at half a window (three quarters in the last 48 hours), and never
more than three quarters of a window across all exams. This time is reserved first in each
window; homework fills the rest. What cannot fit is the shortfall, shown on the exam card.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from argus.planner.windows import WindowSpan

MIN_HOURS, MAX_HOURS = 1.0, 6.0
LEAD = timedelta(days=7)
MIN_WINDOW = 0.5  # hours a window needs before the exam to count
MIN_SLOT = 20 / 60
CAP, LATE_CAP, ALL_EXAMS_CAP = 0.5, 0.75, 0.75
LATE = timedelta(hours=48)


@dataclass(frozen=True, slots=True)
class ExamStudy:
    exam_id: int
    at: datetime
    p80: float  # calibrated p80 hours for the exam item
    logged: float  # hours logged on the exam item


@dataclass(frozen=True)
class StudyPlan:
    quota: dict[str, dict[int, float]] = field(default_factory=dict)  # window id -> exam -> hours
    shortfall: dict[int, float] = field(default_factory=dict)  # exam -> hours that don't fit
    total: dict[int, float] = field(default_factory=dict)  # exam -> clamped study hours

    def reserved(self) -> dict[str, float]:
        """Hours reserved per window, all exams together."""
        return {w: sum(q.values()) for w, q in self.quota.items()}


def _ceil_quarter(hours: float) -> float:
    return math.ceil(hours * 4 - 1e-9) / 4


def study_plan(exams: Sequence[ExamStudy], spans: Sequence[WindowSpan], now: datetime) -> StudyPlan:
    plan = StudyPlan()
    used: dict[str, float] = {}
    for e in sorted(exams, key=lambda e: (e.at, e.exam_id)):
        total = min(MAX_HOURS, max(MIN_HOURS, e.p80))
        plan.total[e.exam_id] = total
        left = max(0.0, total - e.logged)
        if left <= 0 or e.at <= now:
            continue  # done or past
        eligible = [
            s
            for s in spans
            if e.at - LEAD <= s.start < e.at
            and s.end > now
            and s.hours_between(now, e.at) >= MIN_WINDOW
        ]
        if not eligible:
            plan.shortfall[e.exam_id] = left
            continue
        for n, s in enumerate(eligible):
            if left <= 1e-9:
                break
            # Spread what is left over the windows still ahead, so time a capped window can't
            # take moves to the later ones.
            per = max(MIN_SLOT, _ceil_quarter(left / (len(eligible) - n)))
            avail = s.hours_between(now, e.at)
            cap = (LATE_CAP if e.at - s.start <= LATE else CAP) * avail
            room = ALL_EXAMS_CAP * avail - used.get(s.window_id, 0.0)
            q = min(per, cap, room, left)
            if q <= 1e-9:
                continue
            plan.quota.setdefault(s.window_id, {})[e.exam_id] = q
            used[s.window_id] = used.get(s.window_id, 0.0) + q
            left -= q
        if left > 1e-9:
            plan.shortfall[e.exam_id] = left
    return plan
