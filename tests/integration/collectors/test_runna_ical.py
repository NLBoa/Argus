"""Runna sync (D078): runs land in the Running course, move, disappear, and get done."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from argus.clock import FakeClock
from argus.collectors.base import RunStatus
from argus.collectors.http import Fetcher, make_client
from argus.collectors.runna_ical import RunnaIcalCollector
from argus.collectors.runner import SyncRunner
from argus.config import AppConfig, RunnaConfig, Secrets
from argus.domain.changes import summarize_changes
from argus.domain.models import Item, ItemStatus, Source
from argus.logging import redactor
from argus.reminders.rules import Rule, item_key
from argus.store import repo
from tests.integration.conftest import fixture_bytes

RUNNA_URL = "https://cal.runna.com/FAKEFEED.ics"
FEED = fixture_bytes("runna", "feed.ics")
FEED_REPLACED = fixture_bytes("runna", "feed_replaced.ics")
FEED_REPLACED_RENAMED = fixture_bytes("runna", "feed_replaced_renamed.ics")
START = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
PLAN = b"00000000-0000-4000-8000-000000000001_plan_week_6"


async def _no_sleep(_: float) -> None:
    return None


def make_runner(
    db: sqlite3.Connection, config: AppConfig, secrets: Secrets, clock: FakeClock
) -> SyncRunner:
    return SyncRunner(
        db,
        config,
        secrets,
        clock,
        Fetcher(make_client(), sleep=_no_sleep, rand=lambda: 0.0),
        collectors=[RunnaIcalCollector()],
    )


@pytest.fixture
def runna_config(app_config: AppConfig) -> AppConfig:
    return app_config.model_copy(update={"runna": RunnaConfig()})


@pytest.fixture
def runna_secrets() -> Secrets:
    return Secrets(runna_ical_url=SecretStr(RUNNA_URL))


@pytest.fixture
def runner(
    db: sqlite3.Connection, runna_config: AppConfig, runna_secrets: Secrets, clock: FakeClock
) -> SyncRunner:
    clock.set(START)
    return make_runner(db, runna_config, runna_secrets, clock)


def serve(body: bytes) -> None:
    respx.get(RUNNA_URL).respond(200, content=body)


def runs(db: sqlite3.Connection) -> dict[str, Item]:
    return {i.title: i for i in repo.list_items(db) if i.course_code == "RUN"}


def item_ids(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        """SELECT s.title, s.item_id FROM item_sources s
           WHERE s.source = 'runna_ical' AND s.item_id IS NOT NULL"""
    )
    return {r["title"]: r["item_id"] for r in rows}


def change_status(db: sqlite3.Connection, item_id: int, since: datetime = EPOCH) -> str | None:
    events = [e for e in repo.item_changes_since(db, since) if e.item_id == item_id]
    item = repo.get_item(db, item_id)
    net = summarize_changes(events, {item_id: item.due_at if item else None})
    return net[item_id].status if item_id in net else None


def without(body: bytes, uid_part: bytes) -> bytes:
    start = body.index(b"BEGIN:VEVENT\r\nUID:" + uid_part)
    end = body.index(b"END:VEVENT\r\n", start) + len(b"END:VEVENT\r\n")
    return body[:start] + body[end:]


@respx.mock
async def test_runs_land_in_the_running_course(runner: SyncRunner, db: sqlite3.Connection) -> None:
    serve(FEED)
    report = await runner.run_cycle()
    assert {r.source: r.status for r in report.results} == {Source.RUNNA_ICAL: RunStatus.OK}
    got = runs(db)
    # (e), on Nov 1, is stored: the lookback only protects it from removal.
    assert set(got) == {
        "Tempo Run",
        "4mi Easy Run",
        "Steady Run • 5mi",
        "Night Run",
        "6.5mi Long Run",
        "Recovery Run",
    }
    assert repo.active_course_titles(db)["RUN"] == "Running"
    details = repo.run_details(db, [got["4mi Easy Run"].id])
    assert details[got["4mi Easy Run"].id].duration_minutes == 50
    assert details[got["4mi Easy Run"].id].preferred_start == datetime(
        2026, 11, 11, 21, 0, tzinfo=UTC
    )


@respx.mock
async def test_second_identical_fetch_is_unchanged(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    respx.get(RUNNA_URL).respond(200, content=FEED, headers={"ETag": '"v1"'})
    await runner.run_cycle()
    respx.get(RUNNA_URL).respond(304)
    clock.advance(timedelta(hours=1))
    report = await runner.run_cycle(force=True)
    assert report.results[0].status is RunStatus.UNCHANGED
    assert len(runs(db)) == 6


@respx.mock
async def test_deleted_workout_is_removed(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    serve(FEED)
    await runner.run_cycle()
    easy = item_ids(db)["4mi Easy Run"]
    serve(without(FEED, b"UPCOMING_PLAN_WORKOUT-" + PLAN + b"_EASY_RUN_0"))
    since = clock.now()
    clock.advance(timedelta(hours=1))
    await runner.run_cycle(force=True)
    assert "4mi Easy Run" not in runs(db)
    assert change_status(db, easy, since) == "removed"


@respx.mock
async def test_moved_workout_rearms_reminders(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    serve(FEED)
    await runner.run_cycle()
    before = runs(db)["4mi Easy Run"]
    moved = FEED.replace(b"DTSTART;VALUE=DATE:20261111", b"DTSTART;VALUE=DATE:20261112").replace(
        b"DTEND;VALUE=DATE:20261112\r\nSUMMARY:\xf0\x9f\x8f\x83 4mi",
        b"DTEND;VALUE=DATE:20261113\r\nSUMMARY:\xf0\x9f\x8f\x83 4mi",
    )
    assert moved != FEED
    serve(moved)
    since = clock.now()  # changes are logged strictly after `since`
    clock.advance(timedelta(hours=1))
    await runner.run_cycle(force=True)
    after = runs(db)["4mi Easy Run"]
    assert after.id == before.id
    assert after.due_at == datetime(2026, 11, 13, 4, 59, tzinfo=UTC)  # 23:59 EST Nov 12
    assert change_status(db, after.id, since) == "changed"
    assert item_key(after, Rule.DUE_24H) != item_key(before, Rule.DUE_24H)


@respx.mock
async def test_completed_run_is_submitted_and_never_missed(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    serve(FEED)
    clock.set(datetime(2026, 11, 13, 12, 0, tzinfo=UTC))
    await runner.run_cycle()
    got = runs(db)
    assert got["Tempo Run"].status is ItemStatus.SUBMITTED  # completed twin in the feed
    assert got["Steady Run • 5mi"].status is not ItemStatus.MISSED  # past its day, not done


@respx.mock
async def test_completed_replacing_planned_is_submitted_not_removed(  # Review Focus 5
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    serve(without(FEED, b"COMPLETED_PLAN_WORKOUT-AAAAAAAA-0000-4000-8000-00000000000A"))
    await runner.run_cycle()  # fetch 1: the planned Tempo Run, not yet done
    tempo = item_ids(db)["Tempo Run"]
    assert runs(db)["Tempo Run"].status is ItemStatus.UPCOMING
    since = clock.now()
    serve(FEED_REPLACED)
    clock.set(datetime(2026, 11, 10, 23, 0, tzinfo=UTC))
    await runner.run_cycle(force=True)  # fetch 2: replaced by its completed event
    assert runs(db)["Tempo Run"].status is ItemStatus.SUBMITTED
    assert change_status(db, tempo, since) is None  # present throughout, same due date
    clock.advance(timedelta(hours=1))
    await runner.run_cycle(force=True)  # and it stays that way
    assert runs(db)["Tempo Run"].status is ItemStatus.SUBMITTED


@respx.mock
async def test_renamed_completed_replacing_planned_is_submitted(  # case (c), renamed
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    serve(without(FEED, b"COMPLETED_PLAN_WORKOUT-AAAAAAAA-0000-4000-8000-00000000000A"))
    await runner.run_cycle()
    tempo = item_ids(db)["Tempo Run"]
    since = clock.now()
    serve(FEED_REPLACED_RENAMED)  # "Morning Run" is the only completion that day
    clock.set(datetime(2026, 11, 10, 23, 0, tzinfo=UTC))
    await runner.run_cycle(force=True)
    assert runs(db)["Tempo Run"].status is ItemStatus.SUBMITTED
    assert change_status(db, tempo, since) is None
    clock.advance(timedelta(hours=1))
    await runner.run_cycle(force=True)  # done now, so it is found again by its hash
    assert runs(db)["Tempo Run"].status is ItemStatus.SUBMITTED
    assert change_status(db, tempo, since) is None


@respx.mock
@pytest.mark.parametrize("half", ["block_only", "secret_only"])
async def test_half_setup_has_no_running_course(  # Review Focus 4
    half: str,
    db: sqlite3.Connection,
    app_config: AppConfig,
    runna_config: AppConfig,
    runna_secrets: Secrets,
    clock: FakeClock,
) -> None:
    serve(FEED)
    config, secrets = (
        (runna_config, Secrets()) if half == "block_only" else (app_config, runna_secrets)
    )
    await make_runner(db, config, secrets, clock).run_cycle()
    assert "RUN" not in repo.active_course_titles(db)
    assert runs(db) == {}


@respx.mock
async def test_runna_turned_off_hides_stored_runs(
    runner: SyncRunner, db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock
) -> None:
    serve(FEED)
    await runner.run_cycle()
    assert runs(db)
    await make_runner(db, app_config, Secrets(), clock).run_cycle()
    assert "RUN" not in repo.active_course_titles(db)


@respx.mock
async def test_fetch_error_is_redacted(
    runner: SyncRunner, db: sqlite3.Connection, runna_secrets: Secrets
) -> None:
    redactor.register(runna_secrets.secret_values())  # as configure_logging does at startup
    respx.get(RUNNA_URL).mock(side_effect=httpx.ConnectError(f"cannot reach {RUNNA_URL}"))
    report = await runner.run_cycle()
    assert report.results[0].status is RunStatus.ERROR
    health = repo.source_health(db, ["runna_ical"])["runna_ical"]
    assert health.last_error is not None
    assert RUNNA_URL not in health.last_error
