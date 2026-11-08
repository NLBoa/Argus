"""The /goals page (D079): hidden without goal files (D055), FlowSavvy parts only with the key,
problems on the status line, and no AI anywhere."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from argus.clock import FakeClock, local_datetime
from argus.config import AppConfig, GoalsConfig, PathsConfig, Secrets
from argus.domain.goals import DocError, parse_doc
from argus.domain.models import GoalSessionGone, GoalSessionRecord
from argus.store import repo
from argus.store.db import open_db, transaction
from argus.web.app import create_app
from argus.web.deps import OfflineRunner, get_conn
from tests.ai_fakes import FakeTransport
from tests.conftest import REPO_ROOT

VALID = REPO_ROOT / "tests" / "fixtures" / "goals" / "valid"
NOW = local_datetime(date(2026, 11, 12), 18, 40)
KEY = Secrets(flowsavvy_api_key=SecretStr("fs_FAKEKEY"))


def seed(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for name in ("Woodwork.md", "Spanish.md", "Chess.md", "Weeks/2026-W46.md"):
            parsed = parse_doc(f"Goals/{name}", (VALID / name).read_text())
            assert parsed is not None and not isinstance(parsed, DocError)
            repo.upsert_goal_doc(
                conn,
                path=f"Goals/{name}",
                kind="week" if "Weeks" in name else "goal",
                text_hash="h",
                parsed=parsed,
                error=None,
                now=NOW,
            )
        start = local_datetime(date(2026, 11, 13), 19)
        repo.apply_goal_sessions(
            conn,
            [
                GoalSessionRecord(
                    "fri",
                    "Woodwork",
                    "Glue up the carcass",
                    start,
                    start + timedelta(hours=1),
                    60,
                    False,
                    "Square to 1 mm",
                    "h1",
                ),
                GoalSessionRecord(
                    "tue",
                    "Woodwork",
                    "Sharpen chisels",
                    start - timedelta(days=3),
                    start - timedelta(days=3, minutes=-45),
                    45,
                    False,
                    None,
                    "h2",
                ),
            ],
            [GoalSessionGone("tue", "deleted")],
            known_tags={"Woodwork", "Spanish"},
            now=NOW,
        )


def build(
    tmp_path: Path,
    clock: FakeClock,
    *,
    goals: bool = True,
    files: bool = True,
    secrets: Secrets = KEY,
    transport: FakeTransport | None = None,
) -> TestClient:
    clock.set(NOW)
    db = tmp_path / "goals.db"
    conn = open_db(db, clock)
    if files:
        seed(conn)
    conn.close()
    config = AppConfig(
        paths=PathsConfig(data_dir=tmp_path, vault_dir=tmp_path / "vault"),
        goals=GoalsConfig() if goals else None,
    )
    return TestClient(
        create_app(
            config,
            secrets,
            clock,
            db_path=db,
            runner_factory=lambda c, f: OfflineRunner(),
            ai_transport=transport,
        )
    )


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    with build(tmp_path, clock) as c:
        yield c


def boot(html: str) -> dict[str, object]:
    m = re.search(r'<script type="application/json" id="argus-boot">(.*?)</script>', html, re.S)
    assert m
    return json.loads(m.group(1))  # type: ignore[no-any-return]


def test_goals_renders_and_marks_sessions(client: TestClient) -> None:
    r = client.get("/goals")
    assert r.status_code == 200
    html = r.text
    assert '<h1 class="page-title">Goals</h1>' in html
    assert "Week 46, Nov 9\N{EN DASH}15" in html
    assert "Build a small bookshelf by spring" in html and "Basic joints" in html
    assert 'data-requires="goal_sessions"' in html
    assert "Glue up the carcass" in html and "Square to 1 mm" in html
    assert "Sharpen chisels" in html and "Dropped" in html
    assert "Measure a normal week first" in html  # the week's change
    assert "Trends start when week 46 ends" in html
    assert 'href="/goals"' in client.get("/today").text  # the More menu
    assert "/goals" not in boot(client.get("/today").text)["pages"]  # type: ignore[operator]


def test_goals_404_without_block(tmp_path: Path, clock: FakeClock) -> None:  # Review Focus 4
    with build(tmp_path, clock, goals=False) as c:
        assert c.get("/goals").status_code == 404
        today = c.get("/today").text
        assert 'href="/goals"' not in today and "/goals" in boot(today)["pages"]  # type: ignore[operator]


def test_goals_404_with_block_but_no_files(tmp_path: Path, clock: FakeClock) -> None:
    with build(tmp_path, clock, files=False) as c:
        assert c.get("/goals").status_code == 404
        today = c.get("/today").text
        assert 'href="/goals"' not in today and 'data-requires="goals"' not in today
        assert "/goals" in boot(today)["pages"]  # type: ignore[operator]


def test_without_flowsavvy_no_session_markup(tmp_path: Path, clock: FakeClock) -> None:
    with build(tmp_path, clock, secrets=Secrets()) as c:
        html = c.get("/goals").text
        assert "Basic joints" in html and "Target 240 min." in html
        assert 'data-requires="goal_sessions"' not in html
        assert "Glue up the carcass" not in html and "Trends" not in html


def test_goals_db_error_renders_error_state_503(client: TestClient) -> None:
    def broken() -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(":memory:")
        yield conn

    client.app.dependency_overrides[get_conn] = broken  # type: ignore[attr-defined]
    r = client.get("/goals")
    assert r.status_code == 503 and "Retry" in r.text


def test_problem_note_in_status_line(tmp_path: Path, clock: FakeClock) -> None:
    with build(tmp_path, clock) as c:
        db = c.app.state.argus.db_path  # type: ignore[attr-defined]
        conn = open_db(db, clock)
        with transaction(conn):
            repo.upsert_goal_doc(
                conn,
                path="Goals/Woodwork.md",
                kind="goal",
                text_hash="h2",
                parsed=None,
                error=DocError(14, "track[1].status", "must be done, current or next"),
                now=NOW,
            )
        conn.close()
        today = c.get("/today").text
        assert "Woodwork.md has an error on line 14" in today
        goals = c.get("/goals").text
        assert "Showing the version read" in goals and "must be done, current or next" in goals


def test_no_ai_calls(tmp_path: Path, clock: FakeClock) -> None:
    transport = FakeTransport([])
    with build(tmp_path, clock, transport=transport) as c:
        assert c.get("/goals").status_code == 200
    assert transport.calls == []
