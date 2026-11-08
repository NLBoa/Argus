"""The goals page (D079): view models built from stored goal files and FlowSavvy sessions.

Everything shown is stored data run through the pure rules in `planner/goals.py`, with the
numbers from `goals.rules`. No AI. FlowSavvy parts appear only with `Feature.GOAL_SESSIONS`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath

from argus.capabilities import Capabilities, Feature
from argus.clock import to_local
from argus.config import AppConfig
from argus.domain.goals import (
    DocError,
    GoalDoc,
    NextStep,
    WeekDoc,
    check_goal_set,
    week_monday,
    week_problems,
)
from argus.planner.goals import (
    GoalRules,
    Session,
    TopicState,
    TrendPoint,
    WeekProgress,
    next_new,
    on_plan,
    topic_states,
    trend,
    week_bounds,
    week_key,
    week_of,
    week_progress,
)
from argus.store import repo
from argus.store.repo import StoredGoalDoc
from argus.web.format import clock_time
from argus.web.study_data import obsidian_link
from argus.web.viewmodels import ACCENTS, StatusNote

TREND_WEEKS = 4
DASH = "\N{EN DASH}"
CHANGE_LABELS = {"none": "None", "pace": "Pace", "method": "Method", "length": "Session length"}
STATE_LABELS = {
    "done": "Done",
    "scheduled": "Scheduled",
    "not_done": "Not done",
    "dropped": "Dropped",
    "moved": "Moved",
}


# --- view models --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MilestoneView:
    title: str
    state: str  # done | current | next
    done_when: str | None


@dataclass(frozen=True, slots=True)
class SessionView:
    what: str
    day_label: str  # "Fri Nov 13"
    time_label: str  # "7 pm"
    minutes_label: str  # "60 min", "2 h"
    state: str
    state_label: str  # Done, Next, Scheduled, Not done, Dropped, Moved
    is_next: bool
    done_when: str | None
    start_at: datetime


@dataclass(frozen=True, slots=True)
class MeterView:
    done_pct: float
    scheduled_pct: float
    target_pct: float
    label: str  # the meter's accessible name, a full sentence


@dataclass(frozen=True, slots=True)
class WeekView:
    focus: str | None
    target_minutes: int
    target_source: str  # plan | scheduled
    summary: str
    rows: list[SessionView]  # done, scheduled, and not done, by start
    dropped: list[SessionView]  # dropped and moved
    meter: MeterView | None


@dataclass(frozen=True, slots=True)
class GoalView:
    tag: str
    accent: str
    aim: str
    why: str | None
    status: str
    path: str
    track: list[MilestoneView]
    current: MilestoneView | None
    next: list[NextStep]
    week: WeekView | None
    on_plan_text: str | None
    stale_text: str | None  # "Showing the version read Sat 9:02 am"

    @property
    def section_id(self) -> str:
        return f"goal-{self.tag.lower()}"


@dataclass(frozen=True, slots=True)
class ProblemView:
    path: str
    name: str  # the file name
    line: int | None
    field: str
    message: str
    href: str | None  # obsidian:// when the note exists
    showing_good: bool  # the page still shows the last valid version

    @property
    def text(self) -> str:
        where = f" on line {self.line}" if self.line else ""
        return f"{self.name} has an error{where}"


@dataclass(frozen=True, slots=True)
class ChangeView:
    kind: str
    kind_label: str
    what: str
    why: str


@dataclass(frozen=True, slots=True)
class TopicRowView:
    goal: str
    accent: str
    title: str
    cells: list[str]  # one score per trend week, or a dash
    status: str
    status_label: str
    overdue: bool


@dataclass(frozen=True, slots=True)
class TrendBar:
    week_label: str
    done: int
    target: int
    partial: bool
    done_pct: float  # of the row's scale
    target_pct: float


@dataclass(frozen=True, slots=True)
class TrendRow:
    tag: str
    accent: str
    bars: list[TrendBar]


@dataclass(frozen=True, slots=True)
class TrendView:
    week_labels: list[str]
    rows: list[TrendRow]
    scale: int  # minutes at 100%


@dataclass(frozen=True, slots=True)
class GoalsPage:
    week_key: str
    week_label: str
    summary: str | None
    change: ChangeView | None
    week_note: str | None  # no plan (or a bad one) for this week
    goals: list[GoalView]
    inactive: list[GoalView]
    problems: list[ProblemView]
    topics: list[TopicRowView]
    topic_weeks: list[str]
    next_new: list[tuple[str, str]]
    trend: TrendView | None
    trend_note: str
    sessions: bool  # FlowSavvy is set up


# --- formatting ---------------------------------------------------------------------------------


def minutes_label(m: int) -> str:
    return f"{m // 60} h" if m >= 120 and m % 60 == 0 else f"{m} min"


def _day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _week_label(key: str) -> str:
    monday = week_monday(key)
    sunday = monday + timedelta(days=6)
    end = str(sunday.day) if sunday.month == monday.month else _day(sunday)
    return f"Week {int(key[-2:])}, {_day(monday)}{DASH}{end}"


def _short_week(key: str) -> str:
    return f"W{key[-2:]}"


def _read_at(dt: datetime) -> str:
    return f"{to_local(dt):%a} {clock_time(dt)}"


# --- loading ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Docs:
    goals: dict[str, tuple[StoredGoalDoc, GoalDoc]]  # by tag, valid version only
    weeks: dict[str, WeekDoc]
    problems: list[ProblemView]


def _problem(config: AppConfig, path: str, err: DocError, showing_good: bool) -> ProblemView:
    return ProblemView(
        path=path,
        name=PurePosixPath(path).name,
        line=err.line,
        field=err.path,
        message=err.message,
        href=obsidian_link(config, path),
        showing_good=showing_good,
    )


def _docs(conn: sqlite3.Connection, config: AppConfig) -> _Docs:
    stored = repo.goal_docs(conn)
    problems: list[ProblemView] = []
    goal_docs: dict[str, GoalDoc] = {d.path: d.good for d in stored if isinstance(d.good, GoalDoc)}
    dupes = check_goal_set(goal_docs)
    goals: dict[str, tuple[StoredGoalDoc, GoalDoc]] = {}
    weeks: dict[str, WeekDoc] = {}
    for d in stored:
        if d.error is not None:
            problems.append(_problem(config, d.path, d.error, d.good is not None))
        if isinstance(d.good, GoalDoc):
            if d.path in dupes:
                if d.error is None:
                    problems.append(_problem(config, d.path, dupes[d.path], False))
                continue
            goals[d.good.tag] = (d, d.good)
        elif isinstance(d.good, WeekDoc):
            weeks[d.good.week] = d.good
    return _Docs(goals, weeks, problems)


def _accent(goal: GoalDoc, n: int) -> str:
    return goal.accent if goal.accent in ACCENTS else ACCENTS[n % len(ACCENTS)]


def _order(goals: Mapping[str, tuple[StoredGoalDoc, GoalDoc]], week: WeekDoc | None) -> list[str]:
    """The week file's order first (the review lists goals the way the student thinks of
    them), then the rest by path."""
    planned = [t for t in (week.goals if week else {}) if t in goals]
    rest = sorted((t for t in goals if t not in planned), key=lambda t: goals[t][0].path)
    return planned + rest


def _session_view(s: Session, state: str, is_next: bool) -> SessionView:
    local = to_local(s.start_at)
    return SessionView(
        what=s.what,
        day_label=f"{local:%a} {_day(local.date())}",
        time_label=clock_time(s.start_at),
        minutes_label=minutes_label(s.minutes),
        state=state,
        state_label="Next" if is_next else STATE_LABELS[state],
        is_next=is_next,
        done_when=s.done_when,
        start_at=s.start_at,
    )


def _meter(p: WeekProgress) -> MeterView:
    scale = max(p.target_minutes, p.done_minutes + p.scheduled_minutes + p.not_done_minutes, 1)
    label = (
        f"{p.done_minutes} of {p.target_minutes} minutes done, {p.scheduled_minutes} more scheduled"
    )
    return MeterView(
        done_pct=round(100 * p.done_minutes / scale, 2),
        scheduled_pct=round(100 * p.scheduled_minutes / scale, 2),
        target_pct=round(100 * p.target_minutes / scale, 2),
        label=label,
    )


def _week_view(p: WeekProgress, next_id: str | None) -> WeekView:
    rows = [
        _session_view(r.session, r.state, r.session.external_id == next_id)
        for r in p.rows
        if r.state in ("done", "scheduled", "not_done")
    ]
    dropped = [
        _session_view(r.session, r.state, False) for r in p.rows if r.state in ("dropped", "moved")
    ]
    summary = (
        f"{p.done_minutes} of {p.target_minutes} min done, {p.scheduled_minutes} scheduled. "
        f"{p.sessions_done} of {p.sessions_planned} sessions."
    )
    return WeekView(p.focus, p.target_minutes, p.target_source, summary, rows, dropped, _meter(p))


def _target_only(week: WeekDoc | None, tag: str) -> WeekView | None:
    goal = week.goals.get(tag) if week else None
    if goal is None:
        return None
    return WeekView(
        goal.focus, goal.target_minutes, "plan", f"Target {goal.target_minutes} min.", [], [], None
    )


def _on_plan_text(history: Sequence[WeekProgress], rules: GoalRules) -> str:
    p = on_plan(history, rules)
    if p.finished_weeks == 0:
        return "On-plan history starts after Sunday."
    if p.may_grow:
        return f"On plan {p.streak} weeks running, so time can grow."
    if p.last_share is not None and p.last_share < rules.on_plan_share:
        return f"Below plan last week ({round(p.last_share * 100)}%), so hold time steady."
    need = rules.on_plan_weeks
    return f"On plan {p.streak} of the last {need} weeks, so hold time steady."


def _since_first_data(
    progress: Sequence[WeekProgress], plans: Mapping[str, WeekDoc]
) -> list[WeekProgress]:
    """Weeks before the goal had a session or a target don't count as off plan."""
    for i, p in enumerate(progress):
        plan = plans.get(p.week)
        if p.rows or (plan is not None and p.tag in plan.goals):
            return list(progress[i:])
    return []


