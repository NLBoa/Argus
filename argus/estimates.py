"""Effort estimates: AI judges, code decides, and nobody guesses (D069).

The AI estimates p50/p80 hours only for an open item whose description it can read in full:
the description must be long enough to describe a task, and the AI must agree that it describes
the whole task (not "see the attached PDF"). Otherwise there is no estimate and the student sets
the time (Today, Upcoming, the item page, or `EST n h`). An AI miss is remembered per input, so
the same description isn't sent again; a failed call is retried a day later. Calibration
(planner/calibration.py) scales AI estimates by how this student's actuals compare to past
ones. A user override always wins.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

from argus.ai import prompts
from argus.ai.client import AIClient, Tier
from argus.ai.schemas import EstimateOut
from argus.clock import Clock, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind
from argus.planner.calibration import Effective, Finished, calibrate, factor
from argus.store import repo
from argus.store.db import transaction

ESTIMATED_KINDS = (ItemKind.ASSIGNMENT, ItemKind.QUIZ, ItemKind.EXAM)
RETRY_FAILED_AFTER = timedelta(hours=24)
MAX_INSTRUCTIONS = 8000
# Characters of description (whitespace collapsed) below which it can't describe a whole task.
MIN_DESCRIPTION = 200
# AI estimates from these prompts guessed from the title alone; they no longer count.
RETIRED_PROMPTS = frozenset({"estimate.v1"})


def input_hash(item: Item) -> str:
    return hashlib.sha256(
        json.dumps([item.title, item.kind.value, item.points, item.description or ""]).encode()
    ).hexdigest()[:24]


def readable(item: Item) -> bool:
    """The description is long enough to be worth asking the AI about."""
    return len(" ".join((item.description or "").split())) >= MIN_DESCRIPTION


def counts(row: repo.EstimateRow | None) -> bool:
    """An estimate Argus uses: the student's, or an AI estimate from the current rules (not a
    per-kind default or a title-only guess from a retired prompt)."""
    return row is not None and (
        row.source == "user" or (row.source == "ai" and row.prompt_version not in RETIRED_PROMPTS)
    )


def finished_rows(conn: sqlite3.Connection, now: datetime) -> list[repo.FinishedRow]:
    """Finished work with logged time. Its estimate is kept only if it counts, so calibration
    learns from the estimates Argus makes now, not from defaults or title-only guesses."""
    latest = repo.latest_estimates(conn)
    return [
        r if counts(latest.get(r.item_id, (None, None))[0]) else replace(r, p50=0.0)
        for r in repo.finished_history(conn, now)
    ]


def _history(
    conn: sqlite3.Connection, now: datetime
) -> tuple[list[repo.FinishedRow], list[Finished]]:
    rows = finished_rows(conn, now)
    return rows, [Finished(r.course_code, r.kind, r.p50, r.actual) for r in rows]


async def estimate_item(
    conn: sqlite3.Connection,
    ai: AIClient,
    config: AppConfig,
    clock: Clock,
    item: Item,
    *,
    force_ai: bool = False,
) -> repo.EstimateRow | None:
    """The new AI estimate, or None: no full description to read, the AI found it incomplete,
    or the call failed. None is recorded as a miss; there is no fallback guess."""
    if not readable(item):
        return None
    now = clock.now()
    rows, _ = _history(conn, now)
    course = config.course(item.course_code)
    past = [
        {
            "title": r.title,
            "kind": r.kind.value,
            "actual": round(r.actual, 1),
            "estimate": round(r.p50, 1) if r.p50 else None,
        }
        for r in rows
        if r.course_code == item.course_code
    ][-8:]
    rendered = prompts.load("estimate").render(
        course=item.course_code,
        difficulty_note=course.difficulty_note if course else None,
        title=item.title,
        kind=item.kind.value,
        points=item.points,
        question_count=None,
        due=f"{to_local(item.due_at):%a %b %d}" if item.due_at else None,
        past=past,
        instructions=(item.description or "")[:MAX_INSTRUCTIONS],
    )
    result = await ai.structured(
        task="estimate",
        tier=Tier.STANDARD,
        prompt=rendered,
        schema=EstimateOut,
        effort="medium",
        use_cache=not force_ai,
    )
    h = input_hash(item)
    v = result.value
    with transaction(conn):
        if v is None or not v.whole_task:
            repo.record_estimate_miss(
                conn,
                item_id=item.id,
                input_hash=h,
                outcome="failed" if v is None else "incomplete",
                reason=result.status
                if v is None
                else v.missing.strip() or "The description doesn't cover the whole task.",
                prompt_version=result.prompt_id,
                now=now,
            )
            return None
        repo.clear_estimate_miss(conn, item.id)
        repo.insert_estimate(
            conn,
            item_id=item.id,
            source="ai",
            p50=v.p50_hours,
            p80=v.p80_hours,
            input_hash=h,
            now=now,
            confidence=v.confidence,
            breakdown=[(s.step, s.hours) for s in v.breakdown],
            assumptions=v.assumptions,
            reasoning=v.reasoning,
            model=result.model,
            prompt_version=result.prompt_id,
        )
    return repo.estimates_for(conn, item.id)[-1]


def needs_estimate(
    item: Item,
    base: repo.EstimateRow | None,
    miss: repo.EstimateMiss | None,
    *,
    overridden: bool,
    ai_available: bool,
    now: datetime,
) -> bool:
    """Ask the AI about this item now? Never when the student set the time, the description
    can't describe the task, or the AI already answered (or declined) this exact input."""
    if not ai_available or overridden or item.kind not in ESTIMATED_KINDS:
        return False
    if not item.status.is_open or (item.due_at is not None and item.due_at < now):
        return False
    if not readable(item):
        return False
    h = input_hash(item)
    if counts(base) and base is not None and base.input_hash == h:
        return False
    if miss is not None and miss.input_hash == h:
        return miss.outcome == "failed" and now - miss.created_at >= RETRY_FAILED_AFTER
    return True


