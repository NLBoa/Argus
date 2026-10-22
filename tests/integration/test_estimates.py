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
    estimate_sources,
    estimate_state,
    input_hash,
    needs_estimate,
    set_override,
)
from argus.store import repo
from argus.store.db import transaction
from tests.ai_fakes import FakeTransport, ok, recorded
from tests.caps import EVERY_SOURCE

HW3_DESCRIPTION = (
    "Prove the three lemmas from lecture 9 about the running time of merge sort. Part A: show "
    "the recurrence holds for powers of two. Part B: extend it to every n. Part C: bound the "
    "number of comparisons. Typeset your answers in LaTeX and submit one PDF on Gradescope."
)


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
                    description=HW3_DESCRIPTION,
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
        repo.refresh_statuses(db, now, tracking_sources=EVERY_SOURCE)
    return {i.title: i for i in repo.list_items(db)}


def ai(db: sqlite3.Connection, clock: FakeClock, transport: FakeTransport | None) -> AIClient:
    return AIClient(db, AIConfig(), clock, transport)


async def test_ai_estimate_is_stored_with_provenance(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    t = FakeTransport([ok(recorded("estimate_hw3"), model="claude-sonnet-5-5")])
    row = await estimate_item(db, ai(db, clock, t), app_config, clock, items["Homework 3"])
    assert row is not None
    assert (row.source, row.p50, row.p80, row.version) == ("ai", 3.5, 5.0, 1)
    assert row.model == "claude-sonnet-5-5" and row.prompt_version == "estimate.v2"
    assert row.breakdown[1] == ("Write up the three proofs", 2.5)
    assert t.calls[0]["model"] == "claude-sonnet-5-5"
    assert "Part B: extend it to every n." in t.calls[0]["user"]
    assert "never drafts answers" in t.calls[0]["system"]


async def test_no_guess_without_a_full_description(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    t = FakeTransport(responder=lambda req: ok(recorded("estimate_hw3")))
    quiz = items["Quiz 2"]  # no description at all
    assert await estimate_item(db, ai(db, clock, t), app_config, clock, quiz) is None
    pointer = replace(items["Homework 3"], description="See the attached PDF.")
    assert await estimate_item(db, ai(db, clock, t), app_config, clock, pointer) is None
    assert t.calls == [] and repo.latest_estimates(db) == {}
    assert estimate_state(quiz, None, None, None, ai=True) == "no_description"
    assert estimate_state(quiz, None, None, None, ai=False) == "ask"


async def test_ai_finding_the_description_incomplete_is_remembered(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    hw3 = items["Homework 3"]
    declined = {
        **recorded("estimate_hw3"),
        "whole_task": False,
        "missing": "The problems are in a PDF.",
    }
    t = FakeTransport([ok(declined)])
    assert await estimate_item(db, ai(db, clock, t), app_config, clock, hw3) is None
    assert repo.estimates_for(db, hw3.id) == []
    miss = repo.estimate_misses(db)[hw3.id]
    assert (miss.outcome, miss.reason) == ("incomplete", "The problems are in a PDF.")
    assert estimate_state(hw3, None, None, miss, ai=True) == "incomplete"
    later = clock.now() + timedelta(days=30)
    assert not needs_estimate(hw3, None, miss, overridden=False, ai_available=True, now=later)
    # A new description is a new input: ask again.
    changed = replace(hw3, description=HW3_DESCRIPTION + " Part D: a lower bound.")
    assert needs_estimate(changed, None, miss, overridden=False, ai_available=True, now=clock.now())


async def test_a_failed_call_is_retried_a_day_later(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    hw3 = items["Homework 3"]
    bad = {**recorded("estimate_hw3"), "p80_hours": 1.0}  # p80 < p50 fails validation
    t = FakeTransport([ok(bad), ok(bad)])
    assert await estimate_item(db, ai(db, clock, t), app_config, clock, hw3) is None
    assert repo.estimates_for(db, hw3.id) == []
    miss = repo.estimate_misses(db)[hw3.id]
    assert (miss.outcome, miss.reason) == ("failed", "invalid")
    assert estimate_state(hw3, None, None, miss, ai=True) == "failed"
    soon, later = clock.now() + timedelta(hours=1), clock.now() + timedelta(hours=25)
    assert not needs_estimate(hw3, None, miss, overridden=False, ai_available=True, now=soon)
    assert needs_estimate(hw3, None, miss, overridden=False, ai_available=True, now=later)
    assert not needs_estimate(hw3, None, miss, overridden=False, ai_available=False, now=later)
    # A later success clears the miss.
    t2 = FakeTransport([ok(recorded("estimate_hw3"))])
    assert await estimate_item(db, ai(db, clock, t2), app_config, clock, hw3, force_ai=True)
    assert hw3.id not in repo.estimate_misses(db)


async def test_ensure_estimates_runs_once_per_input(
    db: sqlite3.Connection, items: dict[str, Any], app_config: AppConfig, clock: FakeClock
) -> None:
    c = ai(db, clock, FakeTransport(responder=lambda req: ok(recorded("estimate_hw3"))))
    # Only Homework 3: Quiz 2 has no description; events and finished work are never estimated.
    assert await ensure_estimates(db, c, app_config, clock) == 1
    assert await ensure_estimates(db, c, app_config, clock) == 0
    latest = repo.latest_estimates(db)
    hw3 = items["Homework 3"]
    base = latest[hw3.id][0]
    assert base is not None
    changed = replace(hw3, description=HW3_DESCRIPTION + " Part D: a lower bound.")
    assert needs_estimate(changed, base, None, overridden=False, ai_available=True, now=clock.now())
    assert input_hash(changed) != input_hash(hw3)
    # The student's own time ends the asking.
    assert not needs_estimate(
        changed, base, None, overridden=True, ai_available=True, now=clock.now()
    )
    assert not needs_estimate(
        items["Office hours"], None, None, overridden=False, ai_available=True, now=clock.now()
    )


@pytest.mark.parametrize(("source", "prompt"), [("default", None), ("ai", "estimate.v1")])
def test_old_guesses_no_longer_count(
    items: dict[str, Any],
    clock: FakeClock,
    db: sqlite3.Connection,
    source: str,
    prompt: str | None,
) -> None:
    """Per-kind defaults and title-only AI guesses (estimate.v1) are ignored, and the item is
    asked about again under the current prompt when its description is readable (D069)."""
    hw3, quiz = items["Homework 3"], items["Quiz 2"]
    with transaction(db):
        for it in (hw3, quiz):
            repo.insert_estimate(
                db,
                item_id=it.id,
                source=source,
                p50=3,
                p80=4.5,
                input_hash=input_hash(it),
                now=clock.now(),
                prompt_version=prompt,
            )
    assert effective_estimates(db, clock.now()) == {}
    assert estimate_sources(db) == {}
    old = repo.estimates_for(db, hw3.id)[-1]
    assert estimate_state(hw3, old, None, None, ai=True) == "pending"
    assert needs_estimate(hw3, old, None, overridden=False, ai_available=True, now=clock.now())
    assert not needs_estimate(quiz, old, None, overridden=False, ai_available=True, now=clock.now())


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
    user = repo.latest_estimates(db)[hw3.id][1]
    assert estimate_state(hw3, None, user, None, ai=True) == "user"
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
