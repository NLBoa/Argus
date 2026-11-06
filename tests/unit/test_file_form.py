"""The Add a file form (D074): checking each field, the "done by" time, and the name preview."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from argus.domain.inbox_name import Depth, FileType
from argus.web.file_form import FileForm, preview
from argus.web.task_form import FieldError

COURSES = {"STAT400", "CMSC351"}
NOW = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)  # Friday 12:00 EST
GOOD = FileForm(
    course="STAT400",
    what="Ch5 lecture slides",
    type="slides",
    by_date="2026-11-09",
    by_time="09:00",
    depth="deep",
)


def field_of(form: FileForm) -> str:
    with pytest.raises(FieldError) as err:
        form.check(COURSES, NOW)
    return err.value.field


def test_good_form_names_the_file() -> None:
    checked = GOOD.check(COURSES, NOW)
    assert checked.file_type is FileType.SLIDES
    assert checked.depth is Depth.DEEP
    assert checked.by == datetime(2026, 11, 9, 14, 0, tzinfo=UTC)
    assert checked.name("pdf") == (
        "STAT400 · Ch5 lecture slides · slides · by 2026-11-09 09:00 · deep.pdf"
    )


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"course": ""}, "course"),
        ({"course": "MATH240"}, "course"),  # not one of the student's courses
        ({"what": "  ·  "}, "what"),
        ({"type": ""}, "type"),
        ({"type": "slide"}, "type"),
        ({"by_date": ""}, "by_date"),
        ({"by_date": "11/09/2026"}, "by_date"),
        ({"by_time": ""}, "by_time"),
        ({"by_time": "9am"}, "by_time"),
        ({"by_date": "2026-11-06", "by_time": "12:00"}, "by_date"),  # exactly now
        ({"by_date": "2026-11-01", "by_time": "09:00"}, "by_date"),  # past
        ({"depth": "medium"}, "depth"),
    ],
)
def test_each_mistake_names_its_field(change: dict[str, str], field: str) -> None:
    assert field_of(FileForm(**{**GOOD.__dict__, **change})) == field


def test_default_depth_is_normal() -> None:
    assert FileForm().depth == "normal"


def test_time_with_seconds_is_accepted() -> None:
    form = FileForm(**{**GOOD.__dict__, "by_time": "09:00:00"})
    assert form.check(COURSES, NOW).by == datetime(2026, 11, 9, 14, 0, tzinfo=UTC)


def test_with_by_fills_local_date_and_time() -> None:
    form = FileForm(course="STAT400").with_by(datetime(2026, 11, 10, 4, 59, tzinfo=UTC))
    assert (form.by_date, form.by_time) == ("2026-11-09", "23:59")


# ---------------------------------------------------------------- preview


def test_preview_of_a_complete_form_is_the_real_name() -> None:
    assert preview(GOOD, "PDF", COURSES) == GOOD.check(COURSES, NOW).name("pdf")


def test_preview_marks_missing_parts() -> None:
    assert preview(FileForm(), "", COURSES) == "… · … · … · by … · normal.…"
    partial = FileForm(course="CMSC351", what="notes / week 3", by_date="2026-11-09")
    assert preview(partial, "docx", COURSES) == "CMSC351 · notes week 3 · … · by … · normal.docx"


def test_preview_ignores_bad_values() -> None:
    bad = FileForm(course="NOPE100", type="slide", depth="x", by_date="2026-11-09", by_time="7")
    assert preview(bad, "exe", COURSES) == "… · … · … · by … · ….…"
