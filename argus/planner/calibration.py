"""Calibration (pure): learn how this student's actual time compares to estimates.

Per (course, kind): m = (n * r_bar + k) / (n + k) with k = 3, where r_bar is the mean of
actual / p50 over n finished items. With no history m = 1; as history grows, m moves from 1
toward r_bar. The calibrated estimate is m * p50 (and m * p80). A user override always wins.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean

from argus.domain.models import ItemKind

K = 3
DEFAULT_HOURS = {
    ItemKind.QUIZ: 1.0,
    ItemKind.ASSIGNMENT: 3.0,
    ItemKind.EXAM: 6.0,
    ItemKind.EVENT: 1.0,
}
DEFAULT_P80_RATIO = 1.5


@dataclass(frozen=True, slots=True)
class Finished:
    course_code: str
    kind: ItemKind
    p50: float  # the estimate it had (0 if none)
    actual: float  # hours logged


@dataclass(frozen=True, slots=True)
class Factor:
    m: float
    n: int
    r_bar: float


@dataclass(frozen=True, slots=True)
class Effective:
    p50: float
    p80: float
    raw_p50: float
    raw_p80: float
    overridden: bool
    factor: float


def factor(history: Sequence[Finished], course_code: str, kind: ItemKind) -> Factor:
    ratios = [
        h.actual / h.p50
        for h in history
        if h.course_code == course_code and h.kind == kind and h.p50 > 0 and h.actual > 0
    ]
    n = len(ratios)
    r_bar = mean(ratios) if ratios else 1.0
    return Factor(m=(n * r_bar + K) / (n + K), n=n, r_bar=r_bar)


def calibrate(*, raw_p50: float, raw_p80: float, f: Factor, override: float | None) -> Effective:
    if override is not None:
        return Effective(override, override, raw_p50, raw_p80, overridden=True, factor=f.m)
    return Effective(f.m * raw_p50, f.m * raw_p80, raw_p50, raw_p80, overridden=False, factor=f.m)
