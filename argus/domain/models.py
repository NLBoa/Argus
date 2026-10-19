"""Domain types shared by collectors, the store, the planner, and the web layer.

Collector output types end in `Record`, or are `SourceItem`. They are immutable, timezone-aware
(UTC), and carry a `raw_hash` of the payload fragment they came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import IntEnum, StrEnum


class Source(StrEnum):
    CANVAS_ICAL = "canvas_ical"
    CANVAS_API = "canvas_api"
    GRADESCOPE = "gradescope"
    PIAZZA = "piazza"
    PIAZZA_EMAIL = "piazza_email"
    UMDIO = "umdio"
    MANUAL = "manual"
    FINDING = "finding"

    @property
    def family(self) -> str:
        """Records of one family describe the same object in the same system."""
        if self in (Source.CANVAS_ICAL, Source.CANVAS_API):
            return "canvas"
        if self in (Source.PIAZZA, Source.PIAZZA_EMAIL):
            return "piazza"
        return self.value

    @property
    def is_official(self) -> bool:
        """Official sources beat dates extracted from posts (non-negotiable)."""
        return self in (Source.CANVAS_ICAL, Source.CANVAS_API, Source.GRADESCOPE)


class ItemKind(StrEnum):
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    EXAM = "exam"
    EVENT = "event"

    @property
    def weight(self) -> int:
        """Higher wins when merged records disagree on kind."""
        return {"event": 0, "assignment": 1, "quiz": 2, "exam": 3}[self.value]

    @property
    def takes_submission(self) -> bool:
        return self in (ItemKind.ASSIGNMENT, ItemKind.QUIZ)


class ItemStatus(StrEnum):
    UPCOMING = "upcoming"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    GRADED = "graded"
    MISSED = "missed"

    @property
    def is_open(self) -> bool:
        """Still needs work: reminders and the planner only consider open items."""
        return self in (ItemStatus.UPCOMING, ItemStatus.IN_PROGRESS)


class AuthorRole(StrEnum):
    INSTRUCTOR = "instructor"
    TA = "ta"
    STUDENT = "student"
    UNKNOWN = "unknown"

    @property
    def is_staff(self) -> bool:
        return self in (AuthorRole.INSTRUCTOR, AuthorRole.TA)


class Weekday(IntEnum):
    MON = 0
    TUE = 1
    WED = 2
    THU = 3
    FRI = 4
    SAT = 5
    SUN = 6


@dataclass(frozen=True, slots=True)
class SourceKey:
    source: Source
    external_id: str

    def __str__(self) -> str:
        return f"{self.source.value}:{self.external_id}"


@dataclass(frozen=True, slots=True)
class SourceItem:
    """One assessment as a single source reports it."""

    source: Source
    external_id: str
    course_code: str
    title: str
    kind: ItemKind
    due_at: datetime | None
    raw_hash: str
    url: str | None = None
    release_at: datetime | None = None
    late_due_at: datetime | None = None
    all_day: bool = False
    points: float | None = None
    submitted: bool | None = None  # None = this source does not know
    graded: bool | None = None
    description: str | None = None
    question_count: int | None = None

    @property
    def key(self) -> SourceKey:
        return SourceKey(self.source, self.external_id)


@dataclass(frozen=True, slots=True)
class MeetingRecord:
    """A recurring class meeting. Times are local wall-clock (America/New_York)."""

    course_code: str
    kind: str  # lecture, discussion, lab
    days: tuple[Weekday, ...]
    start: time
    end: time
    building: str | None = None
    room: str | None = None


@dataclass(frozen=True, slots=True)
class CourseInfo:
    course_code: str
    title: str | None = None
    instructors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PostRecord:
    """A Piazza post, instructor answer, or Canvas announcement."""

    platform: str  # piazza | canvas
    external_id: str
    via: Source
    course_code: str
    title: str
    body: str
    posted_at: datetime
    raw_hash: str
    author_role: AuthorRole = AuthorRole.UNKNOWN
    kind: str = "note"  # note | question | instructor_answer | announcement | syllabus
    url: str | None = None
    edited_at: datetime | None = None
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedItem:
    """The merged view of one item, derived from its source records (see merge.resolve_item)."""

    course_code: str
    title: str
    kind: ItemKind
    due_at: datetime | None
    due_source: Source | None
    release_at: datetime | None
    late_due_at: datetime | None
    points: float | None
    submitted: bool | None
    graded: bool | None
    description: str | None
    date_conflict: tuple[tuple[Source, datetime], ...] = field(default=())


@dataclass(frozen=True, slots=True)
class ItemLink:
    source: Source
    url: str | None


@dataclass(frozen=True, slots=True)
class Item:
    """A persisted, merged item as the rest of the app sees it."""

    id: int
    course_code: str
    kind: ItemKind
    title: str
    due_at: datetime | None
    status: ItemStatus
    due_source: Source | None = None
    release_at: datetime | None = None
    late_due_at: datetime | None = None
    points: float | None = None
    submitted: bool | None = None
    graded: bool | None = None
    description: str | None = None
    date_conflict: tuple[tuple[Source, datetime], ...] = ()
    links: tuple[ItemLink, ...] = ()

    @property
    def announced(self) -> bool:
        """True while the only date comes from an announcement, not an official source."""
        return self.due_source == Source.FINDING
