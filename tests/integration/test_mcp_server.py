"""The Claude connector (argus/web/mcp_server.py, D073): the three tools over a seeded database,
and the HTTP edge (path secret, Host check)."""

import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr
from starlette.testclient import TestClient

from argus.clock import FakeClock
from argus.config import AppConfig, MCPConfig, Secrets
from argus.domain.models import ItemKind, MeetingRecord, Source, SourceItem, Weekday
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
