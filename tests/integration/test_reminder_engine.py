import json
import sqlite3
from datetime import date, time, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from argus.clock import FakeClock, local_datetime
from argus.collectors.http import Fetcher, make_client
from argus.config import AppConfig
from argus.domain.models import ItemKind, MeetingRecord, Source, SourceItem, Weekday
from argus.notify.ntfy import NtfyChannel
from argus.notify.router import Router
from argus.reminders.engine import ReminderEngine
from argus.reminders.guards import Verdict
from argus.reminders.rules import Rule
from argus.store import repo
from argus.store.db import transaction
from tests.caps import EVERY_SOURCE, tracked_caps

NTFY = "https://ntfy.example.test/"


async def _no_sleep(_: float) -> None:
    return None


def fetcher() -> Fetcher:
    return Fetcher(make_client(), sleep=_no_sleep, rand=lambda: 0.0, max_attempts=1)


def engine(
    db: sqlite3.Connection, cfg: AppConfig, clock: FakeClock, topic: str | None = "argus-topic"
) -> ReminderEngine:
    router = Router([NtfyChannel(fetcher(), "https://ntfy.example.test", topic)])
    # These tests are about deadline rules; the morning brief has its own tests (test_sms_rules.py).
    late = cfg.schedule.model_copy(update={"morning_brief": time(23, 59)})
    return ReminderEngine(
        db, cfg.model_copy(update={"schedule": late}), clock, router, caps=tracked_caps(cfg)
    )


