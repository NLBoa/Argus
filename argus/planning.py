"""Planning service: turn estimates, time logged, and free time into work sessions and risk.

Deterministic code makes the plan (planner/scheduler.py). The AI only words the one-line
explanation, with a fallback sentence built from the same facts. Replanning happens when the
inputs change or at least hourly; blocks that have already started are never moved, so start
nudges stay stable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.ai import prompts
from argus.ai.client import AIClient, Tier
from argus.ai.schemas import PlanNoteOut
from argus.clock import Clock, local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import ItemKind
from argus.estimates import effective_estimates
from argus.exams import review_concepts
from argus.planner.freetime import Interval, free_hours_between, free_intervals, subtract
from argus.planner.review import ReviewConcept, place_reviews, review_sessions
from argus.planner.risk import RiskLevel, assess
from argus.planner.scheduler import GRID, Block, Plan, PlanItem, schedule
from argus.store import repo
from argus.store.db import iso, parse_iso, transaction

HORIZON = timedelta(days=14)
REPLAN_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class PlanInputs:
    now: datetime
    items: list[PlanItem]
    free: list[Interval]
    remaining_hours: dict[int, float]

    def digest(self) -> str:
        payload = [
            iso(self.now.replace(minute=0, second=0, microsecond=0)),
            [
                (i.item_id, iso(i.due_at) if i.due_at else None, int(i.remaining / GRID))
                for i in self.items
            ],
            [(iso(f.start), iso(f.end)) for f in self.free],
        ]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _grid_start(now: datetime) -> datetime:
    rem = now.minute % 15
    base = now.replace(second=0, microsecond=0)
    return base if rem == 0 and now.second == 0 else base + timedelta(minutes=15 - rem)


def gather(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> PlanInputs:
    start = _grid_start(now)
    eff = effective_estimates(conn, now)
    progress = credited_hours(conn, now)
    items: list[PlanItem] = []
    remaining: dict[int, float] = {}
    for it in repo.list_items(conn, open_only=True):
        if it.id not in eff or not it.kind.takes_submission:
            continue
        if it.due_at is not None and it.due_at <= now:
            continue
        left = max(0.0, eff[it.id].p80 - progress.get(it.id, 0.0))
        remaining[it.id] = left
        items.append(PlanItem(it.id, it.due_at, timedelta(hours=left)))
    s = config.schedule
    free = free_intervals(
        start,
        start + HORIZON,
        waking=s.waking_window,
        quiet=s.quiet_hours,
        buffer_minutes=s.walking_buffer_minutes,
        meetings=repo.list_meetings(conn),
        commitments=s.commitments,
    )
    # Time already promised to sessions that have started stays promised.
    started = [Interval(b.start, b.end) for b in current_blocks(conn, now) if b.start < start]
    return PlanInputs(
        now=start, items=items, free=subtract(free, started), remaining_hours=remaining
    )


def upcoming_reviews(
    conn: sqlite3.Connection, now: datetime
) -> list[tuple[int, datetime, list[ReviewConcept]]]:
    """Exams within the planning horizon with concepts to review: (exam id, exam time, concepts).
    Only sessions that fall inside the horizon are placed (review days are 7..1 days out)."""
    out = []
    for item in repo.list_items(conn, due_after=now, due_before=now + HORIZON, open_only=True):
        if item.kind is not ItemKind.EXAM or item.due_at is None:
            continue
        concepts = review_concepts(conn, item.id)
        if concepts:
            out.append((item.id, item.due_at, concepts))
    return out


def credited_hours(conn: sqlite3.Connection, now: datetime) -> dict[int, float]:
    """Progress per item. If the student logs time for an item (timer, LOG, or the form), the
    logs are the truth. Otherwise planned sessions that have passed count as done: the plan
    assumes it was followed, so the same work is never re-planned (and re-nudged) forever.
    The student corrects with LOG n h or EST n h."""
    logged = repo.logged_hours(conn, now)
    planned: dict[int, float] = {}
    for r in conn.execute(
        "SELECT item_id, start_at, end_at FROM plan_blocks"
        " WHERE kind = 'work' AND item_id IS NOT NULL AND start_at < ?",
        (iso(now),),
    ):
        start, end = parse_iso(r["start_at"]), min(parse_iso(r["end_at"]), now)
        planned[r["item_id"]] = planned.get(r["item_id"], 0.0) + max(
            0.0, (end - start).total_seconds() / 3600
        )
    return {
        i: logged[i] if i in logged else planned.get(i, 0.0) for i in set(logged) | set(planned)
    }


@dataclass(frozen=True)
class StoredBlock:
    id: int
    item_id: int | None
    kind: str
    start: datetime
    end: datetime
    label: str | None = None


def _stored(r: sqlite3.Row) -> StoredBlock:
    return StoredBlock(
        r["id"],
        r["item_id"],
        r["kind"],
        parse_iso(r["start_at"]),
        parse_iso(r["end_at"]),
        r["label"],
    )


def current_blocks(
    conn: sqlite3.Connection, now: datetime, until: datetime | None = None
) -> list[StoredBlock]:
    sql = "SELECT * FROM plan_blocks WHERE end_at > ?"
    params: list[object] = [iso(now)]
    if until is not None:
        sql += " AND start_at < ?"
        params.append(iso(until))
    rows = conn.execute(sql + " ORDER BY start_at", params).fetchall()
    return [_stored(r) for r in rows]


def blocks_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[StoredBlock]:
    rows = conn.execute(
        "SELECT * FROM plan_blocks WHERE end_at > ? AND start_at < ? ORDER BY start_at",
        (iso(start), iso(end)),
    ).fetchall()
    return [_stored(r) for r in rows]


def facts(plan: Plan, inputs: PlanInputs, titles: dict[int, str]) -> list[str]:
    """Plain facts about a plan, most urgent first. Shared by the AI prompt and the fallback."""
    out = []
    for item_id, hours in sorted(plan.unscheduled.items(), key=lambda kv: -kv[1]):
        out.append(
            f"{titles.get(item_id, 'An item')} needs {hours:.1f} more hours than there is free time before it is due"
        )
    week = inputs.now + timedelta(days=7)
    this_week = [b for b in plan.blocks if b.start < week]
    hours = sum((b.end - b.start).total_seconds() for b in this_week) / 3600
    if this_week:
        out.append(f"{len(this_week)} work sessions ({hours:.1f} hours) planned in the next 7 days")
        first = this_week[0]
        out.append(
            f"Next session: {titles.get(first.item_id, 'work')} at {to_local(first.start):%a %I:%M %p}".replace(
                " 0", " "
            )
        )
    else:
        out.append("Nothing needs planned work in the next 7 days")
    return out


def fallback_line(fs: list[str]) -> str:
    return fs[0] + "." if fs else "Nothing to plan."


def replan(
    conn: sqlite3.Connection, config: AppConfig, clock: Clock, *, force: bool = False
) -> bool:
    """Recompute future sessions if inputs changed (or hourly). Returns True if it replanned."""
    now = clock.now()
    inputs = gather(conn, config, now)
    exams = upcoming_reviews(conn, now)
    digest = hashlib.sha256(
        (
            inputs.digest()
            + json.dumps(
                [(e, [(c.id, c.readiness.value, c.priority) for c in cs]) for e, _, cs in exams]
            )
        ).encode()
    ).hexdigest()
    state = conn.execute("SELECT inputs_hash, planned_at FROM plan_state WHERE id = 1").fetchone()
    if (
        not force
        and state is not None
        and state["inputs_hash"] == digest
        and now - parse_iso(state["planned_at"]) < REPLAN_EVERY
    ):
        return False
    plan = schedule(inputs.items, inputs.free)
    titles = {i.id: i.title for i in repo.list_items(conn)}
    line = fallback_line(facts(plan, inputs, titles))
    # Reviews go in the free time the work plan leaves; past and started blocks stay put.
    free = subtract(inputs.free, [Interval(b.start, b.end) for b in plan.blocks])
    reviews: list[tuple[int, Block, str]] = []
    for exam_id, exam_at, concepts in exams:
        sessions = review_sessions(exam_at, inputs.now, concepts)
        for session, block in zip(sessions, place_reviews(sessions, free), strict=False):
            label = "Review: " + ", ".join(c.name for c in session.concepts)
            reviews.append((exam_id, block, label))
            free = subtract(free, [Interval(block.start, block.end)])
    with transaction(conn):
        conn.execute("DELETE FROM plan_blocks WHERE start_at >= ?", (iso(inputs.now),))
        for b in plan.blocks:
            conn.execute(
                "INSERT INTO plan_blocks (item_id, kind, start_at, end_at, created_at) VALUES (?, 'work', ?, ?, ?)",
                (b.item_id, iso(b.start), iso(b.end), iso(now)),
            )
        for exam_id, b, label in reviews:
            conn.execute(
                "INSERT INTO plan_blocks (item_id, kind, start_at, end_at, label, created_at)"
                " VALUES (?, 'review', ?, ?, ?, ?)",
                (exam_id, iso(b.start), iso(b.end), label, iso(now)),
            )
        conn.execute(
            """INSERT INTO plan_state (id, inputs_hash, explanation, planned_at) VALUES (1, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET inputs_hash = excluded.inputs_hash,
                 explanation = excluded.explanation, planned_at = excluded.planned_at""",
            (digest, line, iso(now)),
        )
    return True


async def explain(conn: sqlite3.Connection, ai: AIClient, config: AppConfig, clock: Clock) -> str:
    """Word the plan with the AI (cached by facts). Falls back to the plain first fact."""
    now = clock.now()
    inputs = gather(conn, config, now)
    plan = Plan(
        tuple(
            Block(b.item_id or 0, b.start, b.end)
            for b in current_blocks(conn, now)
            if b.kind == "work"
        ),
        unscheduled_hours(conn, config, now),
    )
    titles = {i.id: i.title for i in repo.list_items(conn)}
    fs = facts(plan, inputs, titles)
    result = await ai.structured(
        task="plan_note",
        tier=Tier.FAST,
        prompt=prompts.load("plan_note").render(facts=fs),
        schema=PlanNoteOut,
        effort="low",
    )
    line = result.value.line if result.value else fallback_line(fs)
    with transaction(conn):
        conn.execute("UPDATE plan_state SET explanation = ? WHERE id = 1", (line,))
    return line


def explanation(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT explanation FROM plan_state WHERE id = 1").fetchone()
    return str(row[0]) if row else None


def unscheduled_hours(
    conn: sqlite3.Connection, config: AppConfig, now: datetime
) -> dict[int, float]:
    inputs = gather(conn, config, now)
    return schedule(inputs.items, inputs.free).unscheduled


@dataclass(frozen=True)
class RiskView:
    level: RiskLevel
    remaining_p80: float | None
    free_before_due: float | None


def risk_map(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> dict[int, RiskView]:
    """The spec rule for every open item: at risk when free hours before due < remaining p80.
    Items the plan cannot fully fit (earlier deadlines take the time) are at least Watch."""
    inputs = gather(conn, config, now)
    squeezed = schedule(inputs.items, inputs.free).unscheduled
    out: dict[int, RiskView] = {}
    for item in repo.list_items(conn):
        remaining = inputs.remaining_hours.get(item.id)
        free = (
            free_hours_between(inputs.free, inputs.now, item.due_at)
            if item.due_at and remaining is not None
            else None
        )
        level = assess(item, now, remaining_p80_hours=remaining, free_hours_before_due=free)
        if item.id in squeezed and level.severity < RiskLevel.WATCH.severity:
            level = RiskLevel.WATCH
        out[item.id] = RiskView(level, remaining, free)
    return out


@dataclass(frozen=True)
class WorkloadDayData:
    day: datetime
    need_hours: float
    free_hours: float


def workload(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, days: int = 14
) -> list[WorkloadDayData]:
    """Per local day: planned work hours (need) against free hours."""
    inputs = gather(conn, config, now)
    today = to_local(now).date()
    out = []
    for d in range(days):
        day = today + timedelta(days=d)
        start, end = local_datetime(day, 0), local_datetime(day + timedelta(days=1), 0)
        need = sum(
            (min(b.end, end) - max(b.start, start)).total_seconds() / 3600
            for b in blocks_between(conn, start, end)
            if b.kind == "work"
        )
        out.append(WorkloadDayData(start, need, free_hours_between(inputs.free, start, end)))
    return out
