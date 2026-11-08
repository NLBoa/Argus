"""Read-only views of Argus data for the Claude connector (the MCP server, D073).

Deadlines and links only: never grades, scores, points, descriptions, or submission contents.
Times go out as ISO 8601 with the America/New_York offset. The queries take a connection and
never write; the server opens that connection read-only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argus.clock import ensure_utc, to_local
from argus.config import RUN_CODE, AppConfig
from argus.domain.changes import summarize_changes
from argus.domain.goals import WeekDoc, week_monday, week_problems
from argus.domain.models import Item, ItemKind, ItemStatus, MeetingRecord, Source
from argus.domain.titles import is_project
from argus.estimates import effective_estimates
from argus.goals import goal_set, order_goals
from argus.planner.goals import (
    GoalRules,
    Session,
    TopicState,
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

ItemType = Literal["assignment", "quiz", "exam", "project", "event", "run", "slides", "file"]
SourceName = Literal["canvas", "gradescope", "piazza", "runna", "manual"]

MAX_DAYS = 60
OVERDUE_LOOKBACK = timedelta(days=7)
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Which record supplies the link and source when the due date's own source has none.
_LINK_ORDER = (
    Source.CANVAS_API,
    Source.CANVAS_ICAL,
    Source.GRADESCOPE,
    Source.FINDING,
    Source.RUNNA_ICAL,
    Source.MANUAL,
)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class WorkItem(_Out):
    id: str = Field(description="Stable Argus item id; use it to match items across calls.")
    course: str = Field(description="Course code, e.g. CMSC351.")
    title: str
    type: ItemType = Field(
        description="assignment, quiz, exam, project, event (an event's due is its start), or "
        "run (a Runna workout, due by the end of its day)."
    )
    due: str | None = Field(description="ISO 8601 with the America/New_York offset, or null.")
    link: str | None = Field(description="Where the work lives (Canvas, Gradescope, or the post).")
    source: SourceName = Field(description="Where the due date comes from.")
    updated_at: str = Field(description="When Argus last saw this item created or its date change.")
    done: bool = Field(description="Submitted, or marked done in Argus.")
    overdue: bool = Field(description="Past due and not done.")
    estimate_minutes: int | None = Field(
        default=None,
        description="How long it should take, in minutes (the student's own time, Runna's, "
        "or Argus's estimate).",
    )
    preferred_start: str | None = Field(
        default=None,
        description="Runs only: a good time to start (local). Flexible: any free time that day "
        "works.",
    )


class ChangedItem(WorkItem):
    status: Literal["new", "changed", "removed"]
    old_due: str | None = Field(
        default=None, description="For changed: the due date before `since` (null if it had none)."
    )


class Meeting(_Out):
    kind: str = Field(description="lecture, discussion, or lab.")
    days: list[str] = Field(description="Weekday names, e.g. ['Mon', 'Wed'].")
    start: str = Field(description="Local start time HH:MM (America/New_York).")
    end: str = Field(description="Local end time HH:MM (America/New_York).")
    location: str | None


class Course(_Out):
    code: str
    name: str | None
    meetings: list[Meeting]


class CourseList(_Out):
    courses: list[Course]


class UpcomingWork(_Out):
    as_of: str = Field(description="The time this answer was computed (America/New_York).")
    days: int
    items: list[WorkItem]


class Changes(_Out):
    as_of: str = Field(description="Pass this as `since` next time to get only newer changes.")
    since: str
    changes: list[ChangedItem]


# ---------------------------------------------------------------- mapping (pure)


def local_iso(dt: datetime | None) -> str | None:
    return to_local(dt).isoformat(timespec="seconds") if dt is not None else None


def item_type(kind: ItemKind, title: str, course: str) -> ItemType:
    if course == RUN_CODE:
        return "run"
    if kind == ItemKind.ASSIGNMENT:
        return "project" if is_project(title) else "assignment"
    return kind.value  # type: ignore[return-value]  # quiz, exam, event


def is_done(item: Item, now: datetime) -> bool:
    if item.status in (ItemStatus.SUBMITTED, ItemStatus.GRADED):
        return True
    # An exam or event is over once it starts (statuses refresh each cycle; this closes the gap).
    return not item.kind.takes_submission and item.due_at is not None and item.due_at <= now


def _source_name(source: Source, finding_platform: str | None) -> SourceName:
    if source.family == "canvas":
        return "canvas"
    if source == Source.GRADESCOPE:
        return "gradescope"
    if source == Source.RUNNA_ICAL:
        return "runna"
    if source == Source.FINDING:
        return "canvas" if finding_platform == "canvas" else "piazza"
    if source.family == "piazza":
        return "piazza"
    return "manual"


def origin_and_link(item: Item) -> tuple[Source, str | None]:
    """The record the item is reported from, and its link.

    An official due source (Canvas, Gradescope) wins. Otherwise the first record in
    `_LINK_ORDER`, so a Canvas item with a student-confirmed date still reports Canvas."""
    ranked = sorted(
        item.links,
        key=lambda link: (
            _LINK_ORDER.index(link.source) if link.source in _LINK_ORDER else len(_LINK_ORDER)
        ),
    )
    if item.due_source is not None and item.due_source.is_official:
        origin = item.due_source
    elif ranked:
        origin = ranked[0].source
    else:
        origin = item.due_source or Source.MANUAL
    same = [link.url for link in ranked if link.source.family == origin.family and link.url]
    anywhere = [link.url for link in ranked if link.url]
    urls = same or anywhere
    return origin, urls[0] if urls else None


def to_work_item(
    item: Item,
    now: datetime,
    *,
    updated_at: datetime | None,
    finding_platform: str | None = None,
    estimate_minutes: int | None = None,
    preferred_start: datetime | None = None,
) -> WorkItem:
    origin, link = origin_and_link(item)
    done = is_done(item, now)
    stamp = updated_at or item.created_at or now
    return WorkItem(
        id=str(item.id),
        course=item.course_code,
        title=item.title,
        type=item_type(item.kind, item.title, item.course_code),
        due=local_iso(item.due_at),
        link=link,
        source=_source_name(origin, finding_platform),
        updated_at=local_iso(stamp) or "",
        done=done,
        overdue=not done and item.due_at is not None and item.due_at < now,
        estimate_minutes=estimate_minutes,
        preferred_start=local_iso(preferred_start) if item.course_code == RUN_CODE else None,
    )


def meeting_out(m: MeetingRecord) -> Meeting:
    where = " ".join(p for p in (m.building, m.room) if p) or None
    return Meeting(
        kind=m.kind,
        days=[_DAY_NAMES[int(d)] for d in m.days],
        start=m.start.strftime("%H:%M"),
        end=m.end.strftime("%H:%M"),
        location=where,
    )


def parse_since(text: str) -> datetime:
    """An ISO 8601 instant with an offset (or Z). Naive times are refused: ambiguous."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"since must be ISO 8601, e.g. 2026-11-06T07:00:00-05:00: {exc}") from None
    try:
        return ensure_utc(parsed)
    except ValueError:
        raise ValueError(
            "since needs a timezone offset, e.g. 2026-11-06T07:00:00-05:00 or ...Z"
        ) from None