def _topic_label(s: TopicState) -> str:
    if s.status == "reteach":
        method = f"“{s.last_method}”" if s.last_method else "last time"
        return f"Re-teach with something other than {method}"
    if s.status == "practice":
        return "Practice more"
    if s.status == "review_due" and s.due is not None:
        return f"Review overdue: due {_day(s.due)}" if s.overdue else f"Review due {_day(s.due)}"
    if s.status == "review_later" and s.due is not None:
        return f"Review {_day(s.due)}"
    return "Kept"


def _topic_rows(
    states: Sequence[TopicState], accent: str, weeks: Sequence[str]
) -> list[TopicRowView]:
    out: list[TopicRowView] = []
    for s in states:
        if not s.history:
            continue
        cells = []
        for w in weeks:
            scores = [r.score for r in s.history if week_key(r.date) == w]
            cells.append(str(scores[-1]) if scores else DASH)
        out.append(
            TopicRowView(s.goal, accent, s.topic.title, cells, s.status, _topic_label(s), s.overdue)
        )
    return out


def _trend_view(
    rows: list[tuple[str, str, Sequence[TrendPoint]]], weeks: Sequence[str]
) -> TrendView | None:
    """None until a week has finished: one partial bar is not a trend."""
    points = [p for _, _, ps in rows for p in ps]
    if not any(not p.partial for p in points):
        return None
    scale = max([1, *(max(p.done_minutes, p.target_minutes) for p in points)])
    view_rows: list[TrendRow] = []
    for tag, accent, ps in rows:
        by_week = {p.week: p for p in ps}
        bars = []
        for w in weeks:
            p = by_week.get(w)
            done, target, partial = (
                (p.done_minutes, p.target_minutes, p.partial) if p else (0, 0, False)
            )
            bars.append(
                TrendBar(
                    _short_week(w),
                    done,
                    target,
                    partial,
                    round(100 * done / scale, 2),
                    round(100 * target / scale, 2),
                )
            )
        view_rows.append(TrendRow(tag, accent, bars))
    return TrendView([_short_week(w) for w in weeks], view_rows, scale)


