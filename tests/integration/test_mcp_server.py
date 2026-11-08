"""The Claude connector (argus/web/mcp_server.py, D073): the three tools over a seeded database,
and the HTTP edge (path secret, Host check)."""

import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr
from starlette.testclient import TestClient

from argus.clock import FakeClock
from argus.collectors.runna_ical import run_due
from argus.config import AppConfig, GoalsConfig, MCPConfig, RunnaConfig, Secrets
from argus.domain.goals import GoalDoc, WeekDoc
from argus.domain.models import (
    GoalSessionGone,
    GoalSessionRecord,
    ItemKind,
    MeetingRecord,
    Source,
    SourceItem,
    Weekday,
)
from argus.runs import sync_run_estimates
from argus.store import repo
from argus.store.db import transaction
from argus.web.mcp_server import build_server, create_mcp_app, path_secret

SECRET = "s" * 43
HOST = "argus.example.ts.net"
CANVAS = "https://elms.example.edu/courses/1/assignments/"


def src(ext: str, title: str, due: datetime | None, **kw: object) -> SourceItem:
    base: dict[str, object] = {
        "source": Source.CANVAS_ICAL,
        "external_id": ext,
        "course_code": "CMSC351",
        "title": title,
        "kind": ItemKind.ASSIGNMENT,
        "due_at": due,
        "raw_hash": f"{ext}:{due}",
        "url": CANVAS + ext,
        "description": "Rubric: secret details",
        "points": 100.0,
    }
    base.update(kw)
    return SourceItem(**base)  # type: ignore[arg-type]


def ical_items(now: datetime, hw_due: datetime) -> list[SourceItem]:
    return [
        src("hw", "Homework 3", hw_due),
        src("late", "Homework 1", now - timedelta(days=3)),
        src("ancient", "Homework 0", now - timedelta(days=8)),
        src("far", "Homework 9", now + timedelta(days=20)),
        src("undated", "Reading", None),
        src(
            "review",
            "Midterm review session",
            now + timedelta(days=1),
            kind=ItemKind.EVENT,
            course_code="MATH240",
        ),
    ]


@pytest.fixture
def ids(db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock) -> dict[str, int]:
    now = clock.now()
    with transaction(db):
        ids = repo.sync_courses(db, app_config, now)
        repo.replace_meetings(
            db,
            ids["CMSC351"],
            "umdio",
            [
                MeetingRecord(
                    "CMSC351",
                    "lecture",
                    (Weekday.TUE, Weekday.THU),
                    time(9, 30),
                    time(10, 45),
                    "IRB",
                    "0324",
                )
            ],
            now,
        )
        gradescope = [
            src(
                "p2",
                "Project 2",
                now + timedelta(days=5),
                source=Source.GRADESCOPE,
                url="https://www.gradescope.com/courses/1/assignments/2",
            ),
            src(
                "done",
                "Homework 2",
                now - timedelta(days=2),
                source=Source.GRADESCOPE,
                submitted=True,
                url=None,
            ),
        ]
        repo.upsert_source_items(
            db, ical_items(now, now + timedelta(days=2)) + gradescope, ids, now
        )
        repo.apply_merge(db, ids, now)
        repo.refresh_statuses(db, now, tracking_sources=frozenset({"gradescope"}))
    return ids


def secrets(value: str | None = SECRET) -> Secrets:
    return Secrets(mcp_path_secret=SecretStr(value) if value else None)


def config(app_config: AppConfig) -> AppConfig:
    return app_config.model_copy(update={"mcp": MCPConfig(allowed_hosts=[HOST])})


async def call(
    app_config: AppConfig, clock: FakeClock, db_path: Path, tool: str, args: dict[str, Any]
) -> Any:
    server = build_server(config(app_config), secrets(), clock, db_path)
    async with create_connected_server_and_client_session(server) as session:
        return await session.call_tool(tool, args)


async def test_lists_three_read_only_tools(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, ids: dict[str, int]
) -> None:
    server = build_server(config(app_config), secrets(), clock, tmp_path / "argus.db")
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
    assert sorted(t.name for t in tools) == ["get_changes", "get_upcoming_work", "list_courses"]
    for t in tools:
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is True
        assert t.outputSchema is not None


