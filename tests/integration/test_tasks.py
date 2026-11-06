"""Tasks the student adds by hand (D070): stored as a manual source record, so they merge, rank,
and survive syncs like any item; the form, edit (D076), and delete work with and without JS."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus import tasks
from argus.capabilities import derive
from argus.clock import FakeClock, local_datetime, to_local
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.domain.models import Item, ItemKind, Source
from argus.notify.channels import OutboundMessage
from argus.notify.router import Router
from argus.planning import compute, replan
from argus.reminders.engine import ReminderEngine
from argus.reminders.rules import candidates
from argus.store import repo
from argus.store.db import iso, open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner
from tests.caps import tracked_caps

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


# ---------------------------------------------------------------- editing what you added (D076)


def add_exam(conn: sqlite3.Connection, clock: FakeClock, **over: object) -> int:
    args: dict[str, object] = {
        "course_code": "CMSC351",
        "title": "Pop quiz 1",
        "due_at": clock.now() + timedelta(days=3),
        "notes": "Ch. 4",
        "hours": 4.0,
        "kind": ItemKind.EXAM,
        "now": clock.now(),
    } | over
    return tasks.add_task(conn, **args).id  # type: ignore[arg-type]


def edit(conn: sqlite3.Connection, clock: FakeClock, item_id: int, **over: object) -> Item | None:
    """Edit with everything as it is, except `over`."""
    item = repo.get_item(conn, item_id)
    assert item is not None
    args: dict[str, object] = {
        "course_code": item.course_code,
        "title": item.title,
        "due_at": item.due_at,
        "notes": item.description or "",
        "hours": user_hours(conn, item_id),
        "now": clock.now(),
    } | over
    return tasks.edit_task(conn, item_id, **args)  # type: ignore[arg-type]


def user_hours(conn: sqlite3.Connection, item_id: int) -> float | None:
    _, user = repo.latest_estimates(conn).get(item_id, (None, None))
    return user.p50 if user else None


def manual_external_id(conn: sqlite3.Connection, item_id: int) -> str:
    row = conn.execute(
        "SELECT external_id FROM item_sources WHERE item_id = ? AND source = 'manual'", (item_id,)
    ).fetchone()
    return str(row[0])


def test_edit_keeps_the_item_and_shows_the_change(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    item_id = add_exam(conn, clock)
    repo.log_time(conn, item_id, 0.5, clock.now())
    new_due = local_datetime(to_local(clock.now()).date() + timedelta(days=5), 10, 30)
    item = edit(
        conn,
        clock,
        item_id,
        title="  Pop quiz 2 ",
        course_code="MATH240",
        due_at=new_due,
        notes="Ch. 5, room 1101",
    )
    assert item is not None
    assert item.id == item_id
    assert (item.title, item.course_code, item.due_at) == ("Pop quiz 2", "MATH240", new_due)
    assert item.description == "Ch. 5, room 1101"
    assert item.kind == ItemKind.EXAM
    assert item.added_by_you
    assert task_titled(conn, "Pop quiz 1") is None
    assert [i.id for i in repo.list_items(conn) if i.title == "Pop quiz 2"] == [item_id]
    assert len(repo.time_logs_for(conn, item_id)) == 1
    assert repo.search(conn, "Pop quiz 2")
    # Its record now has the id an identical add would make, so adding it again is a no-op.
    again = tasks.add_task(
        conn,
        course_code="MATH240",
        title="Pop quiz 2",
        due_at=new_due,
        notes="Ch. 5, room 1101",
        kind=ItemKind.EXAM,
        now=clock.now(),
    )
    assert again.id == item_id


def test_a_moved_exam_is_logged_as_moved(conn: sqlite3.Connection, clock: FakeClock) -> None:
    item_id = add_exam(conn, clock)
    before = repo.get_item(conn, item_id)
    assert before is not None and before.due_at is not None
    since = clock.now()
    clock.advance(timedelta(minutes=5))
    edit(conn, clock, item_id, due_at=before.due_at + timedelta(days=1))
    moved = [e for e in repo.item_changes_since(conn, since) if e.item_id == item_id]
    assert [(e.change.value, e.old_due, e.new_due) for e in moved] == [
        ("due", before.due_at, before.due_at + timedelta(days=1))
    ]


def test_edit_sets_and_clears_your_study_hours(conn: sqlite3.Connection, clock: FakeClock) -> None:
    item_id = add_exam(conn, clock)
    assert user_hours(conn, item_id) == 4.0
    edit(conn, clock, item_id, hours=5.5)
    assert user_hours(conn, item_id) == 5.5
    replan(conn, DEMO_CONFIG, clock, tracking_sources=TRACKING, force=True)
    study = compute(conn, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING).study
    assert study.total[item_id] == pytest.approx(5.5)
    versions = len(repo.estimates_for(conn, item_id))
    edit(conn, clock, item_id, notes="Same hours")
    assert len(repo.estimates_for(conn, item_id)) == versions  # unchanged hours: nothing appended
    edit(conn, clock, item_id, hours=None)
    assert user_hours(conn, item_id) is None
    assert len(repo.estimates_for(conn, item_id)) == versions  # retracted, never deleted


def test_only_what_you_added_can_be_edited(conn: sqlite3.Connection, clock: FakeClock) -> None:
    synced = next(i for i in repo.list_items(conn) if i.kind == ItemKind.EXAM)
    assert not synced.added_by_you
    assert edit(conn, clock, synced.id, title="Renamed") is None
    assert repo.get_item(conn, synced.id) == synced
    item_id = add_exam(conn, clock)
    assert tasks.remove_task(conn, item_id, clock.now())
    assert edit(conn, clock, item_id, title="Back again") is None
    missing = tasks.edit_task(
        conn, 9999, course_code="CMSC351", title="x", due_at=clock.now(), now=clock.now()
    )
    assert missing is None


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"title": "   "}, "Give the exam a name."),
        ({"title": "x" * 201}, "Keep the name under 200 characters."),
        ({"course_code": "NOPE100"}, "Choose one of your courses."),
        ({"notes": "x" * 4001}, "Keep the notes under 4000 characters."),
        ({"hours": 0.0}, "hours must be between 0 and 200"),
    ],
)
def test_edit_refuses_bad_input_and_changes_nothing(
    conn: sqlite3.Connection, clock: FakeClock, over: dict[str, object], message: str
) -> None:
    item_id = add_exam(conn, clock)
    before = repo.get_item(conn, item_id)
    with pytest.raises(ValueError, match=message):
        edit(conn, clock, item_id, **over)
    assert repo.get_item(conn, item_id) == before
    assert user_hours(conn, item_id) == 4.0


def test_edit_onto_another_exam_you_added_is_refused(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    first = add_exam(conn, clock)
    second = add_exam(conn, clock, title="Pop quiz 2")
    with pytest.raises(ValueError, match=r"You already added this exam\."):
        edit(conn, clock, second, title="pop quiz 1")
    assert task_titled(conn, "Pop quiz 2") == second
    assert task_titled(conn, "Pop quiz 1") == first


def test_after_an_edit_the_old_details_add_a_new_exam(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    item_id = add_exam(conn, clock)
    old_key = manual_external_id(conn, item_id)
    edit(conn, clock, item_id, title="Pop quiz 1 (moved)")
    assert manual_external_id(conn, item_id) != old_key
    other = add_exam(conn, clock)  # the old name and time again: a different exam now
    assert other != item_id
    assert task_titled(conn, "Pop quiz 1 (moved)") == item_id


def test_edit_onto_a_deleted_exam_keeps_yours(conn: sqlite3.Connection, clock: FakeClock) -> None:
    gone = add_exam(conn, clock)
    assert tasks.remove_task(conn, gone, clock.now())
    item_id = add_exam(conn, clock, title="Pop quiz 2")
    item = edit(conn, clock, item_id, title="Pop quiz 1")
    assert item is not None and (item.id, item.title) == (item_id, "Pop quiz 1")
    removed = repo.get_item(conn, gone)
    assert removed is not None and not removed.added_by_you
    assert gone not in [i.id for i in repo.list_items(conn)]
    assert [i.id for i in repo.list_items(conn) if i.title == "Pop quiz 1"] == [item_id]


def test_edit_works_for_tasks_too(conn: sqlite3.Connection, clock: FakeClock) -> None:
    task = tasks.add_task(
        conn,
        course_code="CMSC351",
        title="Read ch. 5",
        due_at=clock.now() + timedelta(days=1),
        now=clock.now(),
    )
    item = edit(conn, clock, task.id, title="Read ch. 6", hours=2.0)
    assert item is not None
    assert (item.id, item.title, item.kind) == (task.id, "Read ch. 6", ItemKind.ASSIGNMENT)
    assert user_hours(conn, task.id) == 2.0


class Silent:
    name = "sms"

    def available(self) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> None:
        return None


def sent_keys(conn: sqlite3.Connection, item_id: int, rule: str) -> list[str]:
    rows = conn.execute(
        "SELECT key FROM notifications WHERE key LIKE ? ORDER BY id", (f"{item_id}:{rule}:%",)
    )
    return [r[0] for r in rows]


async def test_a_moved_date_rearms_its_reminders(tmp_path: Path, clock: FakeClock) -> None:
    """Keys are item:rule:due, so a new date is a new key: the exam's countdown and a task's
    24-hour warning go out again for the new time. (No demo data: its texts fill the day's cap.)"""
    today = to_local(clock.now()).date()
    clock.set(local_datetime(today, 18))
    conn = open_db(tmp_path / "quiet.db", clock)
    repo.sync_courses(conn, DEMO_CONFIG, clock.now())

    def at(days: int, hour: int) -> datetime:
        return local_datetime(today + timedelta(days=days), hour)

    exam = add_exam(conn, clock, due_at=at(3, 10))
    task = tasks.add_task(
        conn, course_code="CMSC351", title="Pre-lab", due_at=at(1, 9), now=clock.now()
    ).id
    engine = ReminderEngine(
        conn, DEMO_CONFIG, clock, Router([Silent()]), caps=tracked_caps(DEMO_CONFIG)
    )
    await engine.tick()

    def due(item_id: int, rule: str) -> list[str]:
        """Keys the rules would send now (sent ones are skipped)."""
        found = candidates(engine.snapshot(clock.now()))
        return [k for r in found for k in r.keys if k.startswith(f"{item_id}:{rule}:")]

    [countdown] = sent_keys(conn, exam, "exam_countdown")
    [warning] = sent_keys(conn, task, "due_24h")
    assert due(exam, "exam_countdown") == due(task, "due_24h") == []  # sent once, not again
    edit(conn, clock, exam, due_at=at(3, 14))
    edit(conn, clock, task, due_at=at(1, 11))
    assert due(exam, "exam_countdown") == [f"{exam}:exam_countdown:{iso(at(3, 14))}:3"]
    assert due(task, "due_24h") == [f"{task}:due_24h:{iso(at(1, 11))}"]
    assert countdown != due(exam, "exam_countdown")[0] and warning != due(task, "due_24h")[0]
    conn.close()


# ---------------------------------------------------------------- editing on the web (D076)


def my_exam(clock: FakeClock, **over: str) -> dict[str, str]:
    return exam_form(clock, title="Pop quiz 1") | over


def added(
    client: TestClient, db_path: Path, clock: FakeClock, path: str, data: dict[str, str]
) -> int:
    client.post(path, data=data, headers=ORIGIN)
    conn = open_db(db_path, clock)
    try:
        item_id = task_titled(conn, data["title"])
    finally:
        conn.close()
    assert item_id is not None
    return item_id


def test_the_exam_page_links_to_edit_only_for_yours(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    mine = added(client, db_path, clock, "/exams", my_exam(clock))
    conn = open_db(db_path, clock)
    try:
        synced = next(
            i.id for i in repo.list_items(conn) if i.kind == ItemKind.EXAM and not i.added_by_you
        )
    finally:
        conn.close()
    assert f'href="/exams/{mine}/edit"' in client.get(f"/exams/{mine}").text
    assert f'href="/exams/{mine}/edit"' in client.get(f"/items/{mine}").text
    assert "/edit" not in client.get(f"/exams/{synced}").text
    assert client.get(f"/exams/{synced}/edit").status_code == 404
    assert client.get("/exams/9999/edit").status_code == 404


def test_the_edit_form_shows_the_exam_as_it_is(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    mine = added(client, db_path, clock, "/exams", my_exam(clock, notes="Room 1101"))
    html = client.get(f"/exams/{mine}/edit").text
    day, _ = tomorrow_evening(clock)
    assert "Edit exam" in html
    assert f'action="/exams/{mine}"' in html
    assert 'value="Pop quiz 1"' in html
    assert f'value="{day}"' in html
    assert 'value="10:00"' in html
    assert 'value="4"' in html
    assert ">Room 1101</textarea>" in html
    assert 'value="CMSC351" selected' in html
    assert f'href="/exams/{mine}"' in html  # cancel goes back to the exam


def test_edit_without_js_redirects_to_the_exam(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    mine = added(client, db_path, clock, "/exams", my_exam(clock))
    data = my_exam(clock, title="Pop quiz 2", course="MATH240", due_time="14:00", hours="")
    r = client.post(f"/exams/{mine}", data=data, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/exams/{mine}"
    conn = open_db(db_path, clock)
    try:
        item = repo.get_item(conn, mine)
        assert item is not None
        assert (item.title, item.course_code) == ("Pop quiz 2", "MATH240")
        assert to_local(item.due_at).hour == 14  # type: ignore[arg-type]
        assert user_hours(conn, mine) is None
    finally:
        conn.close()
    page = client.get(f"/exams/{mine}").text
    assert "Pop quiz 2" in page


def test_edit_with_htmx_renders_the_exam_with_a_toast(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    mine = added(client, db_path, clock, "/exams", my_exam(clock))
    r = client.post(
        f"/exams/{mine}", data=my_exam(clock, title="Pop quiz 2", hours="6"), headers=HX
    )
    assert r.status_code == 200
    assert json.loads(r.headers["HX-Trigger"])["toast"]["message"] == "Saved Pop quiz 2"
    assert r.headers["HX-Push-Url"] == f"/exams/{mine}"
    assert "Pop quiz 2" in r.text


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"title": ""}, "Give the exam a name."),
        ({"due_date": "not-a-date"}, "Choose a due date."),
        ({"hours": "abc"}, "Hours must be a number."),
        ({"course": "NOPE100"}, "Choose one of your courses."),
    ],
)
def test_bad_edit_shows_the_form_again_as_typed(
    client: TestClient, db_path: Path, clock: FakeClock, over: dict[str, str], message: str
) -> None:
    mine = added(client, db_path, clock, "/exams", my_exam(clock))
    data = my_exam(clock, notes="typed this", **over)
    r = client.post(f"/exams/{mine}", data=data, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 422
    assert message in r.text
    assert 'aria-invalid="true"' in r.text
    assert f'action="/exams/{mine}"' in r.text
    assert ">typed this</textarea>" in r.text
    hx = client.post(f"/exams/{mine}", data=data, headers=HX)
    assert hx.status_code == 200
    assert message in hx.text
    assert "HX-Trigger" not in hx.headers


def test_edit_refuses_synced_exams_and_other_origins(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    mine = added(client, db_path, clock, "/exams", my_exam(clock))
    conn = open_db(db_path, clock)
    try:
        synced = next(
            i for i in repo.list_items(conn) if i.kind == ItemKind.EXAM and not i.added_by_you
        )
    finally:
        conn.close()
    r = client.post(f"/exams/{synced.id}", data=my_exam(clock), headers=ORIGIN)
    assert r.status_code == 404
    evil = client.post(
        f"/exams/{mine}", data=my_exam(clock, title="X"), headers={"origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    assert client.get(f"/exams/{synced.id}").text.count(synced.title) >= 1


def test_tasks_you_added_can_be_edited_on_the_web(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    mine = added(client, db_path, clock, "/tasks", form(clock))
    exam = added(client, db_path, clock, "/exams", my_exam(clock))
    assert f'href="/tasks/{mine}/edit"' in client.get(f"/items/{mine}").text
    html = client.get(f"/tasks/{mine}/edit").text
    assert "Edit task" in html and f'action="/tasks/{mine}"' in html
    assert client.get(f"/tasks/{exam}/edit").status_code == 404  # an exam edits as an exam
    assert client.get(f"/exams/{mine}/edit").status_code == 404
    r = client.post(
        f"/tasks/{mine}",
        data=form(clock, title="Read ch. 6"),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/items/{mine}"
    hx = client.post(f"/tasks/{mine}", data=form(clock, title="Read ch. 7"), headers=HX)
    assert json.loads(hx.headers["HX-Trigger"])["toast"]["message"] == "Saved Read ch. 7"
    assert hx.headers["HX-Push-Url"] == f"/items/{mine}"
