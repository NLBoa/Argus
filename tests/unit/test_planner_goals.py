"""Goals planner (D079): weeks, progress, on-plan streaks, the review ladder, trends. The ladder
tests are written straight from the rule (Review Focus 5), with the default numbers
(re-teach < 60, practice < 80, reviews 7 / 14 / 28 days later) unless a test says otherwise."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from argus.clock import local_datetime, to_local
from argus.domain.goals import GoalDoc, TestResult, WeekDoc
from argus.planner.goals import (
    GoalRules,
    Session,
    next_new,
    on_plan,
    session_state,
    topic_states,
    trend,
    week_bounds,
    week_of,
    week_progress,
)

R = GoalRules()

GOAL = GoalDoc.model_validate(
    {
        "version": 1,
        "tag": "Woodwork",
        "aim": "Build a bookshelf",
        "track": [{"id": "joints", "title": "Joints", "status": "current"}],
        "topics": [
            {"id": "safety", "title": "Shop safety"},
            {"id": "dovetails", "title": "Dovetails", "after": ["safety"]},
            {"id": "finishing", "title": "Finishing", "after": ["dovetails"]},
            {"id": "sharpening", "title": "Sharpening"},
        ],
    }
)


def t(
    topic: str, day: str, score: int, method: str | None = None, goal: str = "Woodwork"
) -> TestResult:
    return TestResult(
        goal=goal, topic=topic, date=date.fromisoformat(day), score=score, method=method
    )


def state_of(*results: tuple[str, int], today: date, rules: GoalRules = R, topic: str = "safety"):  # type: ignore[no-untyped-def]
    tests = [t(topic, d, s) for d, s in results]
    return next(s for s in topic_states(GOAL, tests, today, rules) if s.topic.id == topic)


# --- the review ladder (Review Focus 5) ------------------------------------------------------


def test_first_pass_due_one_step_later() -> None:
    s = state_of(("2026-11-14", 72), ("2026-11-21", 85), today=date(2026, 11, 23))
    assert (s.status, s.due, s.overdue) == ("review_due", date(2026, 11, 28), False)


def test_overdue_when_review_not_taken() -> None:
    s = state_of(("2026-11-21", 85), today=date(2026, 12, 3))
    assert (s.status, s.due, s.overdue) == ("review_due", date(2026, 11, 28), True)


def test_second_pass_counts_from_the_latest_test_even_if_late() -> None:
    s = state_of(("2026-11-14", 84), ("2026-11-28", 88), today=date(2026, 12, 3))
    assert (s.status, s.due) == ("review_later", date(2026, 12, 12))  # Nov 28 + 14


def test_third_pass_uses_the_third_step() -> None:
    s = state_of(
        ("2026-11-14", 84), ("2026-11-28", 88), ("2026-12-12", 90), today=date(2026, 12, 13)
    )
    assert s.due == date(2027, 1, 9)  # Dec 12 + 28


def test_passing_every_step_is_kept() -> None:
    s = state_of(
        ("2026-11-14", 84),
        ("2026-11-21", 88),
        ("2026-12-05", 90),
        ("2027-01-02", 91),
        today=date(2027, 1, 3),
    )
    assert (s.status, s.due) == ("kept", None)


def test_a_score_below_practice_resets_the_ladder() -> None:
    s = state_of(
        ("2026-11-14", 85), ("2026-11-21", 70), ("2026-11-28", 86), today=date(2026, 11, 29)
    )
    assert (s.status, s.due) == ("review_due", date(2026, 12, 5))  # back to step 1


def test_due_within_a_week_is_review_due_later_is_review_later() -> None:
    assert state_of(("2026-11-14", 90), today=date(2026, 11, 14)).status == "review_due"  # due 21
    s = state_of(("2026-11-14", 90), ("2026-11-21", 90), today=date(2026, 11, 21))
    assert (s.status, s.due) == ("review_later", date(2026, 12, 5))  # due in 14 > 7


def test_thresholds_are_inclusive_at_the_boundary() -> None:
    today = date(2026, 11, 15)
    for score, status in [
        (0, "reteach"),
        (59, "reteach"),
        (60, "practice"),
        (79, "practice"),
        (80, "review_due"),
        (100, "review_due"),
    ]:
        assert state_of(("2026-11-14", score), today=today).status == status, score


def test_custom_rules_change_the_outcome() -> None:  # Review Focus 7
    custom = GoalRules(reteach_below=50, practice_below=90, review_ladder_days=(3, 10))
    assert state_of(("2026-11-14", 85), today=date(2026, 11, 15), rules=custom).status == "practice"
    assert state_of(("2026-11-14", 55), today=date(2026, 11, 15), rules=custom).status == "practice"
    s = state_of(("2026-11-14", 95), today=date(2026, 11, 15), rules=custom)
    assert s.due == date(2026, 11, 17)  # + 3
    s = state_of(
        ("2026-11-14", 95),
        ("2026-11-17", 92),
        ("2026-11-27", 91),
        today=date(2026, 11, 28),
        rules=custom,
    )
    assert s.status == "kept"  # past a two-step ladder


def test_reteach_remembers_the_last_method() -> None:
    tests = [t("safety", "2026-11-14", 55, method="video")]
    s = next(x for x in topic_states(GOAL, tests, date(2026, 11, 15), R) if x.topic.id == "safety")
    assert (s.status, s.last_score, s.last_method) == ("reteach", 55, "video")


def test_tests_for_other_goals_are_ignored() -> None:
    tests = [t("safety", "2026-11-14", 90, goal="Chess")]
    s = next(x for x in topic_states(GOAL, tests, date(2026, 11, 15), R) if x.topic.id == "safety")
    assert s.status == "new" and s.history == ()


def test_same_day_tests_keep_file_order() -> None:
    tests = [t("safety", "2026-11-14", 90), t("safety", "2026-11-14", 50)]
    s = next(x for x in topic_states(GOAL, tests, date(2026, 11, 15), R) if x.topic.id == "safety")
    assert s.last_score == 50 and s.status == "reteach"


def test_prerequisites_first() -> None:
    today = date(2026, 11, 15)
    states = {s.topic.id: s.status for s in topic_states(GOAL, [], today, R)}
    assert states == {
        "safety": "new",
        "dovetails": "blocked",
        "finishing": "blocked",
        "sharpening": "new",
    }
    states = {
        s.topic.id: s.status for s in topic_states(GOAL, [t("safety", "2026-11-14", 75)], today, R)
    }
    assert states["dovetails"] == "blocked"  # 75 is below practice_below
    states = {
        s.topic.id: s.status for s in topic_states(GOAL, [t("safety", "2026-11-14", 80)], today, R)
    }
    assert states["dovetails"] == "new" and states["finishing"] == "blocked"


def test_next_new_follows_file_order() -> None:
    states = topic_states(GOAL, [t("safety", "2026-11-14", 90)], date(2026, 11, 15), R)
    assert [s.topic.id for s in next_new(states)] == ["dovetails", "sharpening"]
    assert [s.topic.id for s in next_new(states, limit=1)] == ["dovetails"]


# --- weeks ------------------------------------------------------------------------------------


def test_dst_fall_back_week_is_169_hours() -> None:  # Review Focus 1
    start, end = week_bounds("2026-W44")
    assert end - start == timedelta(hours=169)
    assert to_local(start) == datetime(2026, 10, 26, tzinfo=to_local(start).tzinfo)


def test_spring_forward_week_is_167_hours() -> None:
    start, end = week_bounds("2027-W10")  # Mar 8-14 2027; clocks spring forward Mar 14
    assert end - start == timedelta(hours=167)


def test_late_sunday_session_stays_in_its_week() -> None:
    assert week_of(local_datetime(date(2026, 11, 1), 23, 30)) == "2026-W44"
    assert week_of(local_datetime(date(2026, 11, 2), 0, 0)) == "2026-W45"


# --- sessions and progress --------------------------------------------------------------------

NOW = local_datetime(date(2026, 11, 12), 18, 40)  # Thu, week 46


def session(
    sid: str,
    day: int,
    minutes: int = 60,
    *,
    hour: int = 19,
    tag: str = "Woodwork",
    completed: bool = False,
    gone: bool = False,
    moved: bool = False,
) -> Session:
    start = local_datetime(date(2026, 11, day), hour)
    return Session(
        external_id=sid,
        tag=tag,
        what=f"Session {sid}",
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
        minutes=minutes,
        completed=completed,
        done_when=None,
        gone_at=NOW if gone else None,
        moved_at=NOW if moved else None,
    )


def test_session_states() -> None:
    assert session_state(session("a", 9, completed=True), NOW) == "done"
    assert session_state(session("b", 10), NOW) == "not_done"
    assert session_state(session("c", 13), NOW) == "scheduled"
    assert session_state(session("d", 10, gone=True), NOW) == "dropped"
    assert session_state(session("e", 13, moved=True), NOW) == "moved"
    assert session_state(session("f", 9, completed=True, gone=True), NOW) == "done"


WEEK = WeekDoc.model_validate(
    {
        "version": 1,
        "week": "2026-W46",
        "goals": {"Woodwork": {"focus": "Dovetails", "target_minutes": 300, "sessions": 4}},
    }
)
SESSIONS = [
    session("mon", 9, completed=True),
    session("tue", 10, 45, gone=True),
    session("wed", 11, completed=True),
    session("fri", 13),
    session("sat", 14, 120, hour=10),
    session("moved", 12, moved=True),
    session("other", 13, tag="Chess"),
    session("lastweek", 6, completed=True),
]


def test_week_progress_from_the_plan() -> None:
    p = week_progress("Woodwork", "2026-W46", WEEK, SESSIONS, NOW)
    assert (p.focus, p.target_minutes, p.target_source) == ("Dovetails", 300, "plan")
    assert (p.done_minutes, p.scheduled_minutes, p.not_done_minutes) == (120, 180, 0)
    assert (p.sessions_planned, p.sessions_done) == (4, 2)
    assert [r.session.external_id for r in p.rows] == ["mon", "tue", "wed", "moved", "fri", "sat"]
    assert [r.state for r in p.rows] == [
        "done",
        "dropped",
        "done",
        "moved",
        "scheduled",
        "scheduled",
    ]
    assert [s.external_id for s in p.dropped] == ["tue"]
    assert [s.external_id for s in p.moved] == ["moved"]
    assert not p.finished


def test_missing_week_doc_uses_scheduled_minutes_as_target() -> None:
    p = week_progress("Woodwork", "2026-W46", None, SESSIONS, NOW)
    assert (p.target_minutes, p.target_source, p.focus) == (300, "scheduled", None)
    assert p.sessions_planned == 4  # dropped and moved sessions are not planned any more


def test_week_doc_without_this_goal_uses_scheduled_minutes() -> None:
    p = week_progress("Chess", "2026-W46", WEEK, SESSIONS, NOW)
    assert (p.target_minutes, p.target_source) == (60, "scheduled")


def test_share_and_finished() -> None:
    later = local_datetime(date(2026, 11, 16), 9)
    p = week_progress("Woodwork", "2026-W46", WEEK, SESSIONS, later)
    assert p.finished and p.not_done_minutes == 180 and p.share == 120 / 300


def test_share_is_none_without_a_target() -> None:
    p = week_progress("Knitting", "2026-W46", None, SESSIONS, NOW)
    assert p.target_minutes == 0 and p.share is None


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=9, max_value=15),
            st.integers(min_value=0, max_value=240),
            st.booleans(),
            st.booleans(),
            st.booleans(),
        ),
        max_size=12,
    )
)
def test_done_scheduled_and_not_done_add_up(rows: list[tuple[int, int, bool, bool, bool]]) -> None:
    sessions = [
        session(f"s{i}", day, minutes, completed=done, gone=gone, moved=moved)
        for i, (day, minutes, done, gone, moved) in enumerate(rows)
    ]
    p = week_progress("Woodwork", "2026-W46", None, sessions, NOW)
    live = [s for s in sessions if session_state(s, NOW) in ("done", "scheduled", "not_done")]
    assert p.done_minutes + p.scheduled_minutes + p.not_done_minutes == sum(s.minutes for s in live)
    assert p.target_minutes == sum(s.minutes for s in live)
    assert p.sessions_done <= p.sessions_planned == len(live)


# --- on plan ----------------------------------------------------------------------------------


def progress(week: str, done: int, target: int, now: datetime) -> object:
    doc = WeekDoc.model_validate(
        {"version": 1, "week": week, "goals": {"Woodwork": {"target_minutes": target}}}
    )
    start, _ = week_bounds(week)
    s = Session(
        "x" + week,
        "Woodwork",
        "x",
        start + timedelta(hours=12),
        start + timedelta(hours=13),
        done,
        True,
        None,
        None,
        None,
    )
    return week_progress("Woodwork", week, doc, [s] if done else [], now)


def test_on_plan_needs_n_finished_weeks_at_share() -> None:
    now = local_datetime(date(2026, 11, 26), 12)  # in W48
    weeks = [
        progress("2026-W46", 250, 300, now),
        progress("2026-W47", 240, 300, now),
        progress("2026-W48", 0, 300, now),
    ]
    p = on_plan(weeks, R)  # type: ignore[arg-type]
    assert (p.streak, p.may_grow, p.last_share) == (2, True, 0.8)


def test_one_week_below_blocks_growth() -> None:
    now = local_datetime(date(2026, 11, 26), 12)
    weeks = [progress("2026-W46", 300, 300, now), progress("2026-W47", 200, 300, now)]
    p = on_plan(weeks, R)  # type: ignore[arg-type]
    assert (p.streak, p.may_grow) == (0, False)
    assert on_plan(weeks, GoalRules(on_plan_share=0.6)).streak == 2  # type: ignore[arg-type]
    assert not on_plan(weeks[:1], R).may_grow  # type: ignore[arg-type]
    assert on_plan(weeks[:1], GoalRules(on_plan_weeks=1)).may_grow  # type: ignore[arg-type]


def test_current_week_never_counts_toward_on_plan() -> None:
    now = local_datetime(date(2026, 11, 12), 12)
    p = on_plan([progress("2026-W46", 300, 300, now)], R)  # type: ignore[list-item]
    assert (p.streak, p.may_grow, p.last_share, p.finished_weeks) == (0, False, None, 0)


def test_a_week_with_no_target_is_not_on_plan() -> None:
    now = local_datetime(date(2026, 11, 26), 12)
    weeks = [progress("2026-W46", 300, 300, now), progress("2026-W47", 0, 0, now)]
    assert on_plan(weeks, R).streak == 0  # type: ignore[arg-type]


# --- trends -----------------------------------------------------------------------------------


def test_trend_starts_at_the_first_week_with_data() -> None:
    now = local_datetime(date(2026, 11, 12), 18)
    points = trend("Woodwork", SESSIONS, {"2026-W46": WEEK}, now)
    assert [(p.week, p.done_minutes, p.target_minutes, p.partial) for p in points] == [
        ("2026-W45", 60, 60, False),  # last week: one finished session, no plan
        ("2026-W46", 120, 300, True),
    ]


def test_trend_keeps_the_last_n_weeks() -> None:
    now = local_datetime(date(2026, 12, 3), 12)  # W49
    sessions = [session(f"s{d}", d, completed=True) for d in (9, 16, 23)]
    sessions.append(
        Session(
            "dec",
            "Woodwork",
            "x",
            local_datetime(date(2026, 12, 1), 9),
            local_datetime(date(2026, 12, 1), 10),
            60,
            True,
            None,
            None,
            None,
        )
    )
    points = trend("Woodwork", sessions, {}, now, n=3)
    assert [p.week for p in points] == ["2026-W47", "2026-W48", "2026-W49"]


def test_trend_is_empty_without_data() -> None:
    assert trend("Knitting", SESSIONS, {}, NOW) == ()


def test_now_must_be_utc_aware() -> None:
    p = week_progress("Woodwork", "2026-W46", WEEK, SESSIONS, NOW.astimezone(UTC))
    assert p.done_minutes == 120