def _next_session_id(sessions: Sequence[Session], now: datetime) -> str | None:
    """The first session still to come, across every goal: the one marked Next (teal)."""
    upcoming = [
        s
        for s in sessions
        if not s.completed and s.gone_at is None and s.moved_at is None and s.end_at > now
    ]
    return min(upcoming, key=lambda s: s.start_at).external_id if upcoming else None


def _goal_view(
    stored: StoredGoalDoc,
    goal: GoalDoc,
    accent: str,
    week: WeekView | None,
    on_plan_text: str | None,
) -> GoalView:
    track = [MilestoneView(m.title, m.status, m.done_when) for m in goal.track]
    stale = (
        f"Showing the version read {_read_at(stored.good_at)}"
        if stored.error is not None and stored.good_at is not None
        else None
    )
    return GoalView(
        tag=goal.tag,
        accent=accent,
        aim=goal.aim,
        why=goal.why,
        status=goal.status,
        path=stored.path,
        track=track,
        current=next((m for m in track if m.state == "current"), None),
        next=list(goal.next),
        week=week,
        on_plan_text=on_plan_text,
        stale_text=stale,
    )


def _plan_note(config: AppConfig, week: str, plan: WeekDoc | None) -> str | None:
    if plan is not None:
        return None
    folder = config.goals.folder if config.goals else "Goals"
    return (
        f"No plan for week {int(week[-2:])} yet. The Saturday review writes "
        f"{folder}/Weeks/{week}.md."
    )


