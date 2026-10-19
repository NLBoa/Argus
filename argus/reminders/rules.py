"""Reminder rules (pure): what should be said, to whom it refers, and how urgent it is.

Rules never decide *when* a message goes out. Guards do that (`guards.py`). Every reminder
carries idempotency keys of the form `<item_id>:<rule>:<due_at>`, so a moved due date re-arms
it. The engine records keys, and rules skip anything already sent.
Deadline rules are deterministic and never depend on the AI.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from argus.clock import to_local
from argus.domain.models import Item
from argus.reminders.text import (
    SEGMENT,
    due_phrase,
    fit,
    gsm_length,
    hours_short,
    short,
    to_gsm7,
    when,
    window_phrase,
)
from argus.store.db import iso

NEW_ITEM_HORIZON = timedelta(days=7)
NEW_ITEM_LOOKBACK = timedelta(hours=24)
DUE_SOON = timedelta(hours=24)
DUE_NOW = timedelta(hours=3)
ALERT_AFTER_FAILURES = 3


class Rule(StrEnum):
    START_NUDGE = "start_nudge"  # retired (D061): kept so old notifications and mutes still parse
    WINDOW_PLAN = "window_plan"
    AT_RISK = "at_risk"
    DUE_24H = "due_24h"
    DUE_3H = "due_3h"
    NEW_ITEM = "new_item"
    ANNOUNCED = "announced"
    EXAM_COUNTDOWN = "exam_countdown"
    MORNING_BRIEF = "morning_brief"
    SYSTEM_ALERT = "system_alert"

    @property
    def label(self) -> str:
        return {
            "start_nudge": "Start nudges (retired)",
            "window_plan": "Work window plan",
            "at_risk": "At-risk alerts",
            "due_24h": "Due in 24 hours",
            "due_3h": "Due in 3 hours",
            "new_item": "New items",
            "announced": "Announced quizzes and exams",
            "exam_countdown": "Exam countdowns",
            "morning_brief": "Morning brief",
            "system_alert": "System alerts",
        }[self.value]


@dataclass(frozen=True, slots=True)
class Reminder:
    rule: Rule
    keys: tuple[str, ...]
    title: str
    body: str
    item_ids: tuple[int, ...] = ()
    priority: int = 3  # ntfy scale: 1 min .. 5 urgent
    bypass_quiet: bool = False  # ignores quiet hours and class time
    counts_toward_cap: bool = True
    expires_at: datetime | None = None  # after this, drop rather than defer

    @property
    def sms(self) -> str:
        return fit(f"{self.title}: {self.body}")


@dataclass(frozen=True, slots=True)
class SourceAlertState:
    source: str
    failures: int
    failing_since: datetime | None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class NoticeEntry:
    """One line of a work-window message."""

    item_id: int | None  # None for exam study (never numbered: DONE n must not hit an exam)
    course: str
    title: str
    hours: float
    due: datetime | None
    at_risk: bool = False


@dataclass(frozen=True, slots=True)
class WindowNotice:
    window_id: str
    start: datetime
    end: datetime
    entries: tuple[NoticeEntry, ...]


@dataclass(frozen=True, slots=True)
class Brief:
    day: date
    body: str
    item_ids: tuple[int, ...]
    until: datetime  # dropped after this (end of the day)


@dataclass(frozen=True, slots=True)
class Countdown:
    item: Item
    days: int  # 7, 3, or 1
    weakest: tuple[str, ...]  # concept names, weakest first
    until: datetime


@dataclass(frozen=True, slots=True)
class Announced:
    """A quiz/exam finding from a staff post: new, moved, rescoped, or waiting for OK/NO."""

    finding_id: int
    course_code: str
    kind: str  # new_assessment | date_change | scope | cancellation
    title: str
    status: str  # applied | conflict | pending
    date: datetime | None
    item_id: int | None
    official: datetime | None  # the official date when the post disagrees with it
    scope: str | None
    until: datetime


@dataclass(frozen=True, slots=True)
class Snapshot:
    now: datetime
    items: Sequence[Item]
    sent_keys: frozenset[str] = frozenset()
    sources: Mapping[str, SourceAlertState] | None = None
    # Items created before this belong to the first import and never count as "new".
    bootstrap_until: datetime | None = None
    at_risk: frozenset[int] = frozenset()  # item ids the planner says are at risk
    # (window hours before due, hours due by then) for at-risk items, for the message
    at_risk_load: Mapping[int, tuple[float, float]] = field(default_factory=dict)
    # Items planned for the window now in progress: their at-risk text waits until it ends (D061)
    in_window: frozenset[int] = frozenset()
    window: WindowNotice | None = None  # the current or next work window and its split
    notice_lead: timedelta = timedelta(minutes=20)
    ai_budget_month: str | None = None  # "2026-10" when the AI budget was hit this month
    brief: Brief | None = None  # today's morning brief, once its time has come
    countdowns: Sequence[Countdown] = ()
    announced: Sequence[Announced] = ()
    # Items no enabled source reports submissions for (D056): same timing, different wording.
    untracked: frozenset[int] = frozenset()


def item_key(item: Item, rule: Rule) -> str:
    due = iso(item.due_at) if item.due_at else "none"
    return f"{item.id}:{rule.value}:{due}"


def _deadline_candidates(items: Iterable[Item]) -> list[Item]:
    return [i for i in items if i.status.is_open and i.due_at and i.kind.takes_submission]


def _day_title(due: datetime, now: datetime) -> str:
    """By local calendar day, not hours left: a reminder deferred past quiet hours is "Due today"."""
    return "Due today" if to_local(due).date() == to_local(now).date() else "Due tomorrow"


def due_soon(snap: Snapshot) -> list[Reminder]:
    out = []
    for it in _deadline_candidates(snap.items):
        assert it.due_at is not None
        left = it.due_at - snap.now
        if DUE_NOW < left <= DUE_SOON and item_key(it, Rule.DUE_24H) not in snap.sent_keys:
            out.append(
                Reminder(
                    rule=Rule.DUE_24H,
                    keys=(item_key(it, Rule.DUE_24H),),
                    title=_day_title(it.due_at, snap.now),
                    body=f"[1] {it.course_code} {short(it.title, 70)}, {when(it.due_at)}. "
                    "Reply DONE 1 when submitted.",
                    item_ids=(it.id,),
                    priority=4,
                    expires_at=it.due_at - DUE_NOW,
                )
            )
    return out


def due_now(snap: Snapshot) -> list[Reminder]:
    out = []
    for it in _deadline_candidates(snap.items):
        assert it.due_at is not None
        left = it.due_at - snap.now
        if timedelta(0) < left <= DUE_NOW and item_key(it, Rule.DUE_3H) not in snap.sent_keys:
            hours = max(1, round(left.total_seconds() / 3600))
            out.append(
                Reminder(
                    rule=Rule.DUE_3H,
                    keys=(item_key(it, Rule.DUE_3H),),
                    title=f"Due in {hours}h"
                    if left >= timedelta(minutes=50)
                    else "Due within the hour",
                    body=f"[1] {it.course_code} {short(it.title, 70)}, {when(it.due_at)}. "
                    + (
                        "Reply DONE 1 once submitted."
                        if it.id in snap.untracked
                        else "Not submitted yet."
                    ),
                    item_ids=(it.id,),
                    priority=5,
                    bypass_quiet=True,
                    counts_toward_cap=False,
                    expires_at=it.due_at,
                )
            )
    return out


def new_items(snap: Snapshot) -> list[Reminder]:
    fresh = [
        it
        for it in _deadline_candidates(snap.items)
        if it.created_at is not None
        and snap.now - it.created_at <= NEW_ITEM_LOOKBACK
        and (snap.bootstrap_until is None or it.created_at > snap.bootstrap_until)
        and it.due_at is not None
        and snap.now < it.due_at <= snap.now + NEW_ITEM_HORIZON
        and item_key(it, Rule.NEW_ITEM) not in snap.sent_keys
    ]
    if not fresh:
        return []
    fresh.sort(key=lambda i: (i.due_at, i.id))
    parts = []
    for n, it in enumerate(fresh[:3], start=1):
        assert it.due_at is not None
        parts.append(f"[{n}] {it.course_code} {short(it.title, 34)} ({when(it.due_at)})")
    more = f" +{len(fresh) - 3} more" if len(fresh) > 3 else ""
    return [
        Reminder(
            rule=Rule.NEW_ITEM,
            keys=tuple(item_key(i, Rule.NEW_ITEM) for i in fresh),
            title="New item" if len(fresh) == 1 else f"{len(fresh)} new items",
            body="; ".join(parts) + more,
            item_ids=tuple(i.id for i in fresh[:3]),
            priority=3,
            expires_at=min(i.due_at for i in fresh if i.due_at),
        )
    ]


SOURCE_NAMES = {
    "canvas_ical": "ELMS calendar",
    "canvas_api": "ELMS",
    "canvas_modules": "ELMS modules",
    "gradescope": "Gradescope",
    "piazza": "Piazza",
    "piazza_email": "Piazza email",
    "umdio": "umd.io",
    "vault": "Vault git",
}


def system_alerts(snap: Snapshot) -> list[Reminder]:
    out = []
    for state in (snap.sources or {}).values():
        if state.failures < ALERT_AFTER_FAILURES or state.failing_since is None:
            continue
        key = f"system:collector:{state.source}:{iso(state.failing_since)}"
        if key in snap.sent_keys:
            continue
        name = SOURCE_NAMES.get(state.source, state.source)
        out.append(
            Reminder(
                rule=Rule.SYSTEM_ALERT,
                keys=(key,),
                title=f"{name} sync failing",
                body=f"{state.failures} failed syncs in a row since {when(state.failing_since)}. "
                "Deadlines from it may be stale; check Health.",
                priority=4,
            )
        )
    return out


def at_risk(snap: Snapshot) -> list[Reminder]:
    """One message for everything newly at risk (keys stay per item, D020). An item planned for
    the window in progress waits until the window ends: the student may be working on it."""
    risky = [
        it
        for it in _deadline_candidates(snap.items)
        if it.due_at is not None
        and it.due_at > snap.now
        and it.id in snap.at_risk
        and it.id not in snap.in_window
        and item_key(it, Rule.AT_RISK) not in snap.sent_keys
    ]
    if not risky:
        return []
    risky.sort(key=lambda i: (i.due_at, i.id))
    keys = tuple(item_key(i, Rule.AT_RISK) for i in risky)
    expires = min(i.due_at for i in risky if i.due_at)
    if len(risky) == 1:
        it = risky[0]
        assert it.due_at is not None
        load = snap.at_risk_load.get(it.id)
        why = (
            f"only {hours_short(load[0])} of work windows before {when(it.due_at)} for "
            f"{hours_short(load[1])} of work"
            if load
            else f"not enough work-window time before {when(it.due_at)}"
        )
        body = f"[1] {it.course_code} {short(it.title, 50)}: {why}. Reply EST 1 h to correct the estimate."
        return [
            Reminder(
                rule=Rule.AT_RISK,
                keys=keys,
                title="At risk",
                body=fit(body, SEGMENT - len("At risk: ")),
                item_ids=(it.id,),
                priority=4,
                counts_toward_cap=False,
                expires_at=expires,
            )
        ]
    tail = " Reply EST n h to fix an estimate."
    title = f"{len(risky)} at risk"
    parts: list[str] = []
    for n, it in enumerate(risky, start=1):
        assert it.due_at is not None
        part = f"[{n}] {it.course_code} {short(it.title, 30)} ({due_phrase(it.due_at, snap.now)})"
        if gsm_length(to_gsm7(f"{title}: " + "; ".join([*parts, part]) + "." + tail)) > SEGMENT:
            break
        parts.append(part)
    shown = tuple(i.id for i in risky[: len(parts)])
    return [
        Reminder(
            rule=Rule.AT_RISK,
            keys=keys,
            title=title,
            body="; ".join(parts) + "." + tail,
            item_ids=shown,
            priority=4,
            counts_toward_cap=False,
            expires_at=expires,
        )
    ]


NOTICE_AFTER_START = timedelta(minutes=30)


def window_plan(snap: Snapshot) -> list[Reminder]:
    """The work-window message (D061): once per window, `notice_lead` before it starts, saying
    what to do and why. Built entry by entry so it fits one segment and its [n] numbers match
    `item_ids` exactly. Not counted toward the daily cap (at most 3 windows a day)."""
    w = snap.window
    if w is None or not w.entries:
        return []
    if not w.start - snap.notice_lead <= snap.now < w.start + NOTICE_AFTER_START:
        return []
    key = f"window:{w.window_id}"
    if key in snap.sent_keys:
        return []
    title = window_phrase(w.start, w.end, snap.now)
    parts: list[str] = []
    ids: list[int] = []
    for e in w.entries:
        if e.item_id is None:
            part = f"Study {hours_short(e.hours)}: {short(e.title, 30)}."
        else:
            due = f", {due_phrase(e.due, snap.now)}" if e.due else ""
            risk = " (at risk)" if e.at_risk else ""
            part = f"[{len(ids) + 1}] {short(e.title, 30)}, {hours_short(e.hours)} left{due}{risk}."
        if gsm_length(to_gsm7(f"{title}: " + " ".join([*parts, part]))) > SEGMENT:
            break
        parts.append(part)
        if e.item_id is not None:
            ids.append(e.item_id)
    if not parts:
        return []
    return [
        Reminder(
            rule=Rule.WINDOW_PLAN,
            keys=(key,),
            title=title,
            body=" ".join(parts),
            item_ids=tuple(ids),
            priority=4,
            counts_toward_cap=False,
            expires_at=w.start + NOTICE_AFTER_START,
        )
    ]


def ai_budget_alert(snap: Snapshot) -> list[Reminder]:
    if snap.ai_budget_month is None:
        return []
    key = f"system:ai_budget:{snap.ai_budget_month}"
    if key in snap.sent_keys:
        return []
    return [
        Reminder(
            rule=Rule.SYSTEM_ALERT,
            keys=(key,),
            title="AI budget reached",
            body="Argus is using its non-AI fallbacks until next month. Deadlines and reminders are unaffected.",
            priority=3,
        )
    ]


def morning_brief(snap: Snapshot) -> list[Reminder]:
    b = snap.brief
    if b is None or (key := f"brief:{b.day.isoformat()}") in snap.sent_keys:
        return []
    return [
        Reminder(
            rule=Rule.MORNING_BRIEF,
            keys=(key,),
            title="Today",
            body=b.body,
            item_ids=b.item_ids,
            priority=2,
            expires_at=b.until,
        )
    ]


def exam_countdowns(snap: Snapshot) -> list[Reminder]:
    out = []
    for c in snap.countdowns:
        key = f"{item_key(c.item, Rule.EXAM_COUNTDOWN)}:{c.days}"
        if key in snap.sent_keys or c.item.due_at is None:
            continue
        span = "tomorrow" if c.days == 1 else f"in {c.days} days"
        weakest = f" Weakest: {', '.join(c.weakest)}." if c.weakest else ""
        out.append(
            Reminder(
                rule=Rule.EXAM_COUNTDOWN,
                keys=(key,),
                title=f"Exam {span}",
                # Practice exists only with verified concept notes (D055): no promise without them.
                body=f"[1] {c.item.course_code} {short(c.item.title, 40)}, {when(c.item.due_at)}.{weakest}"
                + (" Practice on Exam prep." if c.weakest else ""),
                item_ids=(c.item.id,),
                priority=3,
                expires_at=c.until,
            )
        )
    return out


def _announced_body(a: Announced) -> tuple[str, str, int]:
    """(title, body, priority) for an announced change."""
    what = f"{a.course_code} {short(a.title, 40)}"
    if a.status == "conflict" and a.date is not None and a.official is not None:
        return (
            "Dates disagree",
            f"[1] {what}: post says {when(a.date)}, official {when(a.official)}. "
            "Reply OK 1 to use the post's date, NO 1 to keep the official one.",
            4,
        )
    if a.kind == "cancellation":
        return (
            "Cancelled?",
            f"[1] {what} looks cancelled in a post. Reply OK 1 to hide it, NO 1 to keep it.",
            4,
        )
    if a.kind == "scope":
        return ("Scope posted", f"[1] {what} covers: {short(a.scope or '', 90)}", 3)
    if a.kind == "date_change" and a.date is not None:
        return ("Date moved", f"[1] {what} is now {when(a.date)} (from a post).", 4)
    due = f", {when(a.date)}" if a.date else ""
    return ("Announced", f"[1] {what}{due} (from a post; not on ELMS yet).", 3)


def announced(snap: Snapshot) -> list[Reminder]:
    out = []
    for a in snap.announced:
        key = f"finding:{a.finding_id}:{a.status}"
        if key in snap.sent_keys:
            continue
        title, body, priority = _announced_body(a)
        out.append(
            Reminder(
                rule=Rule.ANNOUNCED,
                keys=(key,),
                title=title,
                body=body,
                item_ids=(a.item_id,) if a.item_id is not None else (),
                priority=priority,
                expires_at=a.until,
            )
        )
    return out


def candidates(snap: Snapshot) -> list[Reminder]:
    """Everything that could be sent now, most urgent first."""
    found = (
        due_now(snap)
        + due_soon(snap)
        + at_risk(snap)
        + window_plan(snap)
        + new_items(snap)
        + announced(snap)
        + exam_countdowns(snap)
        + morning_brief(snap)
        + system_alerts(snap)
        + ai_budget_alert(snap)
    )
    return sorted(found, key=lambda r: (-r.priority, r.keys))
