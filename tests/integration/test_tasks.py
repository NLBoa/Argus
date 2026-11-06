"""Tasks the student adds by hand (D070): stored as a manual source record, so they merge, rank,
and survive syncs like any item; the form and delete work with and without JS."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus import tasks
from argus.capabilities import derive
from argus.clock import FakeClock, local_datetime, to_local
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.domain.models import ItemKind, Source
from argus.planning import compute, replan
from argus.store import repo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner

ORIGIN = {"origin": "http://testserver", "referer": "http://testserver/tasks/new"}
HX = {**ORIGIN, "hx-request": "true"}
TRACKING = derive(DEMO_CONFIG, demo_secrets()).tracking_sources


@pytest.fixture
def db_path(tmp_path: Path, clock: FakeClock) -> Path:
    path = tmp_path / "t.db"
    conn = open_db(path, clock)
    seed_demo(conn, clock.now(), DemoProfile.FULL)
    replan(conn, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    conn.close()
    return path


@pytest.fixture
def conn(db_path: Path, clock: FakeClock) -> Iterator[sqlite3.Connection]:
    c = open_db(db_path, clock)
    yield c
    c.close()


@pytest.fixture
def client(db_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(
        DEMO_CONFIG,
        demo_secrets(),
        clock,
        db_path=db_path,
        runner_factory=lambda c, f: OfflineRunner(),
    )
    with TestClient(app) as c:
        yield c


def tomorrow_evening(clock: FakeClock) -> tuple[str, str]:
    day = to_local(clock.now()).date() + timedelta(days=1)
    return day.isoformat(), "20:00"


def form(clock: FakeClock, **over: str) -> dict[str, str]:
    day, time = tomorrow_evening(clock)
    return {
        "course": "CMSC351",
        "title": "Study for Midterm 2",
        "due_date": day,
        "due_time": time,
        "hours": "3",
        "notes": "",
    } | over


def task_titled(conn: sqlite3.Connection, title: str) -> int | None:
    return next((i.id for i in repo.list_items(conn) if i.title == title), None)


# ---------------------------------------------------------------- the module


def test_add_task_creates_an_open_item_with_your_estimate(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    due = clock.now() + timedelta(days=2)
    item = tasks.add_task(
        conn,
        course_code="CMSC351",
        title="  Study for Midterm 2 ",
        due_at=due,
        notes="Chapters 4-6",
        hours=2.5,
        now=clock.now(),
    )
    assert item.title == "Study for Midterm 2"
    assert item.kind == ItemKind.ASSIGNMENT
    assert item.due_at == due.replace(microsecond=0)
    assert item.description == "Chapters 4-6"
    assert item.status.is_open
    assert item.added_by_you
    assert [link.source for link in item.links] == [Source.MANUAL]
    replan(conn, DEMO_CONFIG, clock, tracking_sources=TRACKING, force=True)
    plan = compute(conn, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING).items
    assert plan[item.id].remaining == pytest.approx(2.5)


def test_adding_the_same_task_twice_keeps_one(conn: sqlite3.Connection, clock: FakeClock) -> None:
    due = clock.now() + timedelta(days=2)
    args = {"course_code": "CMSC351", "title": "Read ch. 5", "due_at": due, "now": clock.now()}
    first = tasks.add_task(conn, **args)  # type: ignore[arg-type]
    second = tasks.add_task(conn, **args)  # type: ignore[arg-type]
    assert first.id == second.id
    assert [i.title for i in repo.list_items(conn)].count("Read ch. 5") == 1


def test_a_task_survives_the_next_merge(conn: sqlite3.Connection, clock: FakeClock) -> None:
    item = tasks.add_task(
        conn,
        course_code="CMSC351",
        title="Read ch. 5",
        due_at=clock.now() + timedelta(days=1),
        now=clock.now(),
    )
    repo.apply_merge(conn, repo.course_ids(conn), clock.now())
    assert task_titled(conn, "Read ch. 5") == item.id


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"title": "   "}, "Give the task a name."),
        ({"title": "x" * 201}, "Keep the name under 200 characters."),
        ({"course_code": "NOPE100"}, "Choose one of your courses."),
        ({"notes": "x" * 4001}, "Keep the notes under 4000 characters."),
        ({"hours": 0.0}, "hours must be between 0 and 200"),
    ],
)
def test_add_task_refuses_bad_input(
    conn: sqlite3.Connection, clock: FakeClock, over: dict[str, object], message: str
) -> None:
    args: dict[str, object] = {
        "course_code": "CMSC351",
        "title": "Read ch. 5",
        "due_at": clock.now() + timedelta(days=1),
        "now": clock.now(),
    } | over
    with pytest.raises(ValueError, match=message):
        tasks.add_task(conn, **args)  # type: ignore[arg-type]
    assert task_titled(conn, "Read ch. 5") is None


def test_remove_task_removes_only_your_tasks(conn: sqlite3.Connection, clock: FakeClock) -> None:
    item = tasks.add_task(
        conn,
        course_code="CMSC351",
        title="Read ch. 5",
        due_at=clock.now() + timedelta(days=1),
        now=clock.now(),
    )
    synced = next(i for i in repo.list_items(conn) if not i.added_by_you)
    assert tasks.remove_task(conn, synced.id, clock.now()) is False
    assert tasks.remove_task(conn, item.id, clock.now()) is True
    assert task_titled(conn, "Read ch. 5") is None
    assert task_titled(conn, synced.title) == synced.id
    assert tasks.remove_task(conn, item.id, clock.now()) is False  # already gone


# ---------------------------------------------------------------- the web


def test_today_links_to_the_form(client: TestClient) -> None:
    html = client.get("/today").text
    assert 'href="/tasks/new"' in html


def test_form_lists_courses_and_defaults_to_tonight(client: TestClient, clock: FakeClock) -> None:
    html = client.get("/tasks/new").text
    assert 'name="title"' in html
    for code in ("CMSC351", "MATH240", "ENGL393"):
        assert f'value="{code}"' in html
    assert f'value="{to_local(clock.now()).date().isoformat()}"' in html
    assert 'value="23:59"' in html


def test_add_without_js_redirects_to_today(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    r = client.post("/tasks", data=form(clock), headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/today"
    conn = open_db(db_path, clock)
    try:
        item_id = task_titled(conn, "Study for Midterm 2")
        assert item_id is not None
        item = repo.get_item(conn, item_id)
        assert item is not None
        assert item.due_at == local_datetime(to_local(clock.now()).date() + timedelta(days=1), 20)
    finally:
        conn.close()
    assert "Study for Midterm 2" in client.get("/today").text


def test_add_with_htmx_renders_today_with_a_toast(client: TestClient, clock: FakeClock) -> None:
    r = client.post("/tasks", data=form(clock), headers=HX)
    assert r.status_code == 200
    assert json.loads(r.headers["HX-Trigger"])["toast"]["message"] == "Added Study for Midterm 2"
    assert r.headers["HX-Push-Url"] == "/today"
    assert "Study for Midterm 2" in r.text


def test_without_hours_the_task_asks_for_a_time(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    client.post("/tasks", data=form(clock, hours=""), headers=ORIGIN)
    conn = open_db(db_path, clock)
    try:
        item_id = task_titled(conn, "Study for Midterm 2")
        assert item_id is not None
        assert repo.latest_estimates(conn).get(item_id) is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"title": ""}, "Give the task a name."),
        ({"due_date": "not-a-date"}, "Choose a due date."),
        ({"due_time": "25:00"}, "Choose a due time."),
        ({"hours": "abc"}, "Hours must be a number."),
        ({"course": "NOPE100"}, "Choose one of your courses."),
    ],
)
def test_bad_input_shows_the_form_again(
    client: TestClient, clock: FakeClock, over: dict[str, str], message: str
) -> None:
    r = client.post("/tasks", data=form(clock, **over), headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 422
    assert message in r.text
    assert 'aria-invalid="true"' in r.text
    hx = client.post("/tasks", data=form(clock, **over), headers=HX)
    assert hx.status_code == 200
    assert message in hx.text


def test_cross_origin_add_is_refused(client: TestClient, clock: FakeClock) -> None:
    r = client.post("/tasks", data=form(clock), headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_item_page_offers_delete_only_for_your_tasks(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    client.post("/tasks", data=form(clock), headers=ORIGIN)
    conn = open_db(db_path, clock)
    try:
        mine = task_titled(conn, "Study for Midterm 2")
        synced = next(i.id for i in repo.list_items(conn) if not i.added_by_you)
    finally:
        conn.close()
    assert f'action="/tasks/{mine}/delete"' in client.get(f"/items/{mine}").text
    assert "/delete" not in client.get(f"/items/{synced}").text


def test_delete_with_and_without_js(client: TestClient, db_path: Path, clock: FakeClock) -> None:
    client.post("/tasks", data=form(clock), headers=ORIGIN)
    client.post("/tasks", data=form(clock, title="Read ch. 5"), headers=ORIGIN)
    conn = open_db(db_path, clock)
    try:
        a = task_titled(conn, "Study for Midterm 2")
        b = task_titled(conn, "Read ch. 5")
        synced = next(i.id for i in repo.list_items(conn) if not i.added_by_you)
    finally:
        conn.close()
    r = client.post(f"/tasks/{a}/delete", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/today"
    hx = client.post(f"/tasks/{b}/delete", headers=HX)
    assert json.loads(hx.headers["HX-Trigger"])["toast"]["message"] == "Deleted Read ch. 5"
    assert hx.headers["HX-Push-Url"] == "/today"
    assert client.post(f"/tasks/{synced}/delete", headers=ORIGIN).status_code == 404
    conn = open_db(db_path, clock)
    try:
        assert task_titled(conn, "Study for Midterm 2") is None
        assert task_titled(conn, "Read ch. 5") is None
    finally:
        conn.close()


# ---------------------------------------------------------------- exams you add (D075)


def exam_form(clock: FakeClock, **over: str) -> dict[str, str]:
    return form(clock, title="Quiz 4", due_time="10:00", hours="4") | over


def test_add_exam_creates_an_exam_with_study_time(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    due = clock.now() + timedelta(days=3)
    item = tasks.add_task(
        conn,
        course_code="MATH240",
        title="Quiz 4",
        due_at=due,
        hours=4,
        kind=ItemKind.EXAM,
        now=clock.now(),
    )
    assert item.kind == ItemKind.EXAM
    assert item.added_by_you
    replan(conn, DEMO_CONFIG, clock, tracking_sources=TRACKING, force=True)
    study = compute(conn, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING).study
    assert study.total[item.id] == pytest.approx(4)
    reserved = sum(q.get(item.id, 0.0) for q in study.quota.values())
    assert reserved + study.shortfall.get(item.id, 0.0) == pytest.approx(4)


def test_an_exam_and_a_task_with_the_same_name_stay_apart(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    args = {
        "course_code": "CMSC351",
        "title": "Quiz 4",
        "due_at": clock.now() + timedelta(days=2),
        "now": clock.now(),
    }
    task = tasks.add_task(conn, **args)  # type: ignore[arg-type]
    exam = tasks.add_task(conn, kind=ItemKind.EXAM, **args)  # type: ignore[arg-type]
    assert task.id != exam.id
    assert task.kind == ItemKind.ASSIGNMENT


def test_only_assignments_and_exams_can_be_added(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    with pytest.raises(ValueError, match="kind"):
        tasks.add_task(
            conn,
            course_code="CMSC351",
            title="Office hours",
            due_at=clock.now() + timedelta(days=1),
            kind=ItemKind.EVENT,
            now=clock.now(),
        )


def test_exams_page_links_to_the_exam_form(client: TestClient) -> None:
    assert 'href="/exams/new"' in client.get("/exams").text
    html = client.get("/exams/new").text
    assert "Add an exam" in html
    assert 'action="/exams"' in html
    assert 'value="23:59"' not in html  # exams aren't at midnight


def test_add_exam_without_js_redirects_to_exams(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    r = client.post("/exams", data=exam_form(clock), headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/exams"
    conn = open_db(db_path, clock)
    try:
        item_id = task_titled(conn, "Quiz 4")
        assert item_id is not None
        item = repo.get_item(conn, item_id)
        assert item is not None and item.kind == ItemKind.EXAM
    finally:
        conn.close()
    assert "Quiz 4" in client.get("/exams").text
    page = client.get(f"/exams/{item_id}").text
    assert "of study" in page or "doesn't fit" in page
    assert f'action="/tasks/{item_id}/delete"' in page
    assert "Delete exam" in page


def test_add_exam_with_htmx_renders_exams_with_a_toast(
    client: TestClient, clock: FakeClock
) -> None:
    r = client.post("/exams", data=exam_form(clock), headers=HX)
    assert r.status_code == 200
    assert json.loads(r.headers["HX-Trigger"])["toast"]["message"] == "Added Quiz 4"
    assert r.headers["HX-Push-Url"] == "/exams"
    assert "Quiz 4" in r.text


def test_bad_exam_input_shows_the_exam_form_again(client: TestClient, clock: FakeClock) -> None:
    r = client.post(
        "/exams", data=exam_form(clock, title=""), headers=ORIGIN, follow_redirects=False
    )
    assert r.status_code == 422
    assert "Give the exam a name." in r.text
    assert 'action="/exams"' in r.text


def test_deleting_an_exam_returns_to_exams(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    client.post("/exams", data=exam_form(clock), headers=ORIGIN)
    conn = open_db(db_path, clock)
    try:
        item_id = task_titled(conn, "Quiz 4")
    finally:
        conn.close()
    r = client.post(f"/tasks/{item_id}/delete", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/exams"
    assert "Quiz 4" not in client.get("/exams").text
