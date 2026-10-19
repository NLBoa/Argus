import sqlite3
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from argus.ai.client import AIClient
from argus.clock import FakeClock
from argus.config import AIConfig, AppConfig
from argus.domain.models import ItemKind, Source, SourceItem
from argus.estimates import (
    clear_override,
    effective_estimates,
    ensure_estimates,
    estimate_item,
    input_hash,
    needs_estimate,
    set_override,
)
from argus.store import repo
from argus.store.db import transaction
from tests.ai_fakes import FakeTransport, ok, recorded


@pytest.fixture
def items(db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock) -> dict[str, Any]:
    now = clock.now()
    with transaction(db):
        ids = repo.sync_courses(db, app_config, now)
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_API,
                    "hw3",
                    "CMSC351",
                    "Homework 3",
                    ItemKind.ASSIGNMENT,
                    now + timedelta(days=3),
                    "h",
                    points=30,
                    description="Prove the three lemmas.",
                ),
                SourceItem(
                    Source.CANVAS_API,
                    "q2",
                    "CMSC351",
                    "Quiz 2",
                    ItemKind.QUIZ,
                    now + timedelta(days=1),
                    "h",
                ),
                SourceItem(
                    Source.CANVAS_API,
                    "ev",
                    "CMSC351",
                    "Office hours",
                    ItemKind.EVENT,
                    now + timedelta(days=1),
                    "h",
                ),
                SourceItem(
                    Source.CANVAS_API,
                    "hw1",
                    "CMSC351",
                    "Homework 1",
                    ItemKind.ASSIGNMENT,
                    now - timedelta(days=20),
                    "h",
                    submitted=True,
                ),
                SourceItem(
                    Source.CANVAS_API,
                    "hw2",
                    "CMSC351",
                    "Homework 2",
                    ItemKind.ASSIGNMENT,
                    now - timedelta(days=10),
                    "h",
                    submitted=True,
                ),
            ],
            ids,
            now,
        )
        repo.apply_merge(db, ids, now)
        repo.refresh_statuses(db, now)
    return {i.title: i for i in repo.list_items(db)}


def ai(db: sqlite3.Connection, clock: FakeClock, transport: FakeTransport | None) -> AIClient:
    return AIClient(db, AIConfig(), clock, transport)


async def test_ai_estimate_is_stored_with_provenance(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    t = FakeTransport([ok(recorded("estimate_hw3"), model="claude-sonnet-5-5")])
    row = await estimate_item(db, ai(db, clock, t), app_config, clock, items["Homework 3"])
    assert (row.source, row.p50, row.p80, row.version) == ("ai", 3.5, 5.0, 1)
    assert row.model == "claude-sonnet-5-5" and row.prompt_version == "estimate.v1"
    assert row.breakdown[1] == ("Write up the three proofs", 2.5)
    assert t.calls[0]["model"] == "claude-sonnet-5-5"
    assert "Prove the three lemmas." in t.calls[0]["user"]
    assert "never drafts answers" in t.calls[0]["system"]


async def test_fallback_without_ai_uses_typical_then_median(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    row = await estimate_item(db, ai(db, clock, None), app_config, clock, items["Homework 3"])
    assert (row.source, row.p50, row.p80) == ("default", 3.0, 4.5)
    assert (row.reasoning or "").startswith("Typical for an assignment; no history yet.")
    # Log actuals on finished homework: the fallback becomes their median.
    with transaction(db):
        repo.log_time(db, items["Homework 1"].id, 2.0, clock.now() - timedelta(days=19))
        repo.log_time(db, items["Homework 2"].id, 4.0, clock.now() - timedelta(days=9))
    row2 = await estimate_item(db, ai(db, clock, None), app_config, clock, items["Homework 3"])
    assert (row2.p50, row2.version) == (3.0, 2)  # median of 2 and 4
    assert "Median of your past" in (row2.reasoning or "")


async def test_invalid_ai_output_falls_back(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    bad = {**recorded("estimate_hw3"), "p80_hours": 1.0}  # p80 < p50 fails validation
    t = FakeTransport([ok(bad), ok(bad)])
    row = await estimate_item(db, ai(db, clock, t), app_config, clock, items["Homework 3"])
    assert row.source == "default"
    assert "invalid" in (row.reasoning or "")


async def test_ensure_estimates_runs_once_per_input(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    c = ai(db, clock, FakeTransport(responder=lambda req: ok(recorded("estimate_hw3"))))
    assert (
        await ensure_estimates(db, c, app_config, clock) == 2
    )  # Homework 3 and Quiz 2: not events or finished items
    assert await ensure_estimates(db, c, app_config, clock) == 0
    latest = repo.latest_estimates(db)
    hw3 = items["Homework 3"]
    assert latest[hw3.id][0] is not None
    changed = replace(hw3, description="Prove the four lemmas.")
    assert needs_estimate(changed, latest[hw3.id][0], True, clock.now())
    assert input_hash(changed) != input_hash(hw3)


def test_needs_estimate_retry_of_defaults(
    items: dict[str, Any], clock: FakeClock, db: sqlite3.Connection
) -> None:
    hw3 = items["Homework 3"]
    with transaction(db):
        repo.insert_estimate(
            db,
            item_id=hw3.id,
            source="default",
            p50=3,
            p80=4.5,
            input_hash=input_hash(hw3),
            now=clock.now(),
        )
    default = repo.estimates_for(db, hw3.id)[-1]
    assert not needs_estimate(hw3, default, True, clock.now())
    assert needs_estimate(hw3, default, True, clock.now() + timedelta(hours=25))
    assert not needs_estimate(hw3, default, False, clock.now() + timedelta(hours=25))
    assert not needs_estimate(items["Office hours"], None, True, clock.now())


async def test_override_wins_and_versions_are_kept(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    hw3 = items["Homework 3"]
    await estimate_item(
        db, ai(db, clock, FakeTransport([ok(recorded("estimate_hw3"))])), app_config, clock, hw3
    )
    set_override(db, hw3, 6.0, clock.now())
    eff = effective_estimates(db, clock.now())[hw3.id]
    assert (eff.p50, eff.p80, eff.overridden, eff.raw_p50) == (6.0, 6.0, True, 3.5)
    clear_override(db, hw3.id, clock.now())
    eff = effective_estimates(db, clock.now())[hw3.id]
    assert (eff.p50, eff.overridden) == (3.5, False)
    assert [e.source for e in repo.estimates_for(db, hw3.id)] == ["ai", "user"]
    with pytest.raises(ValueError, match="between 0 and 200"):
        set_override(db, hw3, 0, clock.now())


async def test_calibration_applies_to_effective(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    now = clock.now()
    with transaction(db):
        for name, actual in (("Homework 1", 4.0), ("Homework 2", 6.0)):
            repo.insert_estimate(
                db,
                item_id=items[name].id,
                source="ai",
                p50=2.0 if name == "Homework 1" else 3.0,
                p80=4,
                input_hash="x",
                now=now,
            )
            repo.log_time(db, items[name].id, actual, now - timedelta(days=5))
        repo.insert_estimate(
            db,
            item_id=items["Homework 3"].id,
            source="ai",
            p50=3.0,
            p80=4.0,
            input_hash="y",
            now=now,
        )
    eff = effective_estimates(db, now)[items["Homework 3"].id]
    assert eff.factor == pytest.approx(1.4)  # two items, both 2x
    assert (eff.p50, eff.p80) == (pytest.approx(4.2), pytest.approx(5.6))
