"""Read-only views of Argus data for the Claude connector (the MCP server, D073).

Deadlines and links only: never grades, scores, points, descriptions, or submission contents.
Times go out as ISO 8601 with the America/New_York offset. The queries take a connection and
never write; the server opens that connection read-only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argus.clock import ensure_utc, to_local
from argus.domain.changes import summarize_changes
from argus.domain.models import Item, ItemKind, ItemStatus, MeetingRecord, Source
from argus.domain.titles import is_project
from argus.store import repo

ItemType = Literal["assignment", "quiz", "exam", "project", "event", "slides", "file"]
SourceName = Literal["canvas", "gradescope", "piazza", "manual"]

MAX_DAYS = 60
OVERDUE_LOOKBACK = timedelta(days=7)
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Which record supplies the link and source when the due date's own source has none.
_LINK_ORDER = (
    Source.CANVAS_API,
    Source.CANVAS_ICAL,
    Source.GRADESCOPE,
    Source.FINDING,
    Source.MANUAL,
)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class WorkItem(_Out):
    id: str = Field(description="Stable Argus item id; use it to match items across calls.")
    course: str = Field(description="Course code, e.g. CMSC351.")
    title: str
    type: ItemType = Field(
        description="assignment, quiz, exam, project, or event (an event's due is its start)."
    )
    due: str | None = Field(description="ISO 8601 with the America/New_York offset, or null.")
    link: str | None = Field(description="Where the work lives (Canvas, Gradescope, or the post).")
    source: SourceName = Field(description="Where the due date comes from.")
    updated_at: str = Field(description="When Argus last saw this item created or its date change.")
    done: bool = Field(description="Submitted, or marked done in Argus.")
    overdue: bool = Field(description="Past due and not done.")


class ChangedItem(WorkItem):
    status: Literal["new", "changed", "removed"]
    old_due: str | None = Field(
        default=None, description="For changed: the due date before `since` (null if it had none)."
    )


class Meeting(_Out):
    kind: str = Field(description="lecture, discussion, or lab.")
    days: list[str] = Field(description="Weekday names, e.g. ['Mon', 'Wed'].")
    start: str = Field(description="Local start time HH:MM (America/New_York).")
    end: str = Field(description="Local end time HH:MM (America/New_York).")
    location: str | None


class Course(_Out):
    code: str
    name: str | None
    meetings: list[Meeting]


class CourseList(_Out):
    courses: list[Course]


class UpcomingWork(_Out):
    as_of: str = Field(description="The time this answer was computed (America/New_York).")
    days: int
    items: list[WorkItem]


class Changes(_Out):
    as_of: str = Field(description="Pass this as `since` next time to get only newer changes.")
    since: str
    changes: list[ChangedItem]


# ---------------------------------------------------------------- mapping (pure)


def local_iso(dt: datetime | None) -> str | None:
    return to_local(dt).isoformat(timespec="seconds") if dt is not None else None


def item_type(kind: ItemKind, title: str) -> ItemType:
    if kind == ItemKind.ASSIGNMENT:
        return "project" if is_project(title) else "assignment"
    return kind.value  # type: ignore[return-value]  # quiz, exam, event


def is_done(item: Item, now: datetime) -> bool:
    if item.status in (ItemStatus.SUBMITTED, ItemStatus.GRADED):
        return True
    # An exam or event is over once it starts (statuses refresh each cycle; this closes the gap).
    return not item.kind.takes_submission and item.due_at is not None and item.due_at <= now


def _source_name(source: Source, finding_platform: str | None) -> SourceName:
    if source.family == "canvas":
        return "canvas"
    if source == Source.GRADESCOPE:
        return "gradescope"
    if source == Source.FINDING:
        return "canvas" if finding_platform == "canvas" else "piazza"
    if source.family == "piazza":
        return "piazza"
    return "manual"


def origin_and_link(item: Item) -> tuple[Source, str | None]:
    """The record the item is reported from, and its link.

    An official due source (Canvas, Gradescope) wins. Otherwise the first record in
    `_LINK_ORDER`, so a Canvas item with a student-confirmed date still reports Canvas."""
    ranked = sorted(
        item.links,
        key=lambda link: (
            _LINK_ORDER.index(link.source) if link.source in _LINK_ORDER else len(_LINK_ORDER)
        ),
    )
    if item.due_source is not None and item.due_source.is_official:
        origin = item.due_source
    elif ranked:
        origin = ranked[0].source
    else:
        origin = item.due_source or Source.MANUAL
    same = [link.url for link in ranked if link.source.family == origin.family and link.url]
    anywhere = [link.url for link in ranked if link.url]
    urls = same or anywhere
    return origin, urls[0] if urls else None


def to_work_item(
    item: Item,
    now: datetime,
    *,
    updated_at: datetime | None,
    finding_platform: str | None = None,
) -> WorkItem:
    origin, link = origin_and_link(item)
    done = is_done(item, now)
    stamp = updated_at or item.created_at or now
    return WorkItem(
        id=str(item.id),
        course=item.course_code,
        title=item.title,
        type=item_type(item.kind, item.title),
        due=local_iso(item.due_at),
        link=link,
        source=_source_name(origin, finding_platform),
        updated_at=local_iso(stamp) or "",
        done=done,
        overdue=not done and item.due_at is not None and item.due_at < now,
    )


def meeting_out(m: MeetingRecord) -> Meeting:
    where = " ".join(p for p in (m.building, m.room) if p) or None
    return Meeting(
        kind=m.kind,
        days=[_DAY_NAMES[int(d)] for d in m.days],
        start=m.start.strftime("%H:%M"),
        end=m.end.strftime("%H:%M"),
        location=where,
    )


def parse_since(text: str) -> datetime:
    """An ISO 8601 instant with an offset (or Z). Naive times are refused: ambiguous."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"since must be ISO 8601, e.g. 2026-11-06T07:00:00-05:00: {exc}") from None
    try:
        return ensure_utc(parsed)
    except ValueError:
        raise ValueError(
            "since needs a timezone offset, e.g. 2026-11-06T07:00:00-05:00 or ...Z"
        ) from None


