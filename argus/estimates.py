"""Effort estimates: AI judges, code decides.

When an open item is new, or its inputs change (title, kind, points, description), Sonnet
estimates p50/p80 hours. Without a usable AI answer, the fallback is the median of this
student's actual hours for the same course and kind, or a per-kind default; both are marked
`default` ("typical" in the UI). Calibration (planner/calibration.py) then scales by how this
student's actuals compare to past estimates. A user override always wins.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

from argus.ai import prompts
from argus.ai.client import AIClient, Tier
from argus.ai.schemas import EstimateOut
from argus.clock import Clock, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind
from argus.planner.calibration import (
    DEFAULT_HOURS,
    DEFAULT_P80_RATIO,
    Effective,
    Finished,
    calibrate,
    factor,
    median_actual,
)
from argus.store import repo
from argus.store.db import transaction

ESTIMATED_KINDS = (ItemKind.ASSIGNMENT, ItemKind.QUIZ, ItemKind.EXAM)
RETRY_DEFAULT_AFTER = timedelta(hours=24)
MAX_INSTRUCTIONS = 8000


def input_hash(item: Item) -> str:
    return hashlib.sha256(
        json.dumps([item.title, item.kind.value, item.points, item.description or ""]).encode()
    ).hexdigest()[:24]


def _history(
    conn: sqlite3.Connection, now: datetime
) -> tuple[list[repo.FinishedRow], list[Finished]]:
    rows = repo.finished_history(conn, now)
    return rows, [Finished(r.course_code, r.kind, r.p50, r.actual) for r in rows]


def fallback(item: Item, history: list[Finished]) -> tuple[float, float, str]:
    med = median_actual(history, item.course_code, item.kind)
    if med is not None:
        return (
            med,
            med * DEFAULT_P80_RATIO,
            f"Median of your past {item.kind.value}s in {item.course_code}.",
        )
    hours = DEFAULT_HOURS[item.kind]
    article = "an" if item.kind.value[0] in "aeiou" else "a"
    return (
        hours,
        hours * DEFAULT_P80_RATIO,
        f"Typical for {article} {item.kind.value}; no history yet.",
    )


async def estimate_item(
    conn: sqlite3.Connection,
    ai: AIClient,
    config: AppConfig,
    clock: Clock,
    item: Item,
    *,
    force_ai: bool = False,
) -> repo.EstimateRow:
    now = clock.now()
    rows, history = _history(conn, now)
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
        instructions=(item.description or "")[:MAX_INSTRUCTIONS] or None,
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
    with transaction(conn):
        if result.value is not None:
            v: EstimateOut = result.value
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
        else:
            p50, p80, why = fallback(item, history)
            repo.insert_estimate(
                conn,
                item_id=item.id,
                source="default",
                p50=p50,
                p80=p80,
                input_hash=h,
                now=now,
                confidence="low",
                reasoning=f"{why} (AI estimate unavailable: {result.status}.)",
            )
    return repo.estimates_for(conn, item.id)[-1]


def needs_estimate(
    item: Item, latest: repo.EstimateRow | None, ai_available: bool, now: datetime
) -> bool:
    if item.kind not in ESTIMATED_KINDS or not item.status.is_open:
        return False
    if item.due_at is not None and item.due_at < now:
        return False
    if latest is None or latest.input_hash != input_hash(item):
        return True
    return (
        latest.source == "default"
        and ai_available
        and now - latest.created_at >= RETRY_DEFAULT_AFTER
    )


async def ensure_estimates(
    conn: sqlite3.Connection, ai: AIClient, config: AppConfig, clock: Clock, *, limit: int = 5
) -> int:
    """Estimate up to `limit` items that need it (soonest due first). Returns how many."""
    now = clock.now()
    latest = repo.latest_estimates(conn)
    todo = [
        i
        for i in repo.list_items(conn, open_only=True)
        if needs_estimate(i, latest.get(i.id, (None, None))[0], ai.available, now)
    ]
    todo.sort(key=lambda i: (i.due_at is None, i.due_at or now, i.id))
    for item in todo[:limit]:
        await estimate_item(conn, ai, config, clock, item)
    return min(len(todo), limit)


def effective_estimates(conn: sqlite3.Connection, now: datetime) -> dict[int, Effective]:
    """Calibrated (or overridden) estimate for every item that has one."""
    _, history = _history(conn, now)
    items = {i.id: i for i in repo.list_items(conn)}
    out: dict[int, Effective] = {}
    for item_id, (base, user) in repo.latest_estimates(conn).items():
        item = items.get(item_id)
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
    """item_id -> where its effective estimate comes from: ai | default | user."""
    out: dict[int, str] = {}
    for item_id, (base, user) in repo.latest_estimates(conn).items():
        if user is not None:
            out[item_id] = "user"
        elif base is not None:
            out[item_id] = base.source
    return out
