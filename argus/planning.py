"""Planning service: work windows, the queue, and risk (D057-D062).

Deterministic code decides everything here; the AI only words the one-line explanation, with a
fallback sentence built from the same facts. `compute` is a read of the store: per item its
remaining hours (E), load (C and D) and risk level; the ranked queue; exam study time; and how
each upcoming window's time would be split. `replan` is the housekeeping run every worker tick
and after the student changes something: seed the work windows once, convert legacy planned
sessions once, and keep the current (or about to start) window's split up to date until it ends.

Progress is only what the student logged (timer, LOG, the form, or an answer to "did you work
on it?"). Nothing is assumed (D060); a running timer counts as it runs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from argus.ai import prompts
from argus.ai.client import AIClient, Tier
from argus.ai.schemas import PlanNoteOut
from argus.clock import Clock, local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind
from argus.domain.status import awaiting_confirmation, effective_deadline
from argus.estimates import effective_estimates
from argus.planner.calibration import DEFAULT_HOURS, DEFAULT_P80_RATIO, Effective
from argus.planner.exam_time import ExamStudy, StudyPlan, study_plan
from argus.planner.queue import Entry, forecast, order
from argus.planner.queue import Tier as QueueTier
from argus.planner.risk import Load, RiskLevel, Work, assess, loads
from argus.planner.windows import (
    WindowSpan,
    WorkWindow,
    current_or_next,
    from_config,
    window_spans,
)
from argus.store import repo
from argus.store.db import iso, parse_iso, transaction

HORIZON = timedelta(days=120)  # window time is counted up to the latest due date, within this
TONIGHT_WITHIN = timedelta(hours=24)  # the queue's window: the current one or the next in 24 h
MIN_LEFT = 0.25  # the queue shows at least 15 minutes left on an open item (display only)
REPLAN_EVERY = timedelta(hours=1)
LOG_AFTER = timedelta(hours=24)  # a LOG this soon after a window answers its question
_TIERS = {
    RiskLevel.OVERDUE: QueueTier.OVERDUE,
    RiskLevel.AT_RISK: QueueTier.AT_RISK,
    RiskLevel.CREEPING: QueueTier.CREEPING,
}


@dataclass(frozen=True)
class ItemPlan:
    item: Item
    tracked: bool
    remaining: float | None  # E, hours; None without an estimate
    load: Load | None  # C and D; None without windows, an estimate, or a due date
    level: RiskLevel


@dataclass(frozen=True)
class Planning:
    now: datetime
    windows_on: bool
    spans: list[WindowSpan]
    items: dict[int, ItemPlan]
    study: StudyPlan
    queue: list[Entry]  # ranked
    window: WindowSpan | None  # the current window, or the next one within 24 h ("tonight")
    forecast: dict[str, list[tuple[str, float]]] = field(default_factory=dict)

    def allocation(self, window_id: str) -> list[tuple[str, float]]:
        return self.forecast.get(window_id, [])


def _overlap(start: datetime, end: datetime, a: datetime, b: datetime) -> float:
    lo, hi = max(start, a), min(end, b)
    return max(0.0, (hi - lo).total_seconds() / 3600)


def windows_from_config(config: AppConfig) -> list[WorkWindow]:
    return from_config(config.schedule.work_windows)


def _remaining(
    items: Sequence[Item],
    eff: dict[int, Effective],
    logs: Sequence[repo.TimeLog],
    marks: dict[int, tuple[float, datetime]],
    now: datetime,
) -> dict[int, float | None]:
    """E per open item that takes a submission: the student's latest "about N h left" minus
    what they logged since, else the calibrated p80 minus everything logged; None without
    an estimate."""
    progress: dict[int, float] = {}
    for lg in logs:
        progress[lg.item_id] = progress.get(lg.item_id, 0.0) + lg.hours(now)
    out: dict[int, float | None] = {}
    for it in items:
        if not (it.status.is_open and it.kind.takes_submission):
            continue
        if it.id in marks:
            hours, at = marks[it.id]
            since = sum(
                _overlap(lg.started_at, lg.ended_at or now, at, now)
                for lg in logs
                if lg.item_id == it.id
            )
            out[it.id] = max(0.0, hours - since)
        elif it.id in eff:
            out[it.id] = max(0.0, eff[it.id].p80 - progress.get(it.id, 0.0))
        else:
            out[it.id] = None
    return out


def _study_cards(items: Sequence[Item], study: StudyPlan, window: WindowSpan) -> list[Entry]:
    """One "Study for <exam>" card per exam with time reserved in this window (D062)."""
    by_id = {i.id: i for i in items}
    return [
        Entry(
            f"study:{exam_id}",
            exam_id,
            "study",
            by_id[exam_id].due_at,
            QueueTier.STUDY,
            None,
            hours,
        )
        for exam_id, hours in sorted(study.quota.get(window.window_id, {}).items())
    ]


def compute(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, *, tracking_sources: frozenset[str]
) -> Planning:
    windows = repo.work_windows(conn)
    items = repo.list_items(conn)
    eff = effective_estimates(conn, now)
    logs = repo.all_time_logs(conn)
    marks = repo.latest_remaining_marks(conn)

    remaining = _remaining(items, eff, logs, marks, now)

    latest_due = max((i.due_at for i in items if i.due_at and i.due_at > now), default=now)
    s = config.schedule
    spans = window_spans(
        now - timedelta(days=1),
        min(max(latest_due + timedelta(days=1), now + timedelta(days=15)), now + HORIZON),
        windows,
        meetings=repo.list_meetings(conn),
        commitments=s.commitments,
        buffer_minutes=s.walking_buffer_minutes,
    )
    windows_on = bool(windows)

    exams = [
        ExamStudy(
            i.id,
            i.due_at,
            p80=eff[i.id].p80 if i.id in eff else DEFAULT_HOURS[ItemKind.EXAM] * DEFAULT_P80_RATIO,
            logged=sum(lg.hours(now) for lg in logs if lg.item_id == i.id),
        )
        for i in items
        if i.kind is ItemKind.EXAM and i.status.is_open and i.due_at is not None and i.due_at > now
    ]
    study = study_plan(exams, spans, now) if windows_on else StudyPlan()
    window = current_or_next(spans, now, within=TONIGHT_WITHIN)
    prefs = repo.queue_prefs(conn)
    skips = {
        int(entry.split(":", 1)[1]): p.skip_window
        for entry, p in prefs.items()
        if entry.startswith("item:") and p.skip_window
    }

    tracked = {i.id: i.tracked_by(tracking_sources) for i in items}
    work = []
    for it in items:
        e = remaining.get(it.id)
        if e is None or it.due_at is None:
            continue
        deadline = effective_deadline(it.due_at, it.late_due_at)
        counts = not awaiting_confirmation(it, now, tracked=tracked[it.id]) and (
            deadline is None or deadline > now
        )
        work.append(Work(it.id, it.due_at, e, counts))
    load = loads(work, now, spans, reserved=study.reserved(), skips=skips) if windows_on else {}

    plans: dict[int, ItemPlan] = {}
    entries: list[Entry] = []
    for it in items:
        lo = load.get(it.id)
        level = assess(it, now, tracked=tracked[it.id], load=lo)
        plans[it.id] = ItemPlan(it, tracked[it.id], remaining.get(it.id), lo, level)
        if not (it.status.is_open and it.kind.takes_submission) or level is RiskLevel.UNCONFIRMED:
            continue
        deadline = effective_deadline(it.due_at, it.late_due_at)
        if deadline is not None and deadline <= now:
            continue  # too late to do anything about it
        entries.append(
            Entry(
                f"item:{it.id}",
                it.id,
                "work",
                it.due_at,
                _TIERS.get(level, QueueTier.OK),
                lo.slack if lo is not None else None,
                remaining.get(it.id) or 0.0,  # unfloored: nothing left gets no window time
            )
        )
    if window is not None:
        entries += _study_cards(items, study, window)
    ranked = order(entries, prefs, now, window.window_id if window else None)
    ahead = (
        forecast(
            ranked,
            spans,
            now,
            reserved=study.quota,
            skips={f"item:{i}": w for i, w in skips.items()},
        )
        if windows_on
        else {}
    )
    return Planning(now, windows_on, spans, plans, study, ranked, window, ahead)


# ---------------------------------------------------------------- facts and the explanation


def facts(p: Planning) -> list[str]:
    """Plain facts about the plan, most urgent first. Shared by the AI prompt and the fallback."""
    if not p.windows_on:
        return ["No work windows are set, so Argus can't tell how much time you have"]
    out = []
    risky = sorted(
        (ip for ip in p.items.values() if ip.level is RiskLevel.AT_RISK and ip.load is not None),
        key=lambda ip: (ip.load.slack if ip.load else 0.0, ip.item.id),
    )
    for ip in risky:
        assert ip.load is not None
        out.append(
            f"{ip.item.title} needs {ip.load.demand - ip.load.capacity:.1f} more hours than "
            "your work windows have before it is due"
        )
    titles = {i: ip.item.title for i, ip in p.items.items()}
    for exam_id, hours in sorted(p.study.shortfall.items()):
        out.append(f"Study time for {titles.get(exam_id, 'an exam')} is {hours:.1f} hours short")
    week = p.now + timedelta(days=7)
    window_hours = sum(sp.hours_between(p.now, week) for sp in p.spans)
    work_hours = sum(
        ip.remaining or 0.0
        for ip in p.items.values()
        if ip.item.due_at is not None and p.now < ip.item.due_at <= week and ip.load is not None
    )
    out.append(
        f"{work_hours:.1f} hours of work due in the next 7 days, "
        f"{window_hours:.1f} hours of work windows"
    )
    if p.window is not None:
        first = next(iter(p.allocation(p.window.window_id)), None)
        what = titles.get(int(first[0].split(":", 1)[1]), "work") if first else "nothing yet"
        start = to_local(p.window.start)
        out.append(f"Next work window {start:%a} {start:%I:%M %p}".replace(" 0", " ") + f": {what}")
    return out


def fallback_line(fs: list[str]) -> str:
    return fs[0] + "." if fs else "Nothing to plan."


def replan(
    conn: sqlite3.Connection,
    config: AppConfig,
    clock: Clock,
    *,
    tracking_sources: frozenset[str],
    force: bool = False,
) -> bool:
    """Housekeeping: seed work windows once, convert legacy sessions once, keep this window's
    split current until it ends. Returns True when the facts changed (or an hour passed), so the
    explanation can be reworded."""
    now = clock.now()
    with transaction(conn):
        repo.seed_work_windows(conn, windows_from_config(config), now)
        repo.convert_legacy_blocks(conn, now)
    p = compute(conn, config, now, tracking_sources=tracking_sources)
    lead = timedelta(minutes=config.notifications.window_notice_minutes)
    fs = facts(p)
    digest = hashlib.sha256(json.dumps(fs).encode()).hexdigest()
    with transaction(conn):
        # The split is made before the window starts and frozen when it starts, so it records
        # what the whole window was for. A worker that was down at the start stores what is left.
        w = p.window
        if w is not None and (
            w.start - lead <= now < w.start
            or (w.start <= now < w.end and repo.window_plan(conn, w.window_id) is None)
        ):
            repo.save_window_plan(
                conn,
                w.window_id,
                starts_at=w.start,
                ends_at=w.end,
                allocation=p.allocation(w.window_id),
                now=now,
            )
        state = conn.execute(
            "SELECT inputs_hash, planned_at FROM plan_state WHERE id = 1"
        ).fetchone()
        if (
            not force
            and state is not None
            and state["inputs_hash"] == digest
            and now - parse_iso(state["planned_at"]) < REPLAN_EVERY
        ):
            return False
        conn.execute(
            """UPDATE plan_state SET inputs_hash = ?, explanation = ?, planned_at = ?,
                 model_since = COALESCE(model_since, ?) WHERE id = 1""",
            (digest, fallback_line(fs), iso(now), iso(now)),
        )
    return True


async def explain(
    conn: sqlite3.Connection,
    ai: AIClient,
    config: AppConfig,
    clock: Clock,
    *,
    tracking_sources: frozenset[str],
) -> str:
    """Word the plan with the AI (cached by facts). Falls back to the plain first fact."""
    fs = facts(compute(conn, config, clock.now(), tracking_sources=tracking_sources))
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
    return str(row[0]) if row and row[0] else None


# ---------------------------------------------------------------- views of the plan


@dataclass(frozen=True)
class RiskView:
    level: RiskLevel
    remaining: float | None
    capacity: float | None  # C: work-window hours before it's due
    demand: float | None  # D: hours of work due by then, this item included


def risk_views(p: Planning) -> dict[int, RiskView]:
    return {
        i: RiskView(
            ip.level,
            ip.remaining,
            ip.load.capacity if ip.load else None,
            ip.load.demand if ip.load else None,
        )
        for i, ip in p.items.items()
    }


def risk_map(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, *, tracking_sources: frozenset[str]
) -> dict[int, RiskView]:
    return risk_views(compute(conn, config, now, tracking_sources=tracking_sources))


@dataclass(frozen=True)
class WorkloadDayData:
    day: datetime
    need_hours: float  # work and study forecast into that day's windows
    free_hours: float  # work-window hours left that day


def workload(p: Planning, days: int = 14) -> list[WorkloadDayData]:
    """Per local day: work-window hours and the work forecast into them."""
    today = to_local(p.now).date()
    out = []
    for n in range(days):
        start = local_datetime(today + timedelta(days=n), 0)
        end = local_datetime(today + timedelta(days=n + 1), 0)
        on_day = [sp for sp in p.spans if start <= sp.start < end]
        out.append(
            WorkloadDayData(
                start,
                sum(h for sp in on_day for _, h in p.allocation(sp.window_id)),
                sum(sp.hours_between(max(p.now, start), sp.end) for sp in on_day),
            )
        )
    return out


@dataclass(frozen=True)
class Confirmation:
    window_id: str
    start: datetime
    end: datetime
    item_ids: tuple[int, ...]


def pending_confirmation(conn: sqlite3.Connection, p: Planning) -> Confirmation | None:
    """After a window ends, the items it was planned for with no time logged in it (and no LOG
    within a day after) get one quiet question on Today: "did you work on it?" (D060). No answer
    means no credit; an answer is recorded once. The question goes when the next window ends."""
    plan = repo.latest_ended_window_plan(conn, p.now)
    if plan is None:
        return None
    later = [sp for sp in p.spans if sp.start >= plan.ends_at]
    if later and later[0].end <= p.now:
        return None
    logs = repo.all_time_logs(conn)
    ids: list[int] = []
    for entry, _hours in plan.allocation:
        if not entry.startswith("item:"):
            continue
        item_id = int(entry.split(":", 1)[1])
        ip = p.items.get(item_id)
        if ip is None or not ip.item.status.is_open:
            continue
        mine = [lg for lg in logs if lg.item_id == item_id]
        answered = any(lg.source == "checkin" and lg.window_id == plan.window_id for lg in mine)
        worked = any(
            lg.source != "checkin"
            and _overlap(lg.started_at, lg.ended_at or p.now, plan.starts_at, plan.ends_at) > 0
            for lg in mine
        )
        logged_after = any(
            lg.source in ("sms", "manual")
            and lg.created_at is not None
            and plan.ends_at <= lg.created_at < plan.ends_at + LOG_AFTER
            for lg in mine
        )
        if not (answered or worked or logged_after):
            ids.append(item_id)
    return Confirmation(plan.window_id, plan.starts_at, plan.ends_at, tuple(ids)) if ids else None


def busy_windows(p: Planning, start: datetime, end: datetime) -> Sequence[WindowSpan]:
    """Work windows overlapping [start, end), for the day rail and the vault's Today note."""
    return [sp for sp in p.spans if sp.end > start and sp.start < end]