# ---------------------------------------------------------------- queries


def courses(conn: sqlite3.Connection) -> CourseList:
    meetings: dict[str, list[Meeting]] = {}
    for m in repo.list_meetings(conn):
        meetings.setdefault(m.course_code, []).append(meeting_out(m))
    return CourseList(
        courses=[
            Course(code=code, name=title, meetings=meetings.get(code, []))
            for code, title in repo.active_course_titles(conn).items()
        ]
    )


def _items_out(conn: sqlite3.Connection, items: list[Item], now: datetime) -> list[WorkItem]:
    stamps = repo.latest_change_at(conn, [i.id for i in items])
    platforms = repo.finding_platforms(conn)
    return [
        to_work_item(i, now, updated_at=stamps.get(i.id), finding_platform=platforms.get(i.id))
        for i in items
    ]


def upcoming(conn: sqlite3.Connection, now: datetime, days: int) -> UpcomingWork:
    """Work due in the next `days` days, plus anything up to 7 days late that isn't done."""
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    now = ensure_utc(now)
    window = repo.list_items(
        conn,
        due_after=now - OVERDUE_LOOKBACK,
        due_before=now + timedelta(days=days),
        include_undated=False,
    )
    keep = [i for i in window if i.due_at is not None and (i.due_at >= now or not is_done(i, now))]
    return UpcomingWork(as_of=local_iso(now) or "", days=days, items=_items_out(conn, keep, now))


def changes(conn: sqlite3.Connection, now: datetime, since: datetime) -> Changes:
    """Items that are new, had their due date changed, or were removed after `since`."""
    now, since = ensure_utc(now), ensure_utc(since)
    events = repo.item_changes_since(conn, since)
    active = repo.active_course_titles(conn)
    items = [
        i
        for i in repo.items_by_ids(conn, sorted({e.item_id for e in events}))
        if i.course_code in active
    ]
    net = summarize_changes(events, {i.id: i.due_at for i in items})
    changed = [i for i in items if i.id in net]
    out = [
        ChangedItem(
            **base.model_dump(),
            status=net[item.id].status,
            old_due=local_iso(net[item.id].old_due),
        )
        for item, base in zip(changed, _items_out(conn, changed, now), strict=True)
    ]
    return Changes(as_of=local_iso(now) or "", since=local_iso(since) or "", changes=out)