async def test_list_courses(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, ids: dict[str, int]
) -> None:
    result = await call(app_config, clock, tmp_path / "argus.db", "list_courses", {})
    courses = result.structuredContent["courses"]
    by_code = {c["code"]: c for c in courses}
    assert set(by_code) == set(ids)
    assert by_code["CMSC351"]["meetings"] == [
        {
            "kind": "lecture",
            "days": ["Tue", "Thu"],
            "start": "09:30",
            "end": "10:45",
            "location": "IRB 0324",
        }
    ]
    assert by_code["MATH240"]["meetings"] == []
    assert "grade" not in str(courses)


async def test_get_upcoming_work(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, ids: dict[str, int]
) -> None:
    result = await call(app_config, clock, tmp_path / "argus.db", "get_upcoming_work", {})
    out = result.structuredContent
    assert out["days"] == 14
    assert out["as_of"] == "2026-10-19T09:00:00-04:00"
    got = {i["title"]: i for i in out["items"]}
    # Late and not done is kept (overdue); 8 days late, done, undated, and 20 days out are not.
    assert list(got) == ["Homework 1", "Midterm review session", "Homework 3", "Project 2"]
    assert got["Homework 1"]["overdue"] is True
    assert got["Homework 3"] == {
        "id": got["Homework 3"]["id"],
        "course": "CMSC351",
        "title": "Homework 3",
        "type": "assignment",
        "due": "2026-10-21T09:00:00-04:00",
        "link": CANVAS + "hw",
        "source": "canvas",
        "updated_at": "2026-10-19T09:00:00-04:00",
        "done": False,
        "overdue": False,
        "estimate_minutes": None,
        "preferred_start": None,
    }
    assert got["Project 2"]["type"] == "project"
    assert got["Project 2"]["source"] == "gradescope"
    assert got["Midterm review session"]["type"] == "event"
    text = str(out)
    assert "Rubric" not in text
    assert "100" not in text


async def test_get_upcoming_work_longer_window_and_bounds(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, ids: dict[str, int]
) -> None:
    db = tmp_path / "argus.db"
    wide = await call(app_config, clock, db, "get_upcoming_work", {"days": 30})
    assert "Homework 9" in [i["title"] for i in wide.structuredContent["items"]]
    for bad in (0, 61):
        result = await call(app_config, clock, db, "get_upcoming_work", {"days": bad})
        assert result.isError


async def test_get_changes(
    db: sqlite3.Connection,
    app_config: AppConfig,
    clock: FakeClock,
    tmp_path: Path,
    ids: dict[str, int],
) -> None:
    seeded_at = clock.now()
    clock.advance(timedelta(hours=1))
    now = clock.now()
    moved = seeded_at + timedelta(days=4)
    with transaction(db):
        items = [i for i in ical_items(seeded_at, moved) if i.external_id != "late"]
        repo.upsert_source_items(
            db, [*items, src("new", "Homework 4", now + timedelta(days=7))], ids, now
        )
        for code in ("CMSC351", "MATH240"):
            repo.mark_absent(
                db,
                Source.CANVAS_ICAL,
                course_id=ids[code],
                present_external_ids={i.external_id for i in items} | {"new"},
                now=now,
                guard=False,
            )
        repo.apply_merge(db, ids, now)

    since = "2026-10-19T09:00:00-04:00"  # == seeded_at, exclusive
    result = await call(app_config, clock, tmp_path / "argus.db", "get_changes", {"since": since})
    out = result.structuredContent
    assert out["since"] == since
    assert out["as_of"] == "2026-10-19T10:00:00-04:00"
    got = {c["title"]: c for c in out["changes"]}
    assert set(got) == {"Homework 1", "Homework 3", "Homework 4"}
    assert got["Homework 1"]["status"] == "removed"
    assert got["Homework 1"]["link"] == CANVAS + "late"
    assert got["Homework 3"]["status"] == "changed"
    assert got["Homework 3"]["old_due"] == "2026-10-21T09:00:00-04:00"
    assert got["Homework 3"]["due"] == "2026-10-23T09:00:00-04:00"
    assert got["Homework 3"]["updated_at"] == "2026-10-19T10:00:00-04:00"
    assert got["Homework 4"]["status"] == "new"
    assert got["Homework 4"]["old_due"] is None

    # Everything since before the first sync is "new"; nothing after now.
    early = await call(
        app_config, clock, tmp_path / "argus.db", "get_changes", {"since": "2026-10-01T00:00:00Z"}
    )
    assert {c["status"] for c in early.structuredContent["changes"]} == {"new"}
    later = await call(
        app_config, clock, tmp_path / "argus.db", "get_changes", {"since": out["as_of"]}
    )
    assert later.structuredContent["changes"] == []


