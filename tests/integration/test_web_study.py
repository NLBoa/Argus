"""Exam prep, self-graded practice, proposals, and obsidian:// links in the dashboard."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.store.db import open_db
from argus.vault.proposals import file_hash
from argus.web.app import create_app
from argus.web.deps import OfflineRunner

ORIGIN = {"Origin": "http://testserver"}
HX = {**ORIGIN, "HX-Request": "true"}
NOTE = "Courses/CMSC351/Concepts/Big-O notation.md"


@pytest.fixture
def config(tmp_path: Path) -> object:
    paths = DEMO_CONFIG.paths.model_copy(
        update={"data_dir": tmp_path / "var", "vault_dir": tmp_path / "School"}
    )
    return DEMO_CONFIG.model_copy(update={"paths": paths})


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock, config: object) -> Iterator[TestClient]:
    db = tmp_path / "w.db"
    conn = open_db(db, clock)
    seed_demo(conn, clock.now())
    conn.close()
    app = create_app(
        config,  # type: ignore[arg-type]
        DEMO_SECRETS,
        clock,
        db_path=db,
        runner_factory=lambda c, f: OfflineRunner(),
    )
    with TestClient(app) as c:
        yield c


def db(client: TestClient) -> sqlite3.Connection:
    c = sqlite3.connect(client.app.state.argus.db_path)  # type: ignore[attr-defined]
    c.row_factory = sqlite3.Row
    return c


def item_id(client: TestClient, title: str) -> int:
    return int(db(client).execute("SELECT id FROM items WHERE title = ?", (title,)).fetchone()[0])


def practice_id(client: TestClient, prompt_start: str) -> int:
    row = (
        db(client)
        .execute("SELECT id FROM practice_items WHERE prompt LIKE ?", (prompt_start + "%",))
        .fetchone()
    )
    return int(row[0])


def test_exam_list_and_prep_page(client: TestClient) -> None:
    html = client.get("/exams").text
    assert "Midterm 1" in html
    assert 'aria-label="Readiness for Midterm 1: 1 solid, 1 shaky, 1 untested"' in html
    assert 'href="/exams"' in client.get("/today").text  # in the main nav
    exam = item_id(client, "Midterm 1")
    page = client.get(f"/exams/{exam}").text
    # Weakest first and interleaved: shaky, then untested, then solid concepts.
    order = [
        page.index(q)
        for q in (
            "Does the master theorem",
            "How many distinct calls",
            "Is n^2 in O(n log n)?",
            "Solve T(n) = 8T(n/2)",
        )
    ]
    assert order == sorted(order)
    assert "of study" in page and "Study time" in page
    assert 'data-level="untested"' in page and ">Memoization<" in page
    assert client.get("/exams/9999").status_code == 404
    assert client.get(f"/exams/{item_id(client, 'Homework 4')}").status_code == 404


def test_self_grading(client: TestClient) -> None:
    pid = practice_id(client, "How many distinct calls")
    r = client.post(f"/practice/{pid}/attempt", data={"result": "correct"}, headers=HX)
    assert r.status_code == 200
    assert 'data-result="correct"' in r.text and "Marked as got it." in r.text
    assert (
        db(client)
        .execute("SELECT result FROM practice_attempts WHERE practice_item_id = ?", (pid,))
        .fetchone()[0]
        == "correct"
    )
    plain = client.post(
        f"/practice/{pid}/attempt", data={"result": "shaky"}, headers=ORIGIN, follow_redirects=False
    )
    assert plain.status_code == 303
    assert (
        client.post(f"/practice/{pid}/attempt", data={"result": "nope"}, headers=HX).status_code
        == 422
    )
    assert (
        client.post("/practice/9999/attempt", data={"result": "correct"}, headers=HX).status_code
        == 404
    )
    assert (
        client.post(f"/practice/{pid}/attempt", data={"result": "correct"}).status_code == 403
    )  # no Origin


def test_proposals_page_and_banner(client: TestClient) -> None:
    assert "Argus wants to change 1 note you edited." in client.get("/today").text
    html = client.get("/proposals").text
    assert NOTE in html and "Approve change" in html and "Keep my version" in html
    assert "Removed: " in html and "Added: " in html  # the diff speaks to screen readers too
    assert "Argus wants to change" not in html  # no banner on the page itself


def test_reject_keeps_the_students_version(client: TestClient) -> None:
    pid = db(client).execute("SELECT id FROM proposals").fetchone()[0]
    r = client.post(f"/proposals/{pid}/reject", headers=HX)
    assert "Kept your version." in r.text
    assert json.loads(r.headers["HX-Trigger"])["toast"]["kind"] == "ok"
    assert db(client).execute("SELECT status FROM proposals").fetchone()[0] == "rejected"
    again = client.post(f"/proposals/{pid}/reject", headers=HX)
    assert json.loads(again.headers["HX-Trigger"])["toast"]["kind"] == "error"
    assert client.get("/proposals").text.count("No proposals") == 1


def test_approve_applies_exactly_once_inside_a_vault_cycle(
    client: TestClient, config: object
) -> None:
    vault_dir: Path = config.paths.vault_dir  # type: ignore[attr-defined]
    conn = db(client)
    pid, before, after = conn.execute(
        "SELECT id, before_text, after_text FROM proposals"
    ).fetchone()
    (vault_dir / NOTE).parent.mkdir(parents=True)
    (vault_dir / NOTE).write_text(before, encoding="utf-8")
    with conn:
        conn.execute("UPDATE proposals SET base_hash = ?", (file_hash(before),))
    r = client.post(f"/proposals/{pid}/approve", headers=HX)
    assert json.loads(r.headers["HX-Trigger"])["toast"] == {
        "message": "Change applied to your note",
        "kind": "ok",
    }
    text = (vault_dir / NOTE).read_text(encoding="utf-8")
    assert "My own note" not in text and "for some c > 0 and all n >= n0" in text
    assert after != before
    assert db(client).execute("SELECT status FROM proposals").fetchone()[0] == "applied"
    assert db(client).execute("SELECT status FROM vault_runs").fetchone()[0] == "ok"
    second = client.post(f"/proposals/{pid}/approve", headers=HX)
    assert json.loads(second.headers["HX-Trigger"])["toast"]["kind"] == "error"
    assert client.post(f"/proposals/{pid}/approve", headers=ORIGIN).status_code == 409


def test_approve_of_a_changed_note_is_stale(client: TestClient) -> None:
    pid = db(client).execute("SELECT id FROM proposals").fetchone()[0]
    r = client.post(f"/proposals/{pid}/approve", headers=HX)
    toast = json.loads(r.headers["HX-Trigger"])["toast"]
    assert toast["kind"] == "error" and "changed since this was proposed" in toast["message"]
    assert db(client).execute("SELECT status FROM proposals").fetchone()[0] == "stale"


def test_obsidian_links_appear_once_the_note_exists(client: TestClient, config: object) -> None:
    vault_dir: Path = config.paths.vault_dir  # type: ignore[attr-defined]
    exam = item_id(client, "Midterm 1")
    page = client.get(f"/items/{exam}").text
    assert f'href="/exams/{exam}"' in page and "obsidian://" not in page
    note = vault_dir / "Courses/CMSC351/Exams/Midterm 1.md"
    note.parent.mkdir(parents=True)
    note.write_text("x", encoding="utf-8")
    assert (
        "obsidian://open?vault=School&amp;file=Courses/CMSC351/Exams/Midterm%201"
        in client.get(f"/items/{exam}").text
    )
    assert "Open exam prep in Obsidian" in client.get(f"/exams/{exam}").text


def test_health_shows_the_vault_and_banner_is_only_on_today(client: TestClient) -> None:
    html = client.get("/health").text
    assert "Study vault" in html and "Not synced yet" in html and "1 proposal waiting" in html
    assert "Argus wants to change" not in client.get("/upcoming").text
    with db(client) as conn:
        conn.execute(
            "INSERT INTO vault_runs (started_at, finished_at, status, error) VALUES (?, ?, 'error', 'git push failed')",
            ("2026-10-19T12:00:00Z", "2026-10-19T12:00:01Z"),
        )
    html = client.get("/health").text
    assert "Failing (1 in a row)" in html and "git push failed" in html


def test_errors_without_javascript_render_a_page(client: TestClient) -> None:
    """Review R15: a plain form post that fails gets the error state, not raw JSON."""
    pid = db(client).execute("SELECT id FROM proposals").fetchone()[0]
    r = client.post(
        f"/proposals/{pid}/approve", headers={**ORIGIN, "Referer": "http://testserver/proposals"}
    )
    assert r.status_code == 409 and r.headers["content-type"].startswith("text/html")
    assert "That changed already" in r.text and "changed since this was proposed" in r.text
    assert 'href="/proposals"' in r.text
    missing = client.get("/exams/9999", headers={"Accept": "text/html"})
    assert missing.status_code == 404 and "Not found" in missing.text
    assert client.get("/exams/9999", headers={"Accept": "application/json"}).json() == {
        "detail": "No such exam"
    }


def test_logging_study_time_on_the_exam_page_reduces_study_time(client: TestClient) -> None:
    exam = item_id(client, "Midterm 1")
    page = client.get(f"/exams/{exam}").text
    assert f'action="/items/{exam}/log"' in page and "Log study time" in page
    r = client.post(
        f"/items/{exam}/log",
        data={"hours": "2"},
        headers={"origin": "http://testserver", "referer": f"http://testserver/exams/{exam}"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/exams/{exam}"