def seed(
    db: sqlite3.Connection, cfg: AppConfig, clock: FakeClock, dues: dict[str, timedelta]
) -> dict[str, int]:
    """Items imported long ago (not 'new'), due at now + offset."""
    now = clock.now()
    with transaction(db):
        ids = repo.sync_courses(db, cfg, now)
        repo.record_sync_run(
            db,
            source="canvas_ical",
            started_at=now - timedelta(days=5),
            finished_at=now - timedelta(days=5),
            status="ok",
        )
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_ICAL, ext, "CMSC351", ext, ItemKind.ASSIGNMENT, now + off, "h"
                )
                for ext, off in dues.items()
            ],
            ids,
            now,
        )
        repo.apply_merge(db, ids, now)
        db.execute(
            "UPDATE items SET created_at = ?",
            ((now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        repo.refresh_statuses(db, now, tracking_sources=EVERY_SOURCE)
    return {i.title: i.id for i in repo.list_items(db)}


@pytest.fixture
def noon(clock: FakeClock) -> FakeClock:
    clock.set(local_datetime(date(2026, 10, 19), 12))  # Mon noon EDT
    return clock


@respx.mock
async def test_sends_once_with_ntfy_payload(
    db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock
) -> None:
    route = respx.post(NTFY).respond(200, json={"id": "x"})
    ids = seed(
        db, app_config, noon, {"Homework 3": timedelta(hours=2), "Homework 4": timedelta(hours=20)}
    )
    eng = engine(db, app_config, noon)
    outcomes = await eng.tick()
    assert [(o.rule, o.reason, o.channel) for o in outcomes] == [
        (Rule.DUE_3H, "sent", "ntfy"),
        (Rule.DUE_24H, "sent", "ntfy"),
    ]
    first = json.loads(route.calls[0].request.content)
    assert first == {
        "topic": "argus-topic",
        "title": "Due in 2h",
        # iCal-only items: no source reports submissions, so Argus asks instead (D056).
        "message": "[1] CMSC351 Homework 3, Mon 2pm. Reply DONE 1 once submitted.",
        "priority": 5,
        "tags": ["rotating_light"],
    }
    sent = repo.recent_notifications(db)
    assert {n.status for n in sent} == {"sent"}
    assert sent[-1].item_ids == (ids["Homework 3"],)

    noon.advance(timedelta(minutes=1))
    assert [o for o in await eng.tick() if o.verdict is Verdict.SEND] == []
    assert route.call_count == 2


@respx.mock
async def test_failed_send_is_retried_without_duplicates(
    db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock
) -> None:
    route = respx.post(NTFY).mock(
        side_effect=[httpx.Response(500), httpx.Response(200), httpx.Response(200)]
    )
    seed(db, app_config, noon, {"Homework 3": timedelta(hours=2)})
    eng = engine(db, app_config, noon)
    [o] = await eng.tick()
    assert o.reason == "delivery failed"
    [row] = repo.recent_notifications(db)
    assert row.status == "failed"
    assert row.error and "500" in row.error
    noon.advance(timedelta(minutes=1))
    await eng.tick()
    [row] = repo.recent_notifications(db)
    assert row.status == "sent"
    assert row.attempts == 2
    noon.advance(timedelta(minutes=1))
    await eng.tick()
    assert route.call_count == 2


@respx.mock
async def test_outage_longer_than_three_ticks_still_delivers_the_warning(
    db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock
) -> None:
    """Review R4: retries continue (with backoff) until the reminder expires."""
    outage_until = noon.now() + timedelta(minutes=20)

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if noon.now() < outage_until else 200)

    route = respx.post(NTFY).mock(side_effect=flaky)
    seed(db, app_config, noon, {"Homework 3": timedelta(hours=2)})
    eng = engine(db, app_config, noon)
    for _ in range(40):
        await eng.tick()
        noon.advance(timedelta(minutes=1))
    [row] = repo.recent_notifications(db)
    assert row.status == "sent" and row.rule == "due_3h"
    # Backoff: 1, 2, 4, 8, 15 minutes between tries, so about 6 attempts, not one per tick.
    assert 4 <= route.call_count <= 8, route.call_count
    assert repo.failing_deadline_sends(db, noon.now()) == 0


@respx.mock
async def test_retries_respect_quiet_hours_and_report_broken_delivery(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock
) -> None:
    respx.post(NTFY).respond(500)
    clock.set(local_datetime(date(2026, 10, 19), 22, 50))
    seed(db, app_config, clock, {"Tomorrow": timedelta(hours=12)})
    eng = engine(db, app_config, clock)
    await eng.tick()  # 22:50: due_24h claimed, delivery fails
    [row] = repo.recent_notifications(db)
    assert (row.rule, row.status, row.attempts) == ("due_24h", "failed", 1)
    clock.set(local_datetime(date(2026, 10, 19), 23, 30))  # quiet hours: the retry waits
    await eng.tick()
    assert repo.recent_notifications(db)[0].attempts == 1
    clock.set(local_datetime(date(2026, 10, 20), 8, 0))
    for _ in range(3):
        await eng.tick()
        clock.advance(timedelta(minutes=10))
    assert repo.recent_notifications(db)[0].attempts >= 3
    assert repo.failing_deadline_sends(db, clock.now()) == 1  # the heartbeat reports /fail


@respx.mock
async def test_quiet_hours_defer_24h_but_not_3h(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock
) -> None:
    respx.post(NTFY).respond(200)
    clock.set(local_datetime(date(2026, 10, 19), 23, 30))
    seed(db, app_config, clock, {"Night owl": timedelta(hours=1), "Tomorrow": timedelta(hours=12)})
    outcomes = {o.rule: o for o in await engine(db, app_config, clock).tick()}
    assert outcomes[Rule.DUE_3H].reason == "sent"
    assert (outcomes[Rule.DUE_24H].verdict, outcomes[Rule.DUE_24H].reason) == (
        Verdict.DEFER,
        "quiet hours",
    )
    clock.set(local_datetime(date(2026, 10, 20), 8))
    outcomes = {o.rule: o for o in await engine(db, app_config, clock).tick()}
    assert outcomes[Rule.DUE_24H].reason == "sent"


@respx.mock
async def test_class_time_defers(
    db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock
) -> None:
    respx.post(NTFY).respond(200)
    ids = seed(db, app_config, noon, {"HW": timedelta(hours=10)})
    assert ids
    lecture = MeetingRecord("CMSC351", "lecture", (Weekday.MON,), time(11, 30), time(12, 45))
    with transaction(db):
        repo.replace_meetings(db, repo.course_ids(db)["CMSC351"], "umdio", [lecture], noon.now())
    [o] = await engine(db, app_config, noon).tick()
    assert (o.verdict, o.reason) == (Verdict.DEFER, "in class")


@respx.mock
async def test_daily_cap(db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock) -> None:
    route = respx.post(NTFY).respond(200)
    dues = {f"HW {i}": timedelta(hours=10, minutes=i) for i in range(8)}
    dues["Urgent"] = timedelta(hours=1)
    seed(db, app_config, noon, dues)
    outcomes = await engine(db, app_config, noon).tick()
    sent = [o for o in outcomes if o.reason == "sent"]
    capped = [o for o in outcomes if o.reason == "daily cap reached"]
    assert len(sent) == 7  # 6 capped + the uncapped 3-hour warning
    assert len(capped) == 2
    assert route.call_count == 7


@respx.mock
async def test_muted_rule_and_missing_channel(
    db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock
) -> None:
    seed(db, app_config, noon, {"HW": timedelta(hours=10), "Soon": timedelta(hours=1)})
    with transaction(db):
        repo.set_rule_mute(db, "due_24h", True, noon.now())
    outcomes = {o.rule: o for o in await engine(db, app_config, noon, topic=None).tick()}
    assert outcomes[Rule.DUE_24H].reason == "rule muted"
    assert outcomes[Rule.DUE_3H].reason == "delivery failed"
    [row] = repo.recent_notifications(db)
    assert row.error == "no channel configured"
    with transaction(db):
        repo.set_rule_mute(db, "due_24h", False, noon.now())
        repo.set_rule_mute(db, "due_3h", True, noon.now(), until=noon.now() - timedelta(minutes=1))
    assert repo.muted_rules(db, noon.now()) == set()


@respx.mock
async def test_new_items_after_bootstrap(
    db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock
) -> None:
    route = respx.post(NTFY).respond(200)
    seed(db, app_config, noon, {"Old": timedelta(days=5)})
    with transaction(db):
        ids = repo.course_ids(db)
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.GRADESCOPE,
                    "g9",
                    "CMSC351",
                    "Quiz 9",
                    ItemKind.QUIZ,
                    noon.now() + timedelta(days=2),
                    "h",
                )
            ],
            ids,
            noon.now(),
        )
        repo.apply_merge(db, ids, noon.now())
    outcomes = await engine(db, app_config, noon).tick()
    assert [(o.rule, o.reason) for o in outcomes] == [(Rule.NEW_ITEM, "sent")]
    assert json.loads(route.calls[0].request.content)["title"] == "New item"