async def ensure_estimates(
    conn: sqlite3.Connection, ai: AIClient, config: AppConfig, clock: Clock, *, limit: int = 5
) -> int:
    """Estimate up to `limit` items that need it (soonest due first). Returns how many."""
    now = clock.now()
    latest = repo.latest_estimates(conn)
    misses = repo.estimate_misses(conn)

    def due(i: Item) -> bool:
        base, user = latest.get(i.id, (None, None))
        return needs_estimate(
            i,
            base,
            misses.get(i.id),
            overridden=user is not None,
            ai_available=ai.available,
            now=now,
        )

    todo = [i for i in repo.list_items(conn, open_only=True) if due(i)]
    todo.sort(key=lambda i: (i.due_at is None, i.due_at or now, i.id))
    for item in todo[:limit]:
        await estimate_item(conn, ai, config, clock, item)
    return min(len(todo), limit)


def estimate_state(
    item: Item,
    base: repo.EstimateRow | None,
    user: repo.EstimateRow | None,
    miss: repo.EstimateMiss | None,
    *,
    ai: bool,
) -> str:
    """Why an item has the estimate it has, for the item page: user | ai | ask (no AI set up)
    | no_description | incomplete | failed | pending (the AI will read it shortly)."""
    if user is not None:
        return "user"
    if counts(base):
        return "ai"
    if not ai or item.kind not in ESTIMATED_KINDS:
        return "ask"
    if not readable(item):
        return "no_description"
    if miss is not None and miss.input_hash == input_hash(item):
        return miss.outcome
    return "pending"


def effective_estimates(conn: sqlite3.Connection, now: datetime) -> dict[int, Effective]:
    """Calibrated (or overridden) estimate for every item that has one."""
    _, history = _history(conn, now)
    items = {i.id: i for i in repo.list_items(conn)}
    out: dict[int, Effective] = {}
    for item_id, (latest, user) in repo.latest_estimates(conn).items():
        item = items.get(item_id)
        base = latest if counts(latest) else None
        if item is None or (base is None and user is None):
            continue
        f = factor(history, item.course_code, item.kind)
        raw50 = base.p50 if base else (user.p50 if user else 0.0)
        raw80 = base.p80 if base else raw50
        out[item_id] = calibrate(
            raw_p50=raw50, raw_p80=raw80, f=f, override=user.p50 if user else None
        )
    return out


def set_override(conn: sqlite3.Connection, item: Item, hours: float, now: datetime) -> None:
    if not 0 < hours <= 200:
        raise ValueError("hours must be between 0 and 200")
    with transaction(conn):
        repo.insert_estimate(
            conn,
            item_id=item.id,
            source="user",
            p50=hours,
            p80=hours,
            input_hash=input_hash(item),
            now=now,
            reasoning="Set by you.",
        )


def clear_override(conn: sqlite3.Connection, item_id: int, now: datetime) -> None:
    with transaction(conn):
        repo.retract_user_estimates(conn, item_id, now)


def estimate_sources(conn: sqlite3.Connection) -> dict[int, str]:
    """item_id -> where its effective estimate comes from: ai | user."""
    out: dict[int, str] = {}
    for item_id, (base, user) in repo.latest_estimates(conn).items():
        if user is not None:
            out[item_id] = "user"
        elif counts(base):
            out[item_id] = "ai"
    return out
