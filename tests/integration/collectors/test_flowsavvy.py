"""FlowSavvy sync (D079): goal sessions land for known tags; a session missing from the schedule is
looked up before it's called dropped; only GET requests; the key never leaks."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from argus.clock import FakeClock
from argus.collectors.base import CollectContext
from argus.collectors.flowsavvy import BASE, FlowSavvyCollector
from argus.collectors.http import Fetcher, make_client
from argus.collectors.runner import SyncRunner
from argus.config import AppConfig, GoalsConfig, Secrets
from argus.domain.goals import GoalDoc
from argus.domain.models import Source
from argus.planner.goals import Session
from argus.store import repo
from argus.store.db import transaction
from tests.conftest import FIXTURES

KEY = "fs_FAKEKEY"
NOW = datetime(2026, 11, 12, 23, 40, tzinfo=UTC)  # Thu 18:40 local, week 46
SCHEDULE = json.loads((FIXTURES / "flowsavvy" / "schedule.json").read_text())


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / "flowsavvy" / name).read_text())


async def _no_sleep(_: float) -> None:
    return None


def goal(tag: str) -> GoalDoc:
    return GoalDoc.model_validate(
        {
            "version": 1,
            "tag": tag,
            "aim": "x",
            "track": [{"id": "a", "title": "A", "status": "current"}],
        }
    )


@pytest.fixture
def goals_config(app_config: AppConfig) -> AppConfig:
    return app_config.model_copy(update={"goals": GoalsConfig()})


@pytest.fixture
def runner(db: sqlite3.Connection, goals_config: AppConfig, clock: FakeClock) -> SyncRunner:
    clock.set(NOW)
    with transaction(db):
        for tag in ("Woodwork", "Spanish"):
            repo.upsert_goal_doc(
                db,
                path=f"Goals/{tag}.md",
                kind="goal",
                text_hash="h",
                parsed=goal(tag),
                error=None,
                now=NOW,
            )
    return SyncRunner(
        db,
        goals_config,
        Secrets(flowsavvy_api_key=SecretStr(KEY)),
        clock,
        Fetcher(make_client(), sleep=_no_sleep, rand=lambda: 0.0),
        collectors=[FlowSavvyCollector()],
    )


class FakeFlowSavvy:
    """Routes by path and query, like the real API; records every request."""

    def __init__(self, schedule: dict[str, Any] | None = None) -> None:
        self.schedule: dict[str, Any] = schedule or SCHEDULE
        self.items_open: list[dict[str, Any]] = [
            fixture_json("items_open.json"),
            fixture_json("items_open_p2.json"),
        ]
        self.items_done = fixture_json("items_done.json")
        self.by_id: dict[str, tuple[int, Any]] = {}
        self.schedule_status = 200
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        path, params = request.url.path, request.url.params
        if path == "/api/schedule":
            if self.schedule_status != 200:
                return httpx.Response(self.schedule_status)
            return httpx.Response(200, json=self.schedule)
        if path == "/api/items":
            if params.get("completed") == "true":
                return httpx.Response(200, json=self.items_done)
            page = 1 if params.get("pageToken") == "page2" else 0
            return httpx.Response(200, json=self.items_open[page])
        if path.startswith("/api/items/"):
            status, body = self.by_id.get(path.rsplit("/", 1)[1], (404, None))
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
        return httpx.Response(404)


@pytest.fixture
def api() -> Callable[..., FakeFlowSavvy]:
    def make(**kw: Any) -> FakeFlowSavvy:
        fake = FakeFlowSavvy(**kw)
        respx.route(host="my.flowsavvy.app").mock(side_effect=fake)
        return fake

    return make


def sessions(db: sqlite3.Connection) -> dict[str, Session]:
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    return {s.external_id: s for s in repo.goal_sessions_between(db, start, end)}


def last_run(db: sqlite3.Connection) -> sqlite3.Row:
    return db.execute(
        "SELECT * FROM sync_runs WHERE source = 'flowsavvy' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def with_block(item_id: str, day: int, title: str = "[Woodwork] Pushed far out") -> dict[str, Any]:
    return {
        "scheduleItems": [
            *SCHEDULE["scheduleItems"],
            {
                "itemId": item_id,
                "itemType": "task",
                "title": title,
                "startTime": f"2026-11-{day:02d}T19:00:00",
                "endTime": f"2026-11-{day:02d}T20:00:00",
                "allDay": False,
                "completed": False,
                "thisPartCompleted": False,
                "isAutoScheduled": True,
                "occurrenceDate": None,
            },
        ]
    }


# --- landing ----------------------------------------------------------------------------------


@respx.mock
async def test_sessions_land_for_known_tags(
    runner: SyncRunner, db: sqlite3.Connection, api: Callable[..., FakeFlowSavvy]
) -> None:
    api()
    await runner.run_cycle(only={Source.FLOWSAVVY})
    got = sessions(db)
    assert sorted(got) == ["r-weekend@2026-11-14", "t-late", "t-mon", "t-split"]  # no CMSC351
    assert got["t-split"].done_when == "carcass glued up square"
    assert got["t-mon"].done_when == "two joints with no gaps" and got["t-mon"].completed
    assert got["r-weekend@2026-11-14"].done_when == "20 new verbs in sentences"
    assert last_run(db)["status"] == "ok"


@respx.mock
async def test_first_sync_backfills_then_uses_the_cursor(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = api()
    await runner.run_cycle(only={Source.FLOWSAVVY})
    schedules = [c for c in fake.calls if c.url.path == "/api/schedule"]
    assert [(c.url.params["startDate"], c.url.params["endDate"]) for c in schedules] == [
        ("2026-11-02", "2026-11-22"),
        ("2026-10-12", "2026-11-01"),
    ]
    first_items = [c for c in fake.calls if c.url.path == "/api/items"]
    assert {c.url.params["modifiedAfter"] for c in first_items} == {"2026-10-26T05:00:00Z"}
    fake.calls.clear()
    clock.advance(timedelta(minutes=30))
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert len([c for c in fake.calls if c.url.path == "/api/schedule"]) == 1
    cursors = {c.url.params["modifiedAfter"] for c in fake.calls if c.url.path == "/api/items"}
    assert cursors == {"2026-11-12T23:38:00Z"}  # the last fetch, minus two minutes of overlap


# --- moved vs dropped (Review Focus 3) --------------------------------------------------------


async def _sync_then_vanish(
    runner: SyncRunner,
    clock: FakeClock,
    api: Callable[..., FakeFlowSavvy],
    item_id: str,
    day: int = 13,
) -> FakeFlowSavvy:
    fake = api(schedule=with_block(item_id, day))
    await runner.run_cycle(only={Source.FLOWSAVVY})
    fake.schedule = SCHEDULE
    clock.advance(timedelta(minutes=30))
    return fake


@respx.mock
async def test_session_pushed_past_window_is_moved_not_dropped(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = await _sync_then_vanish(runner, clock, api, "t-gone")
    fake.by_id["t-gone"] = (200, fixture_json("item_exists_open.json"))
    await runner.run_cycle(only={Source.FLOWSAVVY})
    s = sessions(db)["t-gone"]
    assert s.moved_at is not None and s.gone_at is None


@respx.mock
async def test_deleted_item_is_dropped(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    await _sync_then_vanish(runner, clock, api, "t-gone")
    await runner.run_cycle(only={Source.FLOWSAVVY})  # by_id has nothing: 404
    s = sessions(db)["t-gone"]
    assert s.gone_at == NOW + timedelta(minutes=30) and s.moved_at is None


@respx.mock
async def test_cancelled_occurrence_is_dropped(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    schedule = with_block("r-series", 7, "[Spanish] Weekend session")
    schedule["scheduleItems"][-1]["occurrenceDate"] = "2026-11-07"
    fake = api(schedule=schedule)
    await runner.run_cycle(only={Source.FLOWSAVVY})
    fake.schedule = SCHEDULE
    fake.by_id["r-series"] = (200, fixture_json("series_cancelled.json"))
    clock.advance(timedelta(minutes=30))
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert sessions(db)["r-series@2026-11-07"].gone_at is not None


@respx.mock
async def test_completed_while_missing_is_done(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = await _sync_then_vanish(runner, clock, api, "t-gone")
    fake.by_id["t-gone"] = (200, fixture_json("item_completed.json"))
    await runner.run_cycle(only={Source.FLOWSAVVY})
    s = sessions(db)["t-gone"]
    assert s.completed and s.gone_at is None


@respx.mock
async def test_lookup_429_decides_nothing(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = await _sync_then_vanish(runner, clock, api, "t-gone")
    fake.by_id["t-gone"] = (429, None)
    await runner.run_cycle(only={Source.FLOWSAVVY})
    s = sessions(db)["t-gone"]
    assert s.gone_at is None and s.moved_at is None
    assert last_run(db)["status"] == "ok"  # the sessions themselves synced


@respx.mock
async def test_lookups_capped_at_10(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    schedule = {"scheduleItems": [*SCHEDULE["scheduleItems"]]}
    for i in range(12):
        schedule["scheduleItems"] += with_block(f"t-{i:02d}", 13 + i % 5)["scheduleItems"][-1:]
    fake = api(schedule=schedule)
    await runner.run_cycle(only={Source.FLOWSAVVY})
    fake.schedule = SCHEDULE
    fake.calls.clear()
    clock.advance(timedelta(minutes=30))
    await runner.run_cycle(only={Source.FLOWSAVVY})
    lookups = [c for c in fake.calls if c.url.path.startswith("/api/items/")]
    assert len(lookups) == 10
    assert sum(1 for s in sessions(db).values() if s.gone_at is not None) == 10


@respx.mock
async def test_aged_out_sessions_are_not_looked_up(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = await _sync_then_vanish(runner, clock, api, "t-old", day=13)
    clock.set(NOW + timedelta(days=21))  # the session's week has left the window
    fake.calls.clear()
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert not [c for c in fake.calls if c.url.path.startswith("/api/items/")]
    assert sessions(db)["t-old"].gone_at is None


# --- safety -----------------------------------------------------------------------------------


@respx.mock
async def test_collector_only_sends_get(
    runner: SyncRunner, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = await _sync_then_vanish(runner, clock, api, "t-gone")
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert fake.calls and {c.method for c in fake.calls} == {"GET"}


@respx.mock
async def test_429_on_schedule_is_error_without_retry(
    runner: SyncRunner, db: sqlite3.Connection, api: Callable[..., FakeFlowSavvy]
) -> None:  # Review Focus 6
    fake = api()
    fake.schedule_status = 429
    await runner.run_cycle(only={Source.FLOWSAVVY})
    run = last_run(db)
    assert (run["status"], run["error"]) == ("error", "FlowSavvy rate limit")
    assert len(fake.calls) == 1 and sessions(db) == {}


@respx.mock
async def test_auth_error(
    runner: SyncRunner, db: sqlite3.Connection, api: Callable[..., FakeFlowSavvy]
) -> None:
    api().schedule_status = 401
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert last_run(db)["status"] == "auth_error"


@respx.mock
async def test_key_never_in_logs_or_sync_runs(
    runner: SyncRunner,
    db: sqlite3.Connection,
    api: Callable[..., FakeFlowSavvy],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    api().schedule_status = 500
    await runner.run_cycle(only={Source.FLOWSAVVY})
    run = last_run(db)
    assert run["status"] == "error" and KEY not in (run["error"] or "")
    assert KEY not in caplog.text


def test_disabled_without_goals_block(app_config: AppConfig, clock: FakeClock) -> None:
    ctx = CollectContext(
        config=app_config,
        secrets=Secrets(flowsavvy_api_key=SecretStr(KEY)),
        clock=clock,
        fetcher=Fetcher(make_client()),
    )
    assert app_config.goals is None and not FlowSavvyCollector().enabled(ctx)


def test_base_is_flowsavvy() -> None:
    assert BASE == "https://my.flowsavvy.app"


# --- review fixes ---------------------------------------------------------------------------


@respx.mock
async def test_404_on_schedule_is_an_error_not_an_empty_week(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = await _sync_then_vanish(runner, clock, api, "t-gone")
    fake.schedule_status = 404
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert last_run(db)["status"] == "error"
    s = sessions(db)["t-gone"]
    assert s.gone_at is None and s.moved_at is None
    assert not [c for c in fake.calls if c.url.path.startswith("/api/items/")]


@respx.mock
async def test_a_malformed_block_is_skipped_not_fatal(
    runner: SyncRunner, db: sqlite3.Connection, api: Callable[..., FakeFlowSavvy]
) -> None:
    bad = {"itemId": "t-bad", "itemType": "task", "title": "[Woodwork] Broken", "startTime": "soon"}
    api(schedule={"scheduleItems": [*SCHEDULE["scheduleItems"], bad]})
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert last_run(db)["status"] == "ok"
    assert "t-split" in sessions(db) and "t-bad" not in sessions(db)


@respx.mock
async def test_sessions_of_a_removed_goal_are_not_looked_up(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = await _sync_then_vanish(runner, clock, api, "t-gone")
    with transaction(db):
        repo.mark_goal_docs_missing(
            db, {"Goals/Spanish.md"}, clock.now()
        )  # Woodwork's file is gone
    fake.calls.clear()
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert not [c for c in fake.calls if c.url.path.startswith("/api/items/")]


@respx.mock
async def test_backfill_runs_once_even_with_no_sessions(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock, api: Callable[..., FakeFlowSavvy]
) -> None:
    fake = api(schedule={"scheduleItems": []})
    await runner.run_cycle(only={Source.FLOWSAVVY})
    fake.calls.clear()
    clock.advance(timedelta(minutes=30))
    await runner.run_cycle(only={Source.FLOWSAVVY})
    assert len([c for c in fake.calls if c.url.path == "/api/schedule"]) == 1
