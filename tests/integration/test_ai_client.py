import sqlite3

import pytest

from argus.ai import prompts
from argus.ai.budget import budget_hit_this_month, spent_this_month
from argus.ai.client import AIClient, Tier
from argus.ai.schemas import PlanNoteOut
from argus.clock import FakeClock
from argus.config import AIConfig
from argus.store.db import iso
from tests.ai_fakes import FakeTransport, error, ok, refused

PROMPT = prompts.load("plan_note").render(facts=["2 sessions planned"])


def client(
    db: sqlite3.Connection, clock: FakeClock, transport: FakeTransport | None, budget: float = 10.0
) -> AIClient:
    return AIClient(db, AIConfig(monthly_budget_usd=budget), clock, transport)


def calls(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (r["status"], r["model"])
        for r in db.execute("SELECT status, model FROM ai_calls ORDER BY id")
    ]


async def test_ok_records_cost_and_caches(db: sqlite3.Connection, clock: FakeClock) -> None:
    t = FakeTransport([ok({"line": "Two sessions before Thursday."})])
    c = client(db, clock, t)
    r = await c.structured(task="plan_note", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut)
    assert r.ok and r.value is not None and r.value.line == "Two sessions before Thursday."
    assert r.status == "ok" and r.model == "claude-haiku-5-5" and r.prompt_id == "plan_note.v1"
    sent = t.calls[0]
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["schema"]["additionalProperties"] is False
    assert sent["system"] == PROMPT.system
    assert spent_this_month(db, clock.now()) == pytest.approx((1200 * 0.10 + 300 * 0.50) / 1e6)
    again = await c.structured(task="plan_note", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut)
    assert again.status == "cache_hit" and again.value == r.value
    assert len(t.calls) == 1
    assert calls(db) == [("ok", "claude-haiku-5-5"), ("cache_hit", "claude-haiku-5-5")]


async def test_tiers_use_configured_models(db: sqlite3.Connection, clock: FakeClock) -> None:
    c = client(db, clock, FakeTransport())
    assert [c.model_for(t) for t in Tier] == [
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
    ]


async def test_invalid_then_valid_retries_once(db: sqlite3.Connection, clock: FakeClock) -> None:
    t = FakeTransport([ok("not json"), ok({"line": "fine"})])
    r = await client(db, clock, t).structured(
        task="plan_note", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut
    )
    assert r.status == "ok"
    assert [s for s, _ in calls(db)] == ["invalid", "ok"]


async def test_invalid_twice_falls_back(db: sqlite3.Connection, clock: FakeClock) -> None:
    t = FakeTransport([ok({"line": ""}), ok({"wrong": 1})])
    r = await client(db, clock, t).structured(
        task="plan_note", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut
    )
    assert r.value is None and r.status == "invalid"
    assert len(t.calls) == 2


async def test_truncated_output_is_invalid(db: sqlite3.Connection, clock: FakeClock) -> None:
    from argus.ai.client import TransportResponse, Usage

    cut = TransportResponse('{"line": "abc', "max_tokens", Usage(10, 4096), "claude-haiku-5-5")
    r = await client(db, clock, FakeTransport([cut, cut])).structured(
        task="t", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut
    )
    assert r.status == "invalid"


async def test_refusal_does_not_retry(db: sqlite3.Connection, clock: FakeClock) -> None:
    t = FakeTransport([refused()])
    r = await client(db, clock, t).structured(
        task="plan_note", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut
    )
    assert r.status == "refused" and len(t.calls) == 1


async def test_transport_error(db: sqlite3.Connection, clock: FakeClock) -> None:
    r = await client(db, clock, FakeTransport([error()])).structured(
        task="t", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut
    )
    assert r.status == "error"
    assert db.execute("SELECT error FROM ai_calls").fetchone()[0] == "APIConnectionError: boom"


async def test_unavailable_without_transport(db: sqlite3.Connection, clock: FakeClock) -> None:
    c = client(db, clock, None)
    assert not c.available
    r = await c.structured(task="t", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut)
    assert r.status == "unavailable" and calls(db) == []


async def test_budget_cap_stops_calls(db: sqlite3.Connection, clock: FakeClock) -> None:
    db.execute(
        "INSERT INTO ai_calls (task, model, prompt_version, input_hash, status, cost_usd, created_at)"
        " VALUES ('estimate', 'claude-sonnet-5-5', 'estimate.v1', 'h', 'ok', 10.0, ?)",
        (iso(clock.now()),),
    )
    t = FakeTransport([ok({"line": "never"})])
    c = client(db, clock, t, budget=10.0)
    assert not budget_hit_this_month(db, clock.now())
    r = await c.structured(task="plan_note", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut)
    assert r.status == "budget" and t.calls == []
    assert budget_hit_this_month(db, clock.now())


async def test_cache_survives_budget_cap(db: sqlite3.Connection, clock: FakeClock) -> None:
    await client(db, clock, FakeTransport([ok({"line": "cached"})])).structured(
        task="t", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut
    )
    capped = client(db, clock, FakeTransport(), budget=0.0)
    r = await capped.structured(task="t", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut)
    assert r.status == "cache_hit"


async def test_stale_cache_entry_is_ignored(db: sqlite3.Connection, clock: FakeClock) -> None:
    t = FakeTransport([ok({"line": "first"})])
    c = client(db, clock, t)
    await c.structured(task="t", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut)
    db.execute("UPDATE ai_cache SET response = '{\"bogus\": true}'")
    t.responses.append(ok({"line": "second"}))
    r = await c.structured(task="t", tier=Tier.FAST, prompt=PROMPT, schema=PlanNoteOut)
    assert r.value is not None and r.value.line == "second"
