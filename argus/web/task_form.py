"""The Add a task form (pure, D070), also Add an exam (D075) and editing either (D076): read what
was typed and turn it into a due time and hours, or fill the form from what is stored.

Fields: `course`, `title`, `due_date` (YYYY-MM-DD), `due_time` (HH:MM, local), `hours` (blank =
set it later), `notes`. Errors name the field, so the form marks that input and says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from argus.clock import local_datetime, to_local

DEFAULT_TIME = "23:59"


@dataclass(frozen=True)
class TaskForm:
    """The form as typed (so a mistake is shown back exactly as entered)."""

    course: str = ""
    title: str = ""
    due_date: str = ""
    due_time: str = DEFAULT_TIME
    hours: str = ""
    notes: str = ""

    @classmethod
    def of(
        cls, *, course: str, title: str, due_at: datetime, hours: float | None, notes: str | None
    ) -> TaskForm:
        """The form for an item as stored (local day and time, hours without a trailing .0)."""
        local = to_local(due_at)
        return cls(
            course=course,
            title=title,
            due_date=local.date().isoformat(),
            due_time=local.strftime("%H:%M"),
            hours="" if hours is None else f"{hours:g}",
            notes=notes or "",
        )

    def due_at(self) -> datetime:
        """The due time in UTC. Raises FieldError."""
        try:
            day = date.fromisoformat(self.due_date)
        except ValueError:
            raise FieldError("due_date", "Choose a due date.") from None
        try:
            at = time.fromisoformat(self.due_time or DEFAULT_TIME)
        except ValueError:
            raise FieldError("due_time", "Choose a due time.") from None
        return local_datetime(day, at.hour, at.minute)

    def hours_value(self) -> float | None:
        """The hours typed, or None when blank. Raises FieldError."""
        if not self.hours.strip():
            return None
        try:
            return float(self.hours)
        except ValueError:
            raise FieldError("hours", "Hours must be a number.") from None


class FieldError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def field_for(message: str) -> str:
    """Which input a validation message from `tasks.add_task` or `edit_task` is about."""
    if "course" in message:
        return "course"
    if "notes" in message:
        return "notes"
    if "hours" in message:
        return "hours"
    return "title"