def _change(plan: WeekDoc | None) -> ChangeView | None:
    if plan is None or plan.change is None:
        return None
    c = plan.change
    return ChangeView(c.kind, CHANGE_LABELS[c.kind], c.what, c.why)


def load_goals(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> GoalsPage:
    rules = config.goals.rules.as_rules() if config.goals else GoalRules()
    folder = config.goals.folder if config.goals else "Goals"
    sessions_on = caps.has(Feature.GOAL_SESSIONS)
    docs = _docs(conn, config)
    this_week = week_of(now)
    this_monday = week_monday(this_week)
    weeks = [week_key(this_monday - timedelta(weeks=i)) for i in reversed(range(TREND_WEEKS))]
    plan = docs.weeks.get(this_week)
    problems = list(docs.problems)
    if plan is not None:
        valid = {tag: g for tag, (_, g) in docs.goals.items()}
        path = f"{folder}/Weeks/{this_week}.md"
        problems += [_problem(config, path, e, False) for e in week_problems(plan, valid)]
    sessions: list[Session] = []
    if sessions_on:
        sessions = repo.goal_sessions_between(
            conn, week_bounds(weeks[0])[0], week_bounds(this_week)[1]
        )
    next_id = _next_session_id(sessions, now)
    today = to_local(now).date()
    # A result dated after today is a typo or not taken yet: it doesn't count.
    tests = [t for w in docs.weeks.values() for t in w.tests if t.date <= today]

    active: list[GoalView] = []
    inactive: list[GoalView] = []
    topics: list[TopicRowView] = []
    new_topics: list[tuple[str, str]] = []
    trend_rows: list[tuple[str, str, Sequence[TrendPoint]]] = []
    totals = [0, 0, 0, 0]  # sessions done, planned, minutes done, target
    for n, tag in enumerate(_order(docs.goals, plan)):
        stored, goal = docs.goals[tag]
        accent = _accent(goal, n)
        if goal.status != "active":
            inactive.append(_goal_view(stored, goal, accent, None, None))
            continue
        week, on_plan_text = _target_only(plan, tag), None
        if sessions_on:
            progress = [week_progress(tag, w, docs.weeks.get(w), sessions, now) for w in weeks]
            current = progress[-1]
            week = _week_view(current, next_id)
            on_plan_text = _on_plan_text(_since_first_data(progress, docs.weeks), rules)
            totals = [
                totals[0] + current.sessions_done,
                totals[1] + current.sessions_planned,
                totals[2] + current.done_minutes,
                totals[3] + current.target_minutes,
            ]
            trend_rows.append((tag, accent, trend(tag, sessions, docs.weeks, now, TREND_WEEKS)))
        active.append(_goal_view(stored, goal, accent, week, on_plan_text))
        states = topic_states(goal, tests, today, rules)
        topics += _topic_rows(states, accent, weeks)
        new_topics += [(tag, s.topic.title) for s in next_new(states, limit=1)]

    summary = None
    if sessions_on and active:
        summary = f"{totals[0]} of {totals[1]} sessions done, {totals[2]} of {totals[3]} minutes."
    return GoalsPage(
        week_key=this_week,
        week_label=_week_label(this_week),
        summary=summary,
        change=_change(plan),
        week_note=_plan_note(config, this_week, plan),
        goals=active,
        inactive=inactive,
        problems=problems,
        topics=topics,
        topic_weeks=[_short_week(w) for w in weeks],
        next_new=new_topics,
        trend=_trend_view(trend_rows, weeks) if sessions_on else None,
        trend_note=f"Trends start when week {int(this_week[-2:])} ends. Each finished week adds a bar.",
        sessions=sessions_on,
    )


# --- the status line, Today, and the weekly note ----------------------------------------------


def goal_problem_notes(conn: sqlite3.Connection) -> list[StatusNote]:
    """'Projects.md has an error on line 14' for the status line (D067)."""
    out = []
    for d in repo.goal_docs(conn):
        if d.error is not None:
            where = f" on line {d.error.line}" if d.error.line else ""
            out.append(
                StatusNote(f"{PurePosixPath(d.path).name} has an error{where}", "failing", "/goals")
            )
    return out


def _active_tags(conn: sqlite3.Connection) -> list[str]:
    docs: dict[str, GoalDoc] = {
        d.path: d.good for d in repo.goal_docs(conn) if isinstance(d.good, GoalDoc)
    }
    dupes = check_goal_set(docs)
    return [g.tag for p, g in docs.items() if g.status == "active" and p not in dupes]


def today_goal_line(conn: sqlite3.Connection, caps: Capabilities, now: datetime) -> str | None:
    """One line for Today: goal sessions starting today that haven't happened yet."""
    if not caps.has(Feature.GOAL_SESSIONS) or not repo.has_goal_docs(conn):
        return None
    tags = set(_active_tags(conn))
    day = to_local(now).date()
    start = week_bounds(week_key(day))[0]
    todays = [
        s
        for s in repo.goal_sessions_between(conn, start, start + timedelta(days=8))
        if s.tag in tags
        and to_local(s.start_at).date() == day
        and not s.completed
        and s.gone_at is None
        and s.moved_at is None
        and s.end_at > now
    ]
    if not todays:
        return None
    if len(todays) == 1:
        s = todays[0]
        return f"Goal session at {clock_time(s.start_at)}: {s.tag}, {s.what} ({minutes_label(s.minutes)})."
    parts = "; ".join(f"{s.tag}, {s.what} ({minutes_label(s.minutes)})" for s in todays)
    return f"Goal sessions today: {parts}."


def weekly_goal_lines(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, day: date, now: datetime
) -> list[str]:
    """Lines for Argus's weekly vault note: each active goal's week, and the week's change."""
    if not caps.has(Feature.GOALS) or not repo.has_goal_docs(conn):
        return []
    docs = _docs(conn, config)
    key = week_key(day)
    plan = docs.weeks.get(key)
    sessions_on = caps.has(Feature.GOAL_SESSIONS)
    start, end = week_bounds(key)
    sessions = repo.goal_sessions_between(conn, start, end) if sessions_on else []
    lines: list[str] = []
    for tag in _order(docs.goals, plan):
        _, goal = docs.goals[tag]
        if goal.status != "active":
            continue
        planned = plan.goals.get(tag) if plan else None
        focus = f" {planned.focus}." if planned and planned.focus else ""
        if sessions_on:
            p = week_progress(tag, key, plan, sessions, now)
            line = (
                f"{tag}:{focus} {p.done_minutes} of {p.target_minutes} min, "
                f"{p.sessions_done} of {p.sessions_planned} sessions."
            )
            if p.dropped:
                line += " Dropped: " + ", ".join(s.what for s in p.dropped) + "."
        elif planned is not None:
            line = f"{tag}:{focus} Target {planned.target_minutes} min."
        else:
            continue
        lines.append(line)
    if plan is not None and plan.change is not None:
        c = plan.change
        if c.kind == "none":
            lines.append(f"Change this week: none ({c.what}).")
        else:
            lines.append(
                f"Change this week: {CHANGE_LABELS[c.kind].lower()}, {c.what}. Why: {c.why}"
            )
    return lines
