"""Goals ("get good at X", D079): week progress, on-plan streaks, the topic review ladder, and
trends. Pure and deterministic. The rule numbers come from `goals.rules` in config.yaml.

Weeks are ISO weeks, Monday 00:00 to the next Monday 00:00 in America/New_York, so a week with a
DST change is 167 or 169 hours long.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from argus.clock import local_datetime, to_local
from argus.domain.goals import GoalDoc, TestResult, Topic, WeekDoc, week_key, week_monday

__all__ = [
    "GoalRules",
    "OnPlan",
    "Session",
    "SessionRow",
    "TopicState",
    "TrendPoint",
    "WeekProgress",
    "next_new",
    "on_plan",
    "session_state",
    "topic_states",
    "trend",
    "week_bounds",
    "week_key",
    "week_of",
    "week_progress",
]

# A review is "due" (worth scheduling) when it falls within this many days.
REVIEW_HORIZON_DAYS = 7


@dataclass(frozen=True, slots=True)
class GoalRules:
    """The tunable numbers behind the goal rules (D079). Defaults are the student's first draft."""

    reteach_below: int = 60  # latest score below this: re-teach with a different method
    practice_below: int = 80  # below this: more practice; at or above: the review ladder
    review_ladder_days: tuple[int, ...] = (7, 14, 28)
    on_plan_share: float = 0.8  # a week is on plan when done >= share x target
    on_plan_weeks: int = 2  # finished on-plan weeks in a row before time may grow


# --- weeks ------------------------------------------------------------------------------------


def week_bounds(key: str) -> tuple[datetime, datetime]:
    """UTC start and end of an ISO week: local Monday 00:00 to the next Monday 00:00."""
    monday = week_monday(key)
    return local_datetime(monday, 0), local_datetime(monday + timedelta(days=7), 0)


def week_of(at: datetime) -> str:
    """The ISO week a UTC instant falls in, by its local date."""
    return week_key(to_local(at).date())


# --- sessions ---------------------------------------------------------------------------------

SessionState = Literal["scheduled", "done", "not_done", "dropped", "moved"]


@dataclass(frozen=True, slots=True)
class Session:
    """A stored FlowSavvy goal session."""

    external_id: str
    tag: str
    what: str
    start_at: datetime
    end_at: datetime
    minutes: int
    completed: bool
    done_when: str | None
    gone_at: datetime | None  # FlowSavvy confirmed it was deleted or cancelled before it was done
    moved_at: datetime | None  # still exists, but left the schedule window not done


def session_state(s: Session, now: datetime) -> SessionState:
    if s.completed:
        return "done"
    if s.gone_at is not None:
        return "dropped"
    if s.moved_at is not None:
        return "moved"
    return "not_done" if s.end_at <= now else "scheduled"


@dataclass(frozen=True, slots=True)
class SessionRow:
    session: Session
    state: SessionState


@dataclass(frozen=True, slots=True)
class WeekProgress:
    tag: str
    week: str
    focus: str | None
    target_minutes: int
    target_source: Literal["plan", "scheduled"]  # the week file's target, or what was scheduled
    done_minutes: int
    scheduled_minutes: int  # still to come
    not_done_minutes: int  # past, and not marked done
    sessions_planned: int
    sessions_done: int
    rows: tuple[SessionRow, ...]  # every session that started in the week, by start time
    finished: bool

    @property
    def share(self) -> float | None:
        return self.done_minutes / self.target_minutes if self.target_minutes else None

    @property
    def dropped(self) -> tuple[Session, ...]:
        return tuple(r.session for r in self.rows if r.state == "dropped")

    @property
    def moved(self) -> tuple[Session, ...]:
        return tuple(r.session for r in self.rows if r.state == "moved")


_LIVE = ("done", "scheduled", "not_done")


def week_progress(
    tag: str, week: str, plan: WeekDoc | None, sessions: Sequence[Session], now: datetime
) -> WeekProgress:
    start, end = week_bounds(week)
    mine = sorted(
        (s for s in sessions if s.tag == tag and start <= s.start_at < end),
        key=lambda s: (s.start_at, s.external_id),
    )
    rows = tuple(SessionRow(s, session_state(s, now)) for s in mine)

    def minutes(*states: str) -> int:
        return sum(r.session.minutes for r in rows if r.state in states)

    live = [r for r in rows if r.state in _LIVE]
    goal = plan.goals.get(tag) if plan is not None else None
    return WeekProgress(
        tag=tag,
        week=week,
        focus=goal.focus if goal is not None else None,
        target_minutes=goal.target_minutes if goal is not None else minutes(*_LIVE),
        target_source="plan" if goal is not None else "scheduled",
        done_minutes=minutes("done"),
        scheduled_minutes=minutes("scheduled"),
        not_done_minutes=minutes("not_done"),
        sessions_planned=(
            goal.sessions if goal is not None and goal.sessions is not None else len(live)
        ),
        sessions_done=sum(1 for r in rows if r.state == "done"),
        rows=rows,
        finished=end <= now,
    )


