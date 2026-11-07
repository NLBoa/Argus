"""Mapping stored items to the Claude connector's shape (argus/feed.py, D073)."""

from datetime import UTC, datetime, time, timedelta

import pytest

from argus.domain.models import Item, ItemKind, ItemLink, ItemStatus, MeetingRecord, Source, Weekday
from argus.feed import (
    item_type,
    listed,
    local_iso,
    meeting_out,
    origin_and_link,
    parse_since,
    to_work_item,
)

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)  # Fri 10:00 EST
CANVAS = "https://elms.example.edu/courses/1/assignments/7"
GS = "https://www.gradescope.com/courses/1/assignments/7"


def make(**kw: object) -> Item:
    base: dict[str, object] = {
        "id": 42,
        "course_code": "CMSC351",
        "kind": ItemKind.ASSIGNMENT,
        "title": "Homework 3",
        "due_at": NOW + timedelta(days=2),
        "status": ItemStatus.UPCOMING,
        "due_source": Source.CANVAS_ICAL,
        "links": (ItemLink(Source.CANVAS_ICAL, CANVAS),),
        "created_at": NOW - timedelta(days=10),
    }
    base.update(kw)
    return Item(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "title", "expected"),
    [
        (ItemKind.ASSIGNMENT, "Homework 3", "assignment"),
        (ItemKind.ASSIGNMENT, "Project 2: Sorting", "project"),
        (ItemKind.QUIZ, "Quiz 4", "quiz"),
        (ItemKind.EXAM, "Midterm 1", "exam"),
        (ItemKind.EVENT, "Midterm review session", "event"),
        (ItemKind.EVENT, "Office hours", "event"),
    ],
)
def test_item_type(kind: ItemKind, title: str, expected: str) -> None:
    assert item_type(kind, title, "CMSC351") == expected


def test_a_run_is_type_run() -> None:
    assert item_type(ItemKind.ASSIGNMENT, "4mi Easy Run", "RUN") == "run"


def test_work_item_shape_and_new_york_offset() -> None:
    w = to_work_item(make(), NOW, updated_at=NOW - timedelta(hours=1))
    assert w.model_dump() == {
        "id": "42",
        "course": "CMSC351",
        "title": "Homework 3",
        "type": "assignment",
        "due": "2026-11-08T10:00:00-05:00",
        "link": CANVAS,
        "source": "canvas",
        "updated_at": "2026-11-06T09:00:00-05:00",
        "done": False,
        "overdue": False,
        "estimate_minutes": None,
        "preferred_start": None,
    }


def test_offset_follows_daylight_saving() -> None:
    assert local_iso(datetime(2026, 10, 23, 3, 59, tzinfo=UTC)) == "2026-10-22T23:59:00-04:00"
    assert local_iso(datetime(2026, 11, 3, 4, 59, tzinfo=UTC)) == "2026-11-02T23:59:00-05:00"
    assert local_iso(None) is None


def test_no_grades_points_or_descriptions_leak() -> None:
    w = to_work_item(
        make(points=100.0, description="secret rubric", graded=True, status=ItemStatus.GRADED),
        NOW,
        updated_at=None,
    )
    dumped = str(w.model_dump())
    assert "100" not in dumped
    assert "rubric" not in dumped
    assert w.done is True


def test_updated_at_falls_back_to_created_at() -> None:
    w = to_work_item(make(), NOW, updated_at=None)
    assert w.updated_at == "2026-10-27T11:00:00-04:00"


@pytest.mark.parametrize(
    ("status", "due_delta", "kind", "done", "overdue"),
    [
        (ItemStatus.UPCOMING, timedelta(days=1), ItemKind.ASSIGNMENT, False, False),
        (ItemStatus.UPCOMING, -timedelta(days=1), ItemKind.ASSIGNMENT, False, True),
        (ItemStatus.MISSED, -timedelta(days=6), ItemKind.ASSIGNMENT, False, True),
        (ItemStatus.SUBMITTED, -timedelta(days=1), ItemKind.ASSIGNMENT, True, False),
        (ItemStatus.GRADED, timedelta(days=1), ItemKind.QUIZ, True, False),
        (ItemStatus.IN_PROGRESS, -timedelta(hours=1), ItemKind.QUIZ, False, True),
        # An exam is over once it starts, even before the next status refresh.
        (ItemStatus.UPCOMING, -timedelta(minutes=1), ItemKind.EXAM, True, False),
        (ItemStatus.UPCOMING, timedelta(days=3), ItemKind.EXAM, False, False),
    ],
)
def test_done_and_overdue(
    status: ItemStatus, due_delta: timedelta, kind: ItemKind, done: bool, overdue: bool
) -> None:
    w = to_work_item(make(status=status, due_at=NOW + due_delta, kind=kind), NOW, updated_at=None)
    assert (w.done, w.overdue) == (done, overdue)


def test_undated_item_is_never_overdue() -> None:
    w = to_work_item(make(due_at=None), NOW, updated_at=None)
    assert (w.due, w.overdue) == (None, False)


