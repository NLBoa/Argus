"""Goals (D079) outside the web views: the line on Today and the goals section of Argus's
weekly vault note. Both read stored goal files and FlowSavvy sessions; no AI."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta

from argus.capabilities import Capabilities, Feature
from argus.clock import to_local
from argus.domain.goals import GoalDoc, WeekDoc, check_goal_set
from argus.planner.goals import week_bounds, week_key, week_progress
from argus.store import repo
from argus.store.repo import StoredGoalDoc
from argus.web.format import clock_time

CHANGE_LABELS = {"none": "None", "pace": "Pace", "method": "Method", "length": "Session length"}


def minutes_label(m: int) -> str:
    return f"{m // 60} h" if m >= 120 and m % 60 == 0 else f"{m} min"


def order_goals(
    goals: Mapping[str, tuple[StoredGoalDoc, GoalDoc]], week: WeekDoc | None
) -> list[str]:
    """The week file's order first (the review lists goals the way the student thinks of
    them), then the rest by path."""
    planned = [t for t in (week.goals if week else {}) if t in goals]
    rest = sorted((t for t in goals if t not in planned), key=lambda t: goals[t][0].path)
    return planned + rest


def goal_set(
    conn: sqlite3.Connection,
) -> tuple[dict[str, tuple[StoredGoalDoc, GoalDoc]], dict[str, WeekDoc]]:
    """Valid goal files by tag (a duplicated tag keeps its first file) and week files by week."""
    stored = repo.goal_docs(conn)
    goal_docs: dict[str, GoalDoc] = {d.path: d.good for d in stored if isinstance(d.good, GoalDoc)}
    dupes = check_goal_set(goal_docs)
    goals = {
        d.good.tag: (d, d.good)
        for d in stored
        if isinstance(d.good, GoalDoc) and d.path not in dupes
    }
    weeks = {d.good.week: d.good for d in stored if isinstance(d.good, WeekDoc)}
    return goals, weeks


def _active_tags(conn: sqlite3.Connection) -> list[str]:
    goals, _ = goal_set(conn)
    return [tag for tag, (_, g) in goals.items() if g.status == "active"]


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
    conn: sqlite3.Connection, caps: Capabilities, day: date, now: datetime
) -> list[str]:
    """Lines for Argus's weekly vault note: each active goal's week, and the week's change."""
    if not caps.has(Feature.GOALS) or not repo.has_goal_docs(conn):
        return []
    goals, weeks = goal_set(conn)
    key = week_key(day)
    plan = weeks.get(key)
    sessions_on = caps.has(Feature.GOAL_SESSIONS)
    start, end = week_bounds(key)
    sessions = repo.goal_sessions_between(conn, start, end) if sessions_on else []
    lines: list[str] = []
    for tag in order_goals(goals, plan):
        _, goal = goals[tag]
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