async def test_get_changes_refuses_naive_since(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, ids: dict[str, int]
) -> None:
    result = await call(
        app_config, clock, tmp_path / "argus.db", "get_changes", {"since": "2026-10-19T09:00:00"}
    )
    assert result.isError
    assert "timezone offset" in result.content[0].text


def test_server_cannot_write(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, ids: dict[str, int]
) -> None:
    from argus.store.db import connect_readonly

    conn = connect_readonly(tmp_path / "argus.db")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM items")
    conn.close()


@pytest.mark.parametrize("value", [None, "short", "has spaces " + "x" * 40, "x" * 31])
def test_refuses_missing_or_weak_secret(value: str | None) -> None:
    with pytest.raises(ValueError, match="mcp_path_secret"):
        path_secret(secrets(value))


# ---------------------------------------------------------------- HTTP edge


def _rpc(client: TestClient, path: str, host: str = HOST, **headers: str) -> Any:
    return client.post(
        path,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Host": host,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **headers,
        },
    )


def test_http_secret_path_and_host_check(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, ids: dict[str, int]
) -> None:
    app = create_mcp_app(config(app_config), secrets(), clock, tmp_path / "argus.db")
    with TestClient(app, base_url=f"https://{HOST}") as client:
        ok = _rpc(client, f"/mcp/{SECRET}")
        assert ok.status_code == 200
        assert len(ok.json()["result"]["tools"]) == 3

        for path in ("/mcp", "/", f"/mcp/{SECRET[:-1]}x", f"/mcp/{SECRET}/extra", "/sse"):
            assert _rpc(client, path).status_code == 404, path
        assert client.get("/").status_code == 404

        assert _rpc(client, f"/mcp/{SECRET}", host="evil.example.com").status_code == 421
        assert _rpc(client, f"/mcp/{SECRET}", host="127.0.0.1:8002").status_code == 200
        assert _rpc(client, f"/mcp/{SECRET}", Origin="https://evil.example").status_code == 403
        assert _rpc(client, f"/mcp/{SECRET}", Origin="https://claude.ai").status_code == 200


def test_secret_never_reaches_the_logs(
    app_config: AppConfig,
    clock: FakeClock,
    tmp_path: Path,
    ids: dict[str, int],
) -> None:
    import io

    from argus.logging import configure_logging, get_logger

    stream = io.StringIO()
    configure_logging("DEBUG", secrets=secrets().secret_values(), stream=stream)
    get_logger("test").warning("request for /mcp/%s failed", SECRET)
    assert SECRET not in stream.getvalue()


# ---------------------------------------------------------------- Runna runs (D078)

RUNNA_URL = "https://cal.runna.com/FAKEFEED.ics"


def _runs(db: sqlite3.Connection, app_config: AppConfig, now: datetime, *, running: bool) -> None:
    """Today's run (open), yesterday's run (ended, not done), in the Running course."""
    config = app_config.model_copy(update={"runna": RunnaConfig()})
    with transaction(db):
        ids = repo.sync_courses(db, config, now, running=True)
        runs = [
            SourceItem(
                Source.RUNNA_ICAL,
                ext,
                "RUN",
                title,
                ItemKind.ASSIGNMENT,
                run_due(day),
                ext,
                all_day=True,
                description="Easy Run • 4mi • 35m - 45m",
                duration_minutes=45,
                preferred_start=datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC),
            )
            for ext, title, day in (
                ("uid-today", "4mi Easy Run", now.date()),
                ("uid-yesterday", "Long Run", now.date() - timedelta(days=1)),
            )
        ]
        repo.upsert_source_items(db, runs, ids, now)
        repo.apply_merge(db, ids, now)
        repo.refresh_statuses(db, now, tracking_sources=frozenset())
        sync_run_estimates(db, config, now)
        if not running:  # Runna no longer set up: the course goes inactive
            repo.sync_courses(db, app_config, now, running=False)