def test_due_source_picks_the_link() -> None:
    both = (ItemLink(Source.CANVAS_ICAL, CANVAS), ItemLink(Source.GRADESCOPE, GS))
    assert origin_and_link(make(due_source=Source.GRADESCOPE, links=both)) == (
        Source.GRADESCOPE,
        GS,
    )
    assert origin_and_link(make(due_source=Source.CANVAS_ICAL, links=both)) == (
        Source.CANVAS_ICAL,
        CANVAS,
    )


def test_student_override_still_reports_the_official_source() -> None:
    both = (ItemLink(Source.GRADESCOPE, GS), ItemLink(Source.CANVAS_ICAL, CANVAS))
    origin, link = origin_and_link(make(due_source=Source.MANUAL, links=both))
    assert (origin, link) == (Source.CANVAS_ICAL, CANVAS)


def test_link_falls_back_to_any_url() -> None:
    links = (ItemLink(Source.CANVAS_ICAL, None), ItemLink(Source.GRADESCOPE, GS))
    assert origin_and_link(make(due_source=Source.CANVAS_ICAL, links=links)) == (
        Source.CANVAS_ICAL,
        GS,
    )
    assert origin_and_link(make(due_source=None, links=())) == (Source.MANUAL, None)
    assert origin_and_link(make(due_source=Source.FINDING, links=())) == (Source.FINDING, None)


@pytest.mark.parametrize(
    ("platform", "expected"), [("piazza", "piazza"), ("canvas", "canvas"), (None, "piazza")]
)
def test_finding_source_is_the_post_platform(platform: str | None, expected: str) -> None:
    post = "https://piazza.com/class/x/post/12"
    item = make(due_source=Source.FINDING, links=(ItemLink(Source.FINDING, post),))
    w = to_work_item(item, NOW, updated_at=None, finding_platform=platform)
    assert (w.source, w.link) == (expected, post)


def test_manual_and_gradescope_source_names() -> None:
    manual = make(due_source=Source.MANUAL, links=(ItemLink(Source.MANUAL, None),))
    assert to_work_item(manual, NOW, updated_at=None).source == "manual"
    gs = make(due_source=Source.GRADESCOPE, links=(ItemLink(Source.GRADESCOPE, GS),))
    assert to_work_item(gs, NOW, updated_at=None).source == "gradescope"
    piazza = make(due_source=None, links=(ItemLink(Source.PIAZZA, None),))
    assert to_work_item(piazza, NOW, updated_at=None).source == "piazza"


def test_meeting_out() -> None:
    m = MeetingRecord(
        "CMSC351", "lecture", (Weekday.MON, Weekday.WED), time(9, 30), time(10, 45), "IRB", "0324"
    )
    assert meeting_out(m).model_dump() == {
        "kind": "lecture",
        "days": ["Mon", "Wed"],
        "start": "09:30",
        "end": "10:45",
        "location": "IRB 0324",
    }
    bare = MeetingRecord("CMSC351", "lab", (Weekday.FRI,), time(14, 0), time(15, 50))
    assert meeting_out(bare).location is None


def test_parse_since() -> None:
    assert parse_since("2026-11-06T07:00:00-05:00") == datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    assert parse_since(" 2026-11-06T12:00:00Z ") == datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone offset"):
        parse_since("2026-11-06T07:00:00")
    with pytest.raises(ValueError, match="ISO 8601"):
        parse_since("yesterday")


# ---------------------------------------------------------------- Runna runs (D078)


def run(**kw: object) -> Item:
    base: dict[str, object] = {
        "course_code": "RUN",
        "title": "4mi Easy Run",
        "due_source": Source.RUNNA_ICAL,
        "links": (ItemLink(Source.RUNNA_ICAL, None),),
    }
    base.update(kw)
    return make(**base)


def test_run_work_item() -> None:
    w = to_work_item(
        run(),
        NOW,
        updated_at=NOW,
        estimate_minutes=45,
        preferred_start=datetime(2026, 11, 10, 21, 0, tzinfo=UTC),
    )
    assert (w.type, w.source, w.course) == ("run", "runna", "RUN")
    assert w.estimate_minutes == 45
    assert w.preferred_start == "2026-11-10T16:00:00-05:00"
    assert "description" not in w.model_dump()


def test_coursework_gets_estimate_minutes_and_no_preferred_start() -> None:
    w = to_work_item(make(), NOW, updated_at=NOW, estimate_minutes=90)
    assert w.estimate_minutes == 90 and w.preferred_start is None
    assert to_work_item(make(), NOW, updated_at=NOW).estimate_minutes is None


def test_ended_run_is_not_listed() -> None:
    assert not listed(run(due_at=NOW - timedelta(minutes=1)), NOW)
    assert listed(run(due_at=NOW + timedelta(hours=3)), NOW)
    assert listed(run(due_at=NOW), NOW)  # due this instant: its day hasn't ended


def test_overdue_coursework_is_still_listed() -> None:
    assert listed(make(due_at=NOW - timedelta(days=2)), NOW)
    assert not listed(make(due_at=NOW - timedelta(days=2), status=ItemStatus.SUBMITTED), NOW)
    assert not listed(make(due_at=None), NOW)
