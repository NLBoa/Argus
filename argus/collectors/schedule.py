"""Polling policy (pure): which sources are due, given when each was last attempted.

Sub-daily polls use UTC-aligned slots with a per-source offset, so sources are staggered
(at most one burst per source per slot). DST shifts are whole hours, so 30-minute slots are
unaffected. Weekly and nightly polls are anchored to local wall-clock time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from argus.clock import local_datetime, to_local
from argus.domain.models import Source, Weekday

_EPOCH = datetime(2000, 1, 3, tzinfo=UTC)  # a Monday at 00:00 UTC


@dataclass(frozen=True, slots=True)
class Every:
    interval: timedelta
    offset: timedelta = timedelta(0)

    def last_slot(self, now: datetime) -> datetime:
        elapsed = now - _EPOCH - self.offset
        slots = elapsed // self.interval
        return _EPOCH + self.offset + slots * self.interval


@dataclass(frozen=True, slots=True)
class DailyLocal:
    at: time

    def last_slot(self, now: datetime) -> datetime:
        day = to_local(now).date()
        slot = local_datetime(day, self.at.hour, self.at.minute)
        if slot > now:
            slot = local_datetime(day - timedelta(days=1), self.at.hour, self.at.minute)
        return slot


@dataclass(frozen=True, slots=True)
class WeeklyLocal:
    weekday: Weekday
    at: time

    def last_slot(self, now: datetime) -> datetime:
        local = to_local(now)
        back = (local.weekday() - self.weekday) % 7
        day = local.date() - timedelta(days=back)
        slot = local_datetime(day, self.at.hour, self.at.minute)
        if slot > now:
            slot = local_datetime(day - timedelta(days=7), self.at.hour, self.at.minute)
        return slot


Policy = Every | DailyLocal | WeeklyLocal

HALF_HOUR = timedelta(minutes=30)
POLICIES: Mapping[Source, Policy] = {
    Source.CANVAS_ICAL: Every(HALF_HOUR, timedelta(minutes=0)),
    Source.GRADESCOPE: Every(HALF_HOUR, timedelta(minutes=6)),
    Source.PIAZZA: Every(HALF_HOUR, timedelta(minutes=12)),
    Source.PIAZZA_EMAIL: Every(HALF_HOUR, timedelta(minutes=18)),
    Source.CANVAS_API: Every(HALF_HOUR, timedelta(minutes=24)),
    Source.UMDIO: WeeklyLocal(Weekday.SUN, time(4, 0)),
}


def is_due(policy: Policy, now: datetime, last_attempt: datetime | None) -> bool:
    return last_attempt is None or last_attempt < policy.last_slot(now)


def due_sources(
    now: datetime,
    last_attempts: Mapping[Source, datetime | None],
    sources: Iterable[Source],
    policies: Mapping[Source, Policy] = POLICIES,
) -> list[Source]:
    return [s for s in sources if s in policies and is_due(policies[s], now, last_attempts.get(s))]


def next_slot(policy: Policy, now: datetime) -> datetime:
    """The first slot strictly after `now` (for 'next sync' displays)."""
    probe = now
    step = timedelta(minutes=1)
    current = policy.last_slot(now)
    while True:
        probe += step
        slot = policy.last_slot(probe)
        if slot != current and slot > now:
            return slot
