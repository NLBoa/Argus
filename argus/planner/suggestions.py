""" "Done by" suggestions for the file adder (D074). Pure and deterministic.

For a course: before its next class (from the meetings), then the due times of its upcoming
open items. The form defaults to the first one, so the next class when there is one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.clock import ensure_utc, to_local
from argus.domain.models import Item, MeetingRecord
from argus.planner.freetime import meetings_on

ITEM_HORIZON = timedelta(days=30)
ITEM_LIMIT = 5


@dataclass(frozen=True, slots=True)
class BySuggestion:
    kind: str  # class | item
    label: str
    at: datetime  # UTC
    item_id: int | None = None


def next_class(course: str, meetings: Iterable[MeetingRecord], now: datetime) -> datetime | None:
    """When the course's next class starts, strictly after `now` (a class that has started is
    not "next"). Looks eight local days ahead, so a once-a-week class wraps to next week."""
    now = ensure_utc(now)
    mine = [m for m in meetings if m.course_code == course]
    if not mine:
        return None
    today = to_local(now).date()
    for offset in range(8):
        starts = [b.start for b in meetings_on(today + timedelta(days=offset), mine)]
        upcoming = [s for s in starts if s > now]
        if upcoming:
            return min(upcoming)
    return None


def by_suggestions(
    course: str,
    meetings: Iterable[MeetingRecord],
    items: Iterable[Item],
    now: datetime,
    *,
    limit: int = ITEM_LIMIT,
) -> list[BySuggestion]:
    """The next class first, then up to `limit` open items of the course due within
    ITEM_HORIZON, soonest first."""
    now = ensure_utc(now)
    out: list[BySuggestion] = []
    start = next_class(course, meetings, now)
    if start is not None:
        out.append(BySuggestion("class", f"Before next {course} class", start))
    due = sorted(
        (
            i
            for i in items
            if i.course_code == course
            and i.status.is_open
            and i.due_at is not None
            and now < i.due_at <= now + ITEM_HORIZON
        ),
        key=lambda i: (i.due_at, i.title),
    )
    for i in due[:limit]:
        assert i.due_at is not None
        out.append(BySuggestion("item", f"{i.title} due", i.due_at, item_id=i.id))
    return out


def default_by(suggestions: Sequence[BySuggestion]) -> datetime | None:
    """The form's starting "done by": the next class, else the soonest item, else nothing."""
    classes = [s.at for s in suggestions if s.kind == "class"]
    if classes:
        return classes[0]
    items = [s.at for s in suggestions]
    return min(items) if items else None
