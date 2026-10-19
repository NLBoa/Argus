"""Display formatting (local time, relative time, hours). Pure; used by view models and Jinja."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from argus.clock import to_local


def clock_time(dt: datetime) -> str:
    """'11:30 pm', '9 am', '12 pm'."""
    local = to_local(dt)
    hour = local.hour % 12 or 12
    suffix = "am" if local.hour < 12 else "pm"
    return f"{hour}:{local.minute:02d} {suffix}" if local.minute else f"{hour} {suffix}"


def day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if today < day < today + timedelta(days=7):
        return f"{day:%A}"
    return f"{day:%a, %b} {day.day}"


def long_date(day: date) -> str:
    return f"{day:%A, %B} {day.day}"


def due_text(dt: datetime, now: datetime) -> str:
    """'Today 11:30 pm', 'Thu 11:30 pm', 'Oct 30, 11:59 pm'."""
    local, today = to_local(dt), to_local(now).date()
    d = local.date()
    if d == today:
        prefix = "Today"
    elif d == today + timedelta(days=1):
        prefix = "Tomorrow"
    elif today < d < today + timedelta(days=7):
        prefix = f"{local:%a}"
    else:
        prefix = f"{local:%b} {local.day},"
    return f"{prefix} {clock_time(dt)}"


def _span(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours = seconds / 3600
    if hours < 36:
        return f"{round(hours)}h"
    return f"{round(hours / 24)} days"


def relative(dt: datetime, now: datetime) -> str:
    """'in 14h', '3h ago', 'just now', 'in 2 days'."""
    delta = (dt - now).total_seconds()
    if abs(delta) < 45:
        return "just now"
    return f"in {_span(delta)}" if delta > 0 else f"{_span(-delta)} ago"


def hours(h: float | None) -> str:
    if h is None:
        return "-"
    if h < 1:
        return f"{max(5, round(h * 60 / 5) * 5)} min"
    return f"{h:.1f}".rstrip("0").rstrip(".") + " h"


def elapsed(seconds: float) -> str:
    """'0:42', '1:05:09'."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
