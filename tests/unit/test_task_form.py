"""The task and exam form (pure): filling it from a stored item reads back the same item (D076)."""

from __future__ import annotations

from datetime import date

import pytest

from argus.clock import local_datetime
from argus.web.task_form import FieldError, TaskForm


@pytest.mark.parametrize(
    "day",
    [date(2026, 10, 22), date(2026, 11, 1), date(2027, 3, 14)],  # EDT, the fall-back day, EST
)
def test_a_filled_form_reads_back_the_same_due_time(day: date) -> None:
    due = local_datetime(day, 9, 30)
    form = TaskForm.of(course="CMSC351", title="Quiz", due_at=due, hours=None, notes=None)
    assert (form.due_date, form.due_time) == (day.isoformat(), "09:30")
    assert form.due_at() == due
    assert (form.hours, form.notes) == ("", "")
    assert form.hours_value() is None


@pytest.mark.parametrize(("hours", "shown"), [(4.0, "4"), (2.5, "2.5"), (0.25, "0.25")])
def test_hours_show_without_a_trailing_zero(hours: float, shown: str) -> None:
    form = TaskForm.of(
        course="CMSC351",
        title="Quiz",
        due_at=local_datetime(date(2026, 10, 22), 9),
        hours=hours,
        notes="Room 1101",
    )
    assert form.hours == shown
    assert form.hours_value() == hours
    assert form.notes == "Room 1101"


def test_bad_fields_name_their_input() -> None:
    with pytest.raises(FieldError) as e:
        TaskForm(due_date="soon").due_at()
    assert e.value.field == "due_date"
    with pytest.raises(FieldError) as e:
        TaskForm(due_date="2026-10-22", hours="lots").hours_value()
    assert e.value.field == "hours"
