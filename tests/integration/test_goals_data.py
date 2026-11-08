"""The goals page loader (D079): week 1, stale and bad files, missing plans, and the D055 split
between goals (files) and goal sessions (FlowSavvy)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import SecretStr

from argus.capabilities import Capabilities, derive
from argus.clock import local_datetime
from argus.config import AppConfig, GoalRulesConfig, GoalsConfig, PathsConfig, Secrets
from argus.domain.goals import DocError, GoalDoc, WeekDoc, parse_doc
from argus.domain.models import GoalSessionGone, GoalSessionRecord
from argus.store import repo
from argus.store.db import transaction
from argus.web.goals_data import load_goals, today_goal_line, weekly_goal_lines
from tests.conftest import REPO_ROOT

VALID = REPO_ROOT / "tests" / "fixtures" / "goals" / "valid"
NOW = local_datetime(date(2026, 11, 12), 18, 40)  # Thu, week 46
READ = local_datetime(date(2026, 11, 7), 9, 2)  # Sat


def config(tmp_path: object, **rules: object) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(data_dir=tmp_path, vault_dir=tmp_path / "vault"),  # type: ignore[operator]
        goals=GoalsConfig(rules=GoalRulesConfig.model_validate(rules)),
    )


def caps(cfg: AppConfig, *, sessions: bool = True) -> Capabilities:
    return derive(cfg, Secrets(flowsavvy_api_key=SecretStr("fs_FAKEKEY") if sessions else None))


def doc(name: str) -> GoalDoc | WeekDoc:
    rel = f"Goals/{name}"
    parsed = parse_doc(rel, (VALID / name).read_text())
    assert isinstance(parsed, (GoalDoc, WeekDoc))
    return parsed


def put(
    db: sqlite3.Connection,
    path: str,
    parsed: GoalDoc | WeekDoc | None,
    error: DocError | None = None,
    at: datetime = READ,
) -> None:
    with transaction(db):
        repo.upsert_goal_doc(
            db,
            path=path,
            kind="week" if "/Weeks/" in path else "goal",
            text_hash="h",
            parsed=parsed,
            error=error,
            now=at,
        )


def session(
    sid: str,
    day: int,
    minutes: int = 60,
    *,
    hour: int = 19,
    tag: str = "Woodwork",
    completed: bool = False,
) -> GoalSessionRecord:
    start = local_datetime(date(2026, 11, day), hour)
    return GoalSessionRecord(
        sid,
        tag,
        f"Session {sid}",
        start,
        start + timedelta(minutes=minutes),
        minutes,
        completed,
        "Done when it's done" if sid == "fri" else None,
        f"h-{sid}",
    )


def sessions(
    db: sqlite3.Connection,
    records: list[GoalSessionRecord],
    gone: list[GoalSessionGone] | None = None,
) -> None:
    with transaction(db):
        repo.apply_goal_sessions(
            db, records, gone or [], known_tags={"Woodwork", "Spanish"}, now=NOW
        )


@pytest.fixture
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    for name in ("Woodwork.md", "Spanish.md", "Chess.md"):
        put(db, f"Goals/{name}", doc(name))
    put(db, "Goals/Weeks/2026-W46.md", doc("Weeks/2026-W46.md"))
    sessions(
        db,
        [
            session("mon", 9, completed=True),
            session("tue", 10, 45),
            session("wed", 11, completed=True),
            session("fri", 13),
            session("sat", 14, 120, hour=10),
            session("es-sat", 14, 60, hour=15, tag="Spanish"),
        ],
        [GoalSessionGone("tue", "deleted")],
    )
    return db


def test_week_one_page(seeded: sqlite3.Connection, tmp_path: object) -> None:
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg), NOW)
    assert page.week_label == "Week 46, Nov 9\N{EN DASH}15"
    assert page.summary == "2 of 5 sessions done, 120 of 360 minutes."
    assert [g.tag for g in page.goals] == ["Woodwork", "Spanish"]  # the week file's order
    assert [g.tag for g in page.inactive] == ["Chess"]
    wood = page.goals[0]
    assert [(m.title, m.state) for m in wood.track] == [
        ("Hand tools and safety", "done"),
        ("Basic joints", "current"),
        ("Finishing", "next"),
    ]
    assert wood.week is not None
    assert wood.week.summary == "120 of 240 min done, 180 scheduled. 2 of 3 sessions."
    assert [(r.state_label, r.what) for r in wood.week.rows] == [
        ("Done", "Session mon"),
        ("Done", "Session wed"),
        ("Next", "Session fri"),
        ("Scheduled", "Session sat"),
    ]
    assert [r.state_label for r in wood.week.dropped] == ["Dropped"]
    assert wood.week.rows[2].time_label == "7 pm" and wood.week.rows[2].minutes_label == "60 min"
    assert wood.week.rows[3].minutes_label == "2 h"
    assert wood.week.rows[2].done_when == "Done when it's done"
    assert wood.on_plan_text == "On-plan history starts after Sunday."
    assert page.trend is None and page.topics == []
    assert page.next_new == [("Woodwork", "Shop safety"), ("Spanish", "Ser and estar")]
    assert page.change is not None and page.change.kind_label == "None"
    assert page.problems == [] and page.week_note is None


def test_next_marker_is_the_first_upcoming_session_of_all_goals(
    seeded: sqlite3.Connection, tmp_path: object
) -> None:
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg), NOW)
    nexts = [r.what for g in page.goals if g.week for r in g.week.rows if r.is_next]
    assert nexts == ["Session fri"]


def test_stale_goal_shows_good_version_and_problem(
    seeded: sqlite3.Connection, tmp_path: object
) -> None:  # Review Focus 2
    put(seeded, "Goals/Woodwork.md", None, DocError(14, "track[1].status", "must be done"), NOW)
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg), NOW)
    wood = page.goals[0]
    assert wood.stale_text == "Showing the version read Sat 9:02 am"
    (problem,) = page.problems
    assert (problem.path, problem.line, problem.field) == (
        "Goals/Woodwork.md",
        14,
        "track[1].status",
    )
    assert problem.showing_good and problem.text == "Woodwork.md has an error on line 14"


def test_never_valid_goal_is_problem_row(seeded: sqlite3.Connection, tmp_path: object) -> None:
    put(seeded, "Goals/Knitting.md", None, DocError(3, "", "YAML syntax: oops"))
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg), NOW)
    assert "Knitting" not in [g.tag for g in page.goals]
    (problem,) = page.problems
    assert not problem.showing_good and problem.message == "YAML syntax: oops"


def test_no_week_doc_uses_scheduled_minutes(seeded: sqlite3.Connection, tmp_path: object) -> None:
    repo.mark_goal_docs_missing(
        seeded, {"Goals/Woodwork.md", "Goals/Spanish.md", "Goals/Chess.md"}, NOW
    )
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg), NOW)
    wood = next(g for g in page.goals if g.tag == "Woodwork")
    assert wood.week is not None and wood.week.target_source == "scheduled"
    assert wood.week.target_minutes == 300 and wood.week.focus is None
    assert page.week_note == (
        "No plan for week 46 yet. The Saturday review writes Goals/Weeks/2026-W46.md."
    )
    assert page.change is None


def test_without_goal_sessions_feature_no_week_progress(
    seeded: sqlite3.Connection, tmp_path: object
) -> None:  # Review Focus 4
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg, sessions=False), NOW)
    wood = page.goals[0]
    assert wood.week is not None and wood.week.rows == [] and wood.week.meter is None
    assert wood.week.summary == "Target 240 min."
    assert wood.on_plan_text is None and page.trend is None and page.summary is None
    spanish = page.goals[1]
    assert spanish.week is not None and spanish.week.target_minutes == 120


def test_unknown_week_goal_key_is_problem(seeded: sqlite3.Connection, tmp_path: object) -> None:
    week = WeekDoc.model_validate(
        {"version": 1, "week": "2026-W46", "goals": {"Knitting": {"target_minutes": 60}}}
    )
    put(seeded, "Goals/Weeks/2026-W46.md", week)
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg), NOW)
    assert [(p.path, p.field, p.message) for p in page.problems] == [
        ("Goals/Weeks/2026-W46.md", "goals.Knitting", "No goal file has the tag Knitting"),
    ]


def test_moved_session_listed_as_moved_not_dropped(
    seeded: sqlite3.Connection, tmp_path: object
) -> None:
    sessions(seeded, [], [GoalSessionGone("sat", "moved")])
    cfg = config(tmp_path)
    page = load_goals(seeded, cfg, caps(cfg), NOW)
    assert page.goals[0].week is not None
    assert [r.state_label for r in page.goals[0].week.dropped] == ["Dropped", "Moved"]


def later_page(db: sqlite3.Connection, tmp_path: object, **rules: object):  # type: ignore[no-untyped-def]
    """Week 49 (Thu Dec 3) with three finished weeks and tests (the plan's later wireframe)."""
    for week, mins in (("2026-W47", 240), ("2026-W48", 120)):
        monday = date.fromisocalendar(2026, int(week[-2:]), 1)
        start = local_datetime(monday + timedelta(days=1), 19)
        sessions(
            db,
            [
                GoalSessionRecord(
                    f"w{week}",
                    "Woodwork",
                    "x",
                    start,
                    start + timedelta(minutes=mins),
                    mins,
                    True,
                    None,
                    "h",
                )
            ],
        )
        put(
            db,
            f"Goals/Weeks/{week}.md",
            WeekDoc.model_validate(
                {"version": 1, "week": week, "goals": {"Woodwork": {"target_minutes": 240}}}
            ),
        )
    w46 = doc("Weeks/2026-W46.md")
    assert isinstance(w46, WeekDoc)
    tests = [
        {"goal": "Woodwork", "topic": "safety", "date": "2026-11-21", "score": 85},
        {
            "goal": "Woodwork",
            "topic": "dovetails",
            "date": "2026-11-28",
            "score": 55,
            "method": "video",
        },
    ]
    w47 = WeekDoc.model_validate(
        {
            "version": 1,
            "week": "2026-W47",
            "goals": {"Woodwork": {"target_minutes": 240}},
            "tests": tests[:1],
        }
    )
    w48 = WeekDoc.model_validate(
        {
            "version": 1,
            "week": "2026-W48",
            "goals": {"Woodwork": {"target_minutes": 240}},
            "tests": tests[1:],
        }
    )
    put(db, "Goals/Weeks/2026-W47.md", w47)
    put(db, "Goals/Weeks/2026-W48.md", w48)
    cfg = config(tmp_path, **rules)
    return load_goals(db, cfg, caps(cfg), local_datetime(date(2026, 12, 3), 12))


def test_trends_topics_and_on_plan_once_history_exists(
    seeded: sqlite3.Connection, tmp_path: object
) -> None:
    page = later_page(seeded, tmp_path)
    assert page.trend is not None
    wood = next(r for r in page.trend.rows if r.tag == "Woodwork")
    assert [(b.week_label, b.done, b.target, b.partial) for b in wood.bars] == [
        ("W46", 120, 240, False),
        ("W47", 240, 240, False),
        ("W48", 120, 240, False),
        ("W49", 0, 0, True),
    ]
    assert page.trend.week_labels == ["W46", "W47", "W48", "W49"]
    topics = {t.title: t for t in page.topics}
    assert topics["Shop safety"].status_label == "Review overdue: due Nov 28"
    assert topics["Shop safety"].cells == ["\N{EN DASH}", "85", "\N{EN DASH}", "\N{EN DASH}"]
    assert topics["Dovetails"].status_label == "Re-teach with something other than “video”"
    w = next(g for g in page.goals if g.tag == "Woodwork")
    assert w.on_plan_text == "Below plan last week (50%), so hold time steady."


def test_custom_rules_reach_the_page(seeded: sqlite3.Connection, tmp_path: object) -> None:
    page = later_page(seeded, tmp_path, reteach_below=50, on_plan_share=0.5, on_plan_weeks=1)
    topics = {t.title: t for t in page.topics}
    assert topics["Dovetails"].status_label == "Practice more"
    w = next(g for g in page.goals if g.tag == "Woodwork")
    assert w.on_plan_text == "On plan 3 weeks running, so time can grow."


# --- the Today line and the weekly note ---------------------------------------------------------


def test_today_goal_line(seeded: sqlite3.Connection, tmp_path: object) -> None:
    cfg = config(tmp_path)
    friday = local_datetime(date(2026, 11, 13), 9)
    assert today_goal_line(seeded, caps(cfg), friday) == (
        "Goal session at 7 pm: Woodwork, Session fri (60 min)."
    )
    saturday = local_datetime(date(2026, 11, 14), 8)
    assert today_goal_line(seeded, caps(cfg), saturday) == (
        "Goal sessions today: Woodwork, Session sat (2 h); Spanish, Session es-sat (60 min)."
    )
    assert today_goal_line(seeded, caps(cfg, sessions=False), friday) is None
    assert (
        today_goal_line(seeded, caps(cfg), local_datetime(date(2026, 11, 13), 21)) is None
    )  # past


def test_today_goal_line_skips_done_sessions(seeded: sqlite3.Connection, tmp_path: object) -> None:
    cfg = config(tmp_path)
    monday = local_datetime(date(2026, 11, 9), 9)
    assert today_goal_line(seeded, caps(cfg), monday) is None  # Monday's is done


def test_weekly_goal_lines(seeded: sqlite3.Connection, tmp_path: object) -> None:
    cfg = config(tmp_path)
    lines = weekly_goal_lines(seeded, cfg, caps(cfg), date(2026, 11, 12), NOW)
    assert lines == [
        "Woodwork: Dovetails. 120 of 240 min, 2 of 3 sessions. Dropped: Session tue.",
        "Spanish: Ser and estar. 0 of 120 min, 0 of 2 sessions.",
        "Change this week: none (Baseline week).",
    ]
    assert weekly_goal_lines(seeded, cfg, caps(cfg, sessions=False), date(2026, 11, 12), NOW) == [
        "Woodwork: Dovetails. Target 240 min.",
        "Spanish: Ser and estar. Target 120 min.",
        "Change this week: none (Baseline week).",
    ]


def test_utc_now_is_fine(seeded: sqlite3.Connection, tmp_path: object) -> None:
    cfg = config(tmp_path)
    assert load_goals(seeded, cfg, caps(cfg), NOW.astimezone(UTC)).week_label.startswith("Week 46")


def test_weeks_before_a_goal_had_data_are_empty_not_zero(
    db: sqlite3.Connection, tmp_path: object
) -> None:
    put(db, "Goals/Woodwork.md", doc("Woodwork.md"))
    start = local_datetime(date(2026, 11, 24), 19)  # W48
    sessions(
        db,
        [
            GoalSessionRecord(
                "w48", "Woodwork", "x", start, start + timedelta(hours=1), 60, True, None, "h"
            )
        ],
    )
    cfg = config(tmp_path)
    page = load_goals(db, cfg, caps(cfg), local_datetime(date(2026, 12, 3), 12))
    assert page.trend is not None
    bars = page.trend.rows[0].bars
    assert [(b.week_label, b.empty) for b in bars] == [
        ("W46", True),
        ("W47", True),
        ("W48", False),
        ("W49", False),
    ]