async def test_upcoming_work_includes_todays_run(
    app_config: AppConfig,
    clock: FakeClock,
    tmp_path: Path,
    ids: dict[str, int],
    db: sqlite3.Connection,
) -> None:
    _runs(db, app_config, clock.now(), running=True)
    result = await call(app_config, clock, tmp_path / "argus.db", "get_upcoming_work", {"days": 7})
    out = result.structuredContent
    runs = [i for i in out["items"] if i["type"] == "run"]
    assert [r["title"] for r in runs] == ["4mi Easy Run"]  # yesterday's run has left
    run = runs[0]
    assert run["course"] == "RUN" and run["source"] == "runna"
    assert run["estimate_minutes"] == 45
    assert run["preferred_start"] == "2026-10-19T16:00:00-04:00"
    assert run["due"] == "2026-10-19T23:59:00-04:00"
    assert RUNNA_URL not in str(out) and "35m - 45m" not in str(out)


async def test_runs_hidden_when_runna_not_set_up(  # Review Focus 4
    app_config: AppConfig,
    clock: FakeClock,
    tmp_path: Path,
    ids: dict[str, int],
    db: sqlite3.Connection,
) -> None:
    _runs(db, app_config, clock.now(), running=False)
    result = await call(app_config, clock, tmp_path / "argus.db", "get_upcoming_work", {"days": 7})
    assert not [i for i in result.structuredContent["items"] if i["type"] == "run"]
    changes = await call(
        app_config, clock, tmp_path / "argus.db", "get_changes", {"since": "2026-10-01T00:00:00Z"}
    )
    assert not [c for c in changes.structuredContent["changes"] if c["course"] == "RUN"]


# --- get_goals (D079) ---------------------------------------------------------------------------


GOAL = {
    "version": 1,
    "tag": "Woodwork",
    "aim": "Build a bookshelf",
    "why": "Make something that lasts",
    "track": [
        {"id": "tools", "title": "Hand tools", "status": "done"},
        {"id": "joints", "title": "Joints", "status": "current", "done_when": "Three dovetails"},
        {"id": "finish", "title": "Finishing", "status": "next"},
    ],
    "next": [{"what": "Practice dovetails", "done_when": "No gaps"}],
    "topics": [
        {"id": "safety", "title": "Shop safety"},
        {"id": "dovetails", "title": "Dovetails", "after": ["safety"]},
    ],
}


def goals_config(app_config: AppConfig) -> AppConfig:
    return config(app_config).model_copy(update={"goals": GoalsConfig()})


def seed_goals(db: sqlite3.Connection, now: datetime, *, files: bool = True) -> None:
    from argus.planner.goals import week_of

    if not files:
        return
    week = week_of(now)
    with transaction(db):
        repo.upsert_goal_doc(
            db,
            path="Goals/Woodwork.md",
            kind="goal",
            text_hash="h",
            parsed=GoalDoc.model_validate(GOAL),
            error=None,
            now=now,
        )
        repo.upsert_goal_doc(
            db,
            path=f"Goals/Weeks/{week}.md",
            kind="week",
            text_hash="h",
            parsed=WeekDoc.model_validate(
                {
                    "version": 1,
                    "week": week,
                    "change": {"kind": "none", "what": "Baseline week", "why": "Measure first"},
                    "goals": {
                        "Woodwork": {"focus": "Joints", "target_minutes": 180, "sessions": 3}
                    },
                }
            ),
            error=None,
            now=now,
        )
        start = now + timedelta(hours=2)
        repo.apply_goal_sessions(
            db,
            [
                GoalSessionRecord(
                    "s1",
                    "Woodwork",
                    "Glue up",
                    start,
                    start + timedelta(hours=1),
                    60,
                    False,
                    "Square to 1 mm",
                    "h",
                ),
                GoalSessionRecord(
                    "s2",
                    "Woodwork",
                    "Sharpen",
                    start,
                    start + timedelta(minutes=30),
                    30,
                    False,
                    None,
                    "h2",
                ),
            ],
            [GoalSessionGone("s2", "deleted")],
            known_tags={"Woodwork"},
            now=now,
        )


