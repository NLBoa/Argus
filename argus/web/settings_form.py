"""Work-window settings (pure): read the Settings form, check it, and describe windows in words.

The form posts one row per window: `days_<i>` (any of 0-6, Mon=0), `every_<i>` ("Every day"),
`start_<i>` and `end_<i>` (HH:MM). Rows with nothing filled in are ignored, so an added blank row
that is never filled in is not an error.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time

from argus.config import MAX_WINDOWS_PER_DAY
from argus.planner.windows import DAYS, WorkWindow

ALL_DAYS = frozenset(range(7))


@dataclass(frozen=True)
class WindowRow:
    """One row of the form, as typed (so a mistake is shown back exactly as entered)."""

    days: frozenset[int] = frozenset()
    start: str = ""
    end: str = ""
    error: str | None = None

    @property
    def every_day(self) -> bool:
        return self.days == ALL_DAYS

    @property
    def blank(self) -> bool:
        return not self.days and not self.start and not self.end


@dataclass(frozen=True)
class FormResult:
    rows: list[WindowRow]
    windows: list[WorkWindow] = field(default_factory=list)
    error: str | None = None  # a problem that isn't about one row (too many on a day)

    @property
    def ok(self) -> bool:
        return self.error is None and not any(r.error for r in self.rows)


def _time(text: str) -> time | None:
    try:
        return time.fromisoformat(text.strip()) if text.strip() else None
    except ValueError:
        return None


def rows_from_windows(windows: Iterable[WorkWindow]) -> list[WindowRow]:
    return [
        WindowRow(w.days, f"{w.start:%H:%M}", f"{w.end:%H:%M}")
        for w in sorted(windows, key=lambda w: (min(w.days), w.start))
    ]


def read_rows(form: Mapping[str, Sequence[str]], count: int) -> list[WindowRow]:
    """The rows as posted. `form` maps a field to all its values (checkboxes repeat)."""
    rows = []
    for i in range(count):
        if form.get(f"every_{i}"):
            days = ALL_DAYS
        else:
            days = frozenset(
                int(d) for d in form.get(f"days_{i}", []) if d.isdigit() and int(d) < 7
            )
        start = (form.get(f"start_{i}") or [""])[0]
        end = (form.get(f"end_{i}") or [""])[0]
        rows.append(WindowRow(days, start, end))
    return rows


def check(rows: Sequence[WindowRow]) -> FormResult:
    """Each filled-in row needs a day, a start, and an end that differs from the start. No day
    may have more than three windows."""
    checked: list[WindowRow] = []
    windows: list[WorkWindow] = []
    for r in rows:
        if r.blank:
            continue
        start, end = _time(r.start), _time(r.end)
        problem = None
        if not r.days:
            problem = "Pick at least one day."
        elif start is None or end is None:
            problem = "Enter a start and an end time."
        elif start == end:
            problem = "The end time must differ from the start."
        checked.append(WindowRow(r.days, r.start, r.end, problem))
        if problem is None and start is not None and end is not None:
            windows.append(WorkWindow(r.days, start, end))
    too_many = [
        DAYS[d] for d in range(7) if sum(d in w.days for w in windows) > MAX_WINDOWS_PER_DAY
    ]
    error = (
        f"At most {MAX_WINDOWS_PER_DAY} times a day ({', '.join(too_many)} has more)."
        if too_many
        else None
    )
    return FormResult(checked, windows, error)


def _clock(t: time) -> str:
    hour = t.hour % 12 or 12
    minute = f":{t.minute:02d}" if t.minute else ""
    return f"{hour}{minute} {'am' if t.hour < 12 else 'pm'}"


def describe_days(days: frozenset[int]) -> str:
    """'Every day', 'Weekdays', 'Weekends', 'Mon-Thu', or 'Mon, Wed, Fri'."""
    if days == ALL_DAYS:
        return "Every day"
    if days == frozenset(range(5)):
        return "Weekdays"
    if days == frozenset({5, 6}):
        return "Weekends"
    ordered = sorted(days)
    if len(ordered) > 2 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{DAYS[ordered[0]]}-{DAYS[ordered[-1]]}"
    return ", ".join(DAYS[d] for d in ordered)


def describe(w: WorkWindow) -> str:
    """'Every day, 7 pm to 11 pm' (and 'to 1 am the next day' when it runs past midnight)."""
    end = _clock(w.end) + (" the next day" if w.wraps else "")
    return f"{describe_days(w.days)}, {_clock(w.start)} to {end}"
