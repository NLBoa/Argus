"""The Add a file form (D074, pure): read what was typed, check it, and name the file.

Fields: `course`, `what`, `type`, `by_date` (YYYY-MM-DD), `by_time` (HH:MM, local), `depth`.
The route checks the file itself (its type, size, and first bytes). Errors name the field, so
the form marks that input and says why.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time

from argus.clock import local_datetime, to_local
from argus.domain.inbox_name import (
    MIME_TYPES,
    SEP,
    Depth,
    FileType,
    build_name,
    clean_what,
    format_by,
)
from argus.web.task_form import FieldError

PENDING = "…"  # a part the student hasn't filled in yet, in the preview


@dataclass(frozen=True)
class FileForm:
    """The form as typed (so a mistake is shown back exactly as entered)."""

    course: str = ""
    what: str = ""
    type: str = ""
    by_date: str = ""
    by_time: str = ""
    depth: str = Depth.NORMAL.value

    def check(self, courses: Collection[str], now: datetime) -> CheckedFile:
        """Every field, in form order. Raises FieldError for the first one that's wrong."""
        if self.course not in courses:
            raise FieldError("course", "Choose one of your courses.")
        what = clean_what(self.what)
        if not what:
            raise FieldError("what", "Say what the file is.")
        file_type = _enum(FileType, self.type)
        if file_type is None:
            raise FieldError("type", "Choose what kind of file it is.")
        by = self.by_at()
        if by <= now:
            raise FieldError("by_date", "That time has passed. Choose a later one.")
        depth = _enum(Depth, self.depth)
        if depth is None:
            raise FieldError("depth", "Choose deep, normal, or skim.")
        return CheckedFile(self.course, what, file_type, by, depth)

    def by_at(self) -> datetime:
        """The "done by" time in UTC. Raises FieldError."""
        day = _date(self.by_date)
        if day is None:
            raise FieldError("by_date", "Choose the date to have it done by.")
        at = _time(self.by_time)
        if at is None:
            raise FieldError("by_time", "Choose the time to have it done by.")
        return local_datetime(day, at.hour, at.minute)

    def with_by(self, by: datetime) -> FileForm:
        local = to_local(by)
        return FileForm(
            self.course,
            self.what,
            self.type,
            local.date().isoformat(),
            local.strftime("%H:%M"),
            self.depth,
        )


@dataclass(frozen=True)
class CheckedFile:
    course: str
    what: str
    file_type: FileType
    by: datetime  # UTC
    depth: Depth

    def name(self, ext: str) -> str:
        return build_name(
            course=self.course,
            what=self.what,
            file_type=self.file_type,
            by=self.by,
            depth=self.depth,
            ext=ext,
        )


def preview(form: FileForm, ext: str, courses: Collection[str]) -> str:
    """The name as it will be, with "…" for each part not filled in yet. Each part comes from
    the same functions the real name uses."""
    try:
        by = format_by(form.by_at())
    except FieldError:
        by = f"by {PENDING}"
    file_type = _enum(FileType, form.type)
    depth = _enum(Depth, form.depth)
    parts = (
        form.course if form.course in courses else PENDING,
        clean_what(form.what) or PENDING,
        file_type.value if file_type else PENDING,
        by,
        depth.value if depth else PENDING,
    )
    suffix = ext.lower() if ext.lower() in MIME_TYPES else PENDING
    return f"{SEP.join(parts)}.{suffix}"


def _enum[E: (FileType, Depth)](cls: type[E], value: str) -> E | None:
    try:
        return cls(value)
    except ValueError:
        return None


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _time(text: str) -> time | None:
    try:
        return time.fromisoformat(text.strip())
    except ValueError:
        return None