async def goals_session(app_config: AppConfig, clock: FakeClock, db_path: Path, *, key: bool):
    sec = secrets().model_copy(
        update={"flowsavvy_api_key": SecretStr("fs_FAKEKEY") if key else None}
    )
    server = build_server(goals_config(app_config), sec, clock, db_path)
    async with create_connected_server_and_client_session(server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        result = await session.call_tool("get_goals", {}) if "get_goals" in tools else None
    return tools, result


async def test_get_goals_listed_read_only_with_schema(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, db: sqlite3.Connection
) -> None:
    seed_goals(db, clock.now())
    tools, _ = await goals_session(app_config, clock, tmp_path / "argus.db", key=True)
    t = tools["get_goals"]
    assert t.annotations is not None and t.annotations.readOnlyHint is True
    assert t.outputSchema is not None


async def test_get_goals_absent_without_goals_block(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, db: sqlite3.Connection
) -> None:  # Review Focus 4
    server = build_server(config(app_config), secrets(), clock, tmp_path / "argus.db")
    async with create_connected_server_and_client_session(server) as session:
        names = [t.name for t in (await session.list_tools()).tools]
    assert "get_goals" not in names


async def test_get_goals_empty_list_without_files(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, db: sqlite3.Connection
) -> None:
    _, result = await goals_session(app_config, clock, tmp_path / "argus.db", key=True)
    out = result.structuredContent
    assert out["goals"] == [] and out["problems"] == []


async def test_get_goals_shape_week_one(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, db: sqlite3.Connection
) -> None:
    seed_goals(db, clock.now())
    _, result = await goals_session(app_config, clock, tmp_path / "argus.db", key=True)
    out = result.structuredContent
    assert out["change"] == {"kind": "none", "what": "Baseline week", "why": "Measure first"}
    assert out["week_plan_missing"] is False
    assert out["rules"] == {
        "reteach_below": 60,
        "practice_below": 80,
        "review_ladder_days": [7, 14, 28],
        "on_plan_share": 0.8,
        "on_plan_weeks": 2,
    }
    (g,) = out["goals"]
    assert (g["tag"], g["status"], g["current"], g["next_milestone"]) == (
        "Woodwork",
        "active",
        "joints",
        "finish",
    )
    assert g["next"] == [{"what": "Practice dovetails", "done_when": "No gaps"}]
    w = g["week"]
    assert (w["focus"], w["target_minutes"], w["target_source"]) == ("Joints", 180, "plan")
    assert (
        w["done_minutes"],
        w["scheduled_minutes"],
        w["sessions_planned"],
        w["sessions_done"],
    ) == (0, 60, 3, 0)
    assert [s["what"] for s in w["sessions"]] == ["Glue up"]
    assert (
        w["sessions"][0]["state"] == "scheduled"
        and w["sessions"][0]["done_when"] == "Square to 1 mm"
    )
    assert [s["what"] for s in w["dropped"]] == ["Sharpen"]
    assert g["on_plan"] == {"streak_weeks": 0, "may_grow": False, "last_share": None}
    assert [t["id"] for t in g["topics"]["next_new"]] == ["safety"]
    assert g["topics"]["review_due"] == [] and g["topics"]["reteach"] == []


async def test_get_goals_without_flowsavvy_has_targets_only(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, db: sqlite3.Connection
) -> None:
    seed_goals(db, clock.now())
    _, result = await goals_session(app_config, clock, tmp_path / "argus.db", key=False)
    w = result.structuredContent["goals"][0]["week"]
    assert w["sessions_tracked"] is False and w["sessions"] == [] and w["done_minutes"] is None
    assert w["target_minutes"] == 180


async def test_get_goals_never_has_notes_or_key(
    app_config: AppConfig, clock: FakeClock, tmp_path: Path, db: sqlite3.Connection
) -> None:
    seed_goals(db, clock.now())
    _, result = await goals_session(app_config, clock, tmp_path / "argus.db", key=True)
    text = str(result.structuredContent)
    assert "fs_FAKEKEY" not in text and "Goal:" not in text