@respx.mock
async def test_system_alert_after_three_failures(
    db: sqlite3.Connection, app_config: AppConfig, noon: FakeClock
) -> None:
    route = respx.post(NTFY).respond(200)
    seed(db, app_config, noon, {})
    with transaction(db):
        for k in range(3):
            t = noon.now() - timedelta(minutes=90 - 30 * k)
            repo.record_sync_run(
                db, source="gradescope", started_at=t, finished_at=t, status="error", error="boom"
            )
    outcomes = await engine(db, app_config, noon).tick()
    assert [(o.rule, o.reason) for o in outcomes] == [(Rule.SYSTEM_ALERT, "sent")]
    assert json.loads(route.calls[0].request.content)["title"] == "Gradescope sync failing"


def test_claim_is_atomic_across_keys(db: sqlite3.Connection, clock: FakeClock) -> None:
    common = {
        "rule": "new_item",
        "title": "t",
        "body": "b",
        "item_ids": [1],
        "priority": 3,
        "counts_toward_cap": True,
        "now": clock.now(),
    }
    assert repo.claim_notification(db, keys=["a", "b"], **common) is not None
    assert repo.claim_notification(db, keys=["b", "c"], **common) is None
    assert repo.notification_keys(db) == {"a", "b"}
    with pytest.raises(ValueError, match="at least one"):
        repo.claim_notification(db, keys=[], **common)


@respx.mock
def test_notify_test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from argus import cli

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "notifications: {ntfy_server: 'https://ntfy.example.test'}\n"
        f"paths: {{data_dir: '{tmp_path}'}}\n"
    )
    route = respx.post(NTFY).respond(200)
    monkeypatch.setenv("ARGUS_NTFY_TOPIC", "argus-topic")
    result = CliRunner().invoke(cli.app, ["notify-test", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "sent via ntfy"
    assert route.called
    monkeypatch.delenv("ARGUS_NTFY_TOPIC")
    failed = CliRunner().invoke(cli.app, ["notify-test", "-c", str(cfg)])
    assert failed.exit_code == 1
    assert "no channel configured" in failed.output