# --- on plan ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnPlan:
    streak: int  # finished weeks on plan in a row, most recent first
    may_grow: bool  # the last `on_plan_weeks` finished weeks were all on plan
    last_share: float | None  # done / target in the most recent finished week
    finished_weeks: int


def on_plan(history: Sequence[WeekProgress], rules: GoalRules) -> OnPlan:
    """A week is on plan when it's finished and done >= share x target (a week with no target
    is not on plan). The current, unfinished week never counts."""
    finished = sorted((p for p in history if p.finished), key=lambda p: p.week)
    streak = 0
    for p in reversed(finished):
        share = p.share
        if share is None or share < rules.on_plan_share:
            break
        streak += 1
    return OnPlan(
        streak=streak,
        may_grow=streak >= rules.on_plan_weeks,
        last_share=finished[-1].share if finished else None,
        finished_weeks=len(finished),
    )


# --- topics and the review ladder -------------------------------------------------------------

TopicStatus = Literal["new", "blocked", "reteach", "practice", "review_due", "review_later", "kept"]


@dataclass(frozen=True, slots=True)
class TopicState:
    goal: str
    topic: Topic
    status: TopicStatus
    due: date | None  # the next review, on the ladder
    overdue: bool
    step: int  # passing results in a row (0 off the ladder)
    last_score: int | None
    last_method: str | None
    history: tuple[TestResult, ...]  # oldest first


def _tested(
    goal: GoalDoc, topic: Topic, history: tuple[TestResult, ...], today: date, rules: GoalRules
) -> TopicState:
    last = history[-1]
    status: TopicStatus
    due: date | None = None
    step = 0
    if last.score < rules.reteach_below:
        status = "reteach"
    elif last.score < rules.practice_below:
        status = "practice"
    else:
        for r in reversed(history):
            if r.score < rules.practice_below:
                break
            step += 1
        ladder = rules.review_ladder_days
        if step > len(ladder):
            status = "kept"
        else:
            due = last.date + timedelta(days=ladder[step - 1])
            horizon = today + timedelta(days=REVIEW_HORIZON_DAYS)
            status = "review_due" if due <= horizon else "review_later"
    return TopicState(
        goal=goal.tag,
        topic=topic,
        status=status,
        due=due,
        overdue=due is not None and due < today,
        step=step,
        last_score=last.score,
        last_method=last.method,
        history=history,
    )


def topic_states(
    goal: GoalDoc, tests: Sequence[TestResult], today: date, rules: GoalRules
) -> list[TopicState]:
    """Each topic's state, in file order. Untested topics are `new` once every prerequisite's
    latest score reached `practice_below`, else `blocked`."""
    mine = [r for r in tests if r.goal == goal.tag]
    histories = {
        t.id: tuple(sorted((r for r in mine if r.topic == t.id), key=lambda r: r.date))
        for t in goal.topics
    }
    passed = {tid: bool(h) and h[-1].score >= rules.practice_below for tid, h in histories.items()}
    out: list[TopicState] = []
    for t in goal.topics:
        history = histories[t.id]
        if history:
            out.append(_tested(goal, t, history, today, rules))
            continue
        ready = all(passed.get(a, False) for a in t.after)
        out.append(
            TopicState(
                goal=goal.tag,
                topic=t,
                status="new" if ready else "blocked",
                due=None,
                overdue=False,
                step=0,
                last_score=None,
                last_method=None,
                history=(),
            )
        )
    return out


def next_new(states: Sequence[TopicState], limit: int = 2) -> list[TopicState]:
    """The first new topics whose prerequisites are met, in file order."""
    return [s for s in states if s.status == "new"][:limit]


# --- trends -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrendPoint:
    week: str
    done_minutes: int
    target_minutes: int
    partial: bool  # the week isn't over


def trend(
    tag: str,
    sessions: Sequence[Session],
    plans: Mapping[str, WeekDoc],
    now: datetime,
    n: int = 4,
) -> tuple[TrendPoint, ...]:
    """The last `n` weeks up to this one, starting at the first week that has data (a session or
    a target for this goal), so week 1 shows one bar instead of three empty ones."""
    this_monday = week_monday(week_of(now))
    weeks = [week_key(this_monday - timedelta(weeks=i)) for i in reversed(range(n))]
    points: list[TrendPoint] = []
    for week in weeks:
        plan = plans.get(week)
        p = week_progress(tag, week, plan, sessions, now)
        has_data = bool(p.rows) or (plan is not None and tag in plan.goals)
        if not points and not has_data:
            continue
        points.append(TrendPoint(week, p.done_minutes, p.target_minutes, not p.finished))
    return tuple(points)