# ---------------------------------------------------------------- queries


def courses(conn: sqlite3.Connection) -> CourseList:
    meetings: dict[str, list[Meeting]] = {}
    for m in repo.list_meetings(conn):
        meetings.setdefault(m.course_code, []).append(meeting_out(m))
    return CourseList(
        courses=[
            Course(code=code, name=title, meetings=meetings.get(code, []))
            for code, title in repo.active_course_titles(conn).items()
        ]
    )


def _items_out(conn: sqlite3.Connection, items: list[Item], now: datetime) -> list[WorkItem]:
    ids = [i.id for i in items]
    stamps = repo.latest_change_at(conn, ids)
    platforms = repo.finding_platforms(conn)
    estimates = effective_estimates(conn, now)
    runs = repo.run_details(conn, [i.id for i in items if i.course_code == RUN_CODE])
    out = []
    for i in items:
        eff, run = estimates.get(i.id), runs.get(i.id)
        out.append(
            to_work_item(
                i,
                now,
                updated_at=stamps.get(i.id),
                finding_platform=platforms.get(i.id),
                estimate_minutes=round(eff.p50 * 60) if eff is not None else None,
                preferred_start=run.preferred_start if run is not None else None,
            )
        )
    return out


def listed(item: Item, now: datetime) -> bool:
    """In `get_upcoming_work`: due from now on, or late coursework that isn't done. A run whose
    day has ended leaves, done or not: a skipped run isn't late work to reschedule (D078)."""
    if item.due_at is None:
        return False
    if item.due_at >= now:
        return True
    return item.course_code != RUN_CODE and not is_done(item, now)


def upcoming(conn: sqlite3.Connection, now: datetime, days: int) -> UpcomingWork:
    """Work due in the next `days` days, plus anything up to 7 days late that isn't done."""
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    now = ensure_utc(now)
    window = repo.list_items(
        conn,
        due_after=now - OVERDUE_LOOKBACK,
        due_before=now + timedelta(days=days),
        include_undated=False,
    )
    keep = [i for i in window if listed(i, now)]
    return UpcomingWork(as_of=local_iso(now) or "", days=days, items=_items_out(conn, keep, now))


