"""The only place Argus reads the wall clock.

Every component takes a `Clock`. Production uses `SystemClock`; tests use `FakeClock`,
which makes DST, midnight, and quiet-hour behavior reproducible. Storage is always UTC;
`LOCAL_TZ` is for display and for rules phrased in local time (quiet hours, waking window).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")


class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A controllable clock for tests. Always returns aware UTC datetimes."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = ensure_utc(when)

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


def ensure_utc(dt: datetime) -> datetime:
    """Return `dt` in UTC. Naive datetimes are rejected: they are always a bug here."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(UTC)


def to_local(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(LOCAL_TZ)


def today_local(clock: Clock) -> date:
    return to_local(clock.now()).date()


def local_datetime(day: date, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time on `day` in America/New_York, as UTC.

    With fold=0 (PEP 495), a nonexistent time in the spring-forward gap maps forward
    (02:30 -> 03:30 EDT) and an ambiguous fall-back time resolves to its first
    occurrence (01:30 EDT, not EST).
    """
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ).astimezone(UTC)
