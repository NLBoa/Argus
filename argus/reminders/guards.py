"""Guards (pure): whether a reminder goes out now, waits, or is dropped.

Order: muted rule -> drop; expired -> drop; quiet hours or class time -> defer (unless the
reminder bypasses them: only the 3-hour warning); daily cap reached -> defer (at-risk and
3-hour warnings are not counted). Deferral is stateless: the rule fires again next tick
while its condition holds, and expires on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from argus.clock import to_local
from argus.config import TimeWindow
from argus.planner.freetime import Busy
from argus.reminders.rules import Reminder


class Verdict(Enum):
    SEND = "send"
    DEFER = "defer"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class GuardContext:
    quiet_hours: TimeWindow
    busy: Sequence[Busy]  # class meetings around now
    sent_today_capped: int  # sent today (local) that count toward the cap
    daily_cap: int
    muted: frozenset[str] = frozenset()


def check(reminder: Reminder, ctx: GuardContext, now: datetime) -> tuple[Verdict, str]:
    if reminder.rule.value in ctx.muted:
        return Verdict.DROP, "rule muted"
    if reminder.expires_at is not None and now >= reminder.expires_at:
        return Verdict.DROP, "expired"
    if not reminder.bypass_quiet:
        if ctx.quiet_hours.contains(to_local(now).time()):
            return Verdict.DEFER, "quiet hours"
        if any(b.kind == "class" and b.start <= now < b.end for b in ctx.busy):
            return Verdict.DEFER, "in class"
    if reminder.counts_toward_cap and ctx.sent_today_capped >= ctx.daily_cap:
        return Verdict.DEFER, "daily cap reached"
    return Verdict.SEND, "ok"