def changes(conn: sqlite3.Connection, now: datetime, since: datetime) -> Changes:
    """Items that are new, had their due date changed, or were removed after `since`."""
    now, since = ensure_utc(now), ensure_utc(since)
    events = repo.item_changes_since(conn, since)
    active = repo.active_course_titles(conn)
    items = [
        i
        for i in repo.items_by_ids(conn, sorted({e.item_id for e in events}))
        if i.course_code in active
    ]
    net = summarize_changes(events, {i.id: i.due_at for i in items})
    changed = [i for i in items if i.id in net]
    out = [
        ChangedItem(
            **base.model_dump(),
            status=net[item.id].status,
            old_due=local_iso(net[item.id].old_due),
        )
        for item, base in zip(changed, _items_out(conn, changed, now), strict=True)
    ]
    return Changes(as_of=local_iso(now) or "", since=local_iso(since) or "", changes=out)


# --- goals (D079) -----------------------------------------------------------------------------


class RulesOut(_Out):
    reteach_below: int
    practice_below: int
    review_ladder_days: list[int]
    on_plan_share: float
    on_plan_weeks: int


class ChangeOut(_Out):
    kind: Literal["none", "pace", "method", "length"]
    what: str
    why: str


class MilestoneOut(_Out):
    id: str
    title: str
    status: Literal["done", "current", "next"]
    done_when: str | None


class NextOut(_Out):
    what: str
    done_when: str | None


class SessionOut(_Out):
    what: str
    start: str = Field(description="ISO 8601, America/New_York offset")
    minutes: int
    state: Literal["scheduled", "done", "not_done", "dropped", "moved"]
    done_when: str | None


class WeekOut(_Out):
    focus: str | None
    target_minutes: int
    target_source: Literal["plan", "scheduled"]
    sessions_tracked: bool = Field(description="False when FlowSavvy isn't connected to Argus")
    done_minutes: int | None
    scheduled_minutes: int | None
    sessions_planned: int | None
    sessions_done: int | None
    sessions: list[SessionOut]
    dropped: list[SessionOut]
    moved: list[SessionOut]


class OnPlanOut(_Out):
    streak_weeks: int
    may_grow: bool
    last_share: float | None


class TopicOut(_Out):
    id: str
    title: str
    last_score: int | None
    last_method: str | None
    due: str | None = Field(description="Next review date (local, YYYY-MM-DD)")
    overdue: bool


class TopicsOut(_Out):
    reteach: list[TopicOut]
    practice: list[TopicOut]
    review_due: list[TopicOut]
    review_later: list[TopicOut]
    kept: list[TopicOut]
    next_new: list[TopicOut]


class TrendOut(_Out):
    week: str
    done_minutes: int
    target_minutes: int
    partial: bool


class GoalOut(_Out):
    tag: str
    status: Literal["active", "paused", "done"]
    aim: str
    why: str | None
    track: list[MilestoneOut]
    current: str | None
    next_milestone: str | None
    next: list[NextOut]
    week: WeekOut | None
    on_plan: OnPlanOut | None
    topics: TopicsOut
    trend: list[TrendOut]


class ProblemOut(_Out):
    path: str
    line: int | None
    field: str
    message: str


class GoalsFeed(_Out):
    as_of: str
    week: str
    week_start: str
    rules: RulesOut
    change: ChangeOut | None
    week_plan_missing: bool
    goals: list[GoalOut]
    problems: list[ProblemOut]


def _session_out(s: Session, state: str) -> SessionOut:
    return SessionOut(
        what=s.what,
        start=local_iso(s.start_at) or "",
        minutes=s.minutes,
        state=state,
        done_when=s.done_when,
    )


def _topic_out(s: TopicState) -> TopicOut:
    return TopicOut(
        id=s.topic.id,
        title=s.topic.title,
        last_score=s.last_score,
        last_method=s.last_method,
        due=s.due.isoformat() if s.due else None,
        overdue=s.overdue,
    )


def _week_out(p: WeekProgress | None, plan: WeekDoc | None, tag: str) -> WeekOut | None:
    if p is not None:
        return WeekOut(
            focus=p.focus,
            target_minutes=p.target_minutes,
            target_source=p.target_source,
            sessions_tracked=True,
            done_minutes=p.done_minutes,
            scheduled_minutes=p.scheduled_minutes,
            sessions_planned=p.sessions_planned,
            sessions_done=p.sessions_done,
            sessions=[
                _session_out(r.session, r.state)
                for r in p.rows
                if r.state in ("done", "scheduled", "not_done")
            ],
            dropped=[_session_out(s, "dropped") for s in p.dropped],
            moved=[_session_out(s, "moved") for s in p.moved],
        )
    planned = plan.goals.get(tag) if plan else None
    if planned is None:
        return None
    return WeekOut(
        focus=planned.focus,
        target_minutes=planned.target_minutes,
        target_source="plan",
        sessions_tracked=False,
        done_minutes=None,
        scheduled_minutes=None,
        sessions_planned=planned.sessions,
        sessions_done=None,
        sessions=[],
        dropped=[],
        moved=[],
    )


def goals(
    conn: sqlite3.Connection, config: AppConfig, *, sessions_on: bool, now: datetime, weeks: int = 4
) -> GoalsFeed:
    """Goals, this week's plan and progress, and what to study next, by the student's own rules.
    The facts the morning planner schedules from (D079)."""
    rules = config.goals.rules.as_rules() if config.goals else GoalRules()
    folder = config.goals.folder if config.goals else "Goals"
    goal_map, week_docs = goal_set(conn)
    this_week = week_of(now)
    monday = week_monday(this_week)
    keys = [week_key(monday - timedelta(weeks=i)) for i in reversed(range(weeks))]
    plan = week_docs.get(this_week)
    problems = [
        ProblemOut(path=d.path, line=d.error.line, field=d.error.path, message=d.error.message)
        for d in repo.goal_docs(conn)
        if d.error is not None
    ]
    if plan is not None:
        valid = {tag: g for tag, (_, g) in goal_map.items()}
        problems += [
            ProblemOut(
                path=f"{folder}/Weeks/{this_week}.md", line=e.line, field=e.path, message=e.message
            )
            for e in week_problems(plan, valid)
        ]
    sessions = (
        repo.goal_sessions_between(conn, week_bounds(keys[0])[0], week_bounds(this_week)[1])
        if sessions_on
        else []
    )
    today = to_local(now).date()
    tests = [t for w in week_docs.values() for t in w.tests if t.date <= today]
    out: list[GoalOut] = []
    for tag in order_goals(goal_map, plan):
        _, g = goal_map[tag]
        progress = (
            [week_progress(tag, k, week_docs.get(k), sessions, now) for k in keys]
            if sessions_on
            else []
        )
        states = topic_states(g, tests, today, rules)
        by_status = {
            s: [_topic_out(t) for t in states if t.status == s]
            for s in ("reteach", "practice", "review_due", "review_later", "kept")
        }
        op = None
        if sessions_on:
            history = [
                p
                for p in progress
                if p.rows or (week_docs.get(p.week) and tag in week_docs[p.week].goals)
            ]
            o = on_plan(history, rules)
            op = OnPlanOut(streak_weeks=o.streak, may_grow=o.may_grow, last_share=o.last_share)
        out.append(
            GoalOut(
                tag=tag,
                status=g.status,
                aim=g.aim,
                why=g.why,
                track=[
                    MilestoneOut(id=m.id, title=m.title, status=m.status, done_when=m.done_when)
                    for m in g.track
                ],
                current=g.current.id if g.current else None,
                next_milestone=g.next_milestone.id if g.next_milestone else None,
                next=[NextOut(what=n.what, done_when=n.done_when) for n in g.next],
                week=_week_out(progress[-1] if progress else None, plan, tag),
                on_plan=op,
                topics=TopicsOut(**by_status, next_new=[_topic_out(t) for t in next_new(states)]),
                trend=[
                    TrendOut(
                        week=p.week,
                        done_minutes=p.done_minutes,
                        target_minutes=p.target_minutes,
                        partial=p.partial,
                    )
                    for p in (trend(tag, sessions, week_docs, now, weeks) if sessions_on else ())
                ],
            )
        )
    return GoalsFeed(
        as_of=local_iso(now) or "",
        week=this_week,
        week_start=monday.isoformat(),
        rules=RulesOut(
            reteach_below=rules.reteach_below,
            practice_below=rules.practice_below,
            review_ladder_days=list(rules.review_ladder_days),
            on_plan_share=rules.on_plan_share,
            on_plan_weeks=rules.on_plan_weeks,
        ),
        change=(
            ChangeOut(kind=plan.change.kind, what=plan.change.what, why=plan.change.why)
            if plan is not None and plan.change is not None
            else None
        ),
        week_plan_missing=plan is None,
        goals=out,
        problems=problems,
    )
