"""Goal and week files (D079): one fenced YAML block each, validated with line-numbered errors."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import yaml

from argus.domain.goals import (
    GOAL_ACCENTS,
    DocError,
    GoalDoc,
    WeekDoc,
    check_goal_set,
    extract_block,
    parse_doc,
    week_problems,
)
from argus.web.viewmodels import ACCENTS
from tests.conftest import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "goals"


def fixture(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def goal_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "tag": "Woodwork",
        "aim": "Build a bookshelf",
        "status": "active",
        "track": [
            {"id": "tools", "title": "Tools", "status": "done"},
            {"id": "joints", "title": "Joints", "status": "current"},
            {"id": "finish", "title": "Finishing", "status": "next"},
        ],
    }
    data.update(overrides)
    return data


def note(kind: str, data: dict[str, Any]) -> str:
    return f"# Note\n\n```argus-{kind}\n{yaml.safe_dump(data, sort_keys=False)}```\n"


def parse_goal(**overrides: Any) -> GoalDoc | WeekDoc | DocError | None:
    return parse_doc("Goals/Woodwork.md", note("goal", goal_data(**overrides)))


def week_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "week": "2026-W46",
        "change": {"kind": "none", "what": "Baseline week", "why": "Measure first"},
        "goals": {"Woodwork": {"focus": "Dovetails", "target_minutes": 240, "sessions": 3}},
    }
    data.update(overrides)
    return data


def parse_week(**overrides: Any) -> GoalDoc | WeekDoc | DocError | None:
    return parse_doc("Goals/Weeks/2026-W46.md", note("week", week_data(**overrides)))


# --- the block ------------------------------------------------------------------------------


def test_sample_goal_parses() -> None:
    doc = parse_doc("Goals/Woodwork.md", fixture("valid/Woodwork.md"))
    assert isinstance(doc, GoalDoc)
    assert doc.tag == "Woodwork" and doc.accent == "olive"
    assert [m.status for m in doc.track] == ["done", "current", "next"]
    assert doc.track[1].done_when == "Three clean dovetails"
    assert doc.topics[1].after == ("safety",)
    assert doc.since == date(2026, 11, 2)


def test_sample_week_parses() -> None:
    doc = parse_doc("Goals/Weeks/2026-W46.md", fixture("valid/Weeks/2026-W46.md"))
    assert isinstance(doc, WeekDoc)
    assert doc.change is not None and doc.change.kind == "none"
    assert doc.goals["Woodwork"].target_minutes == 240
    assert doc.tests[0].score == 72 and doc.tests[0].date == date(2026, 11, 14)


def test_defaults_for_optional_fields() -> None:
    doc = parse_doc("Goals/Spanish.md", fixture("valid/Spanish.md"))
    assert isinstance(doc, GoalDoc)
    assert doc.why is None and doc.accent is None and doc.next == () and doc.since is None


def test_no_block_is_ignored() -> None:
    assert parse_doc("Goals/Readme.md", "# Just notes\n\n```python\nprint(1)\n```\n") is None


def test_two_blocks_is_an_error() -> None:
    err = parse_doc("Goals/X.md", fixture("bad/bad_two_blocks.md"))
    assert isinstance(err, DocError)
    assert "only one" in err.message and err.line == 11


def test_unclosed_block_is_an_error() -> None:
    err = parse_doc("Goals/X.md", fixture("bad/bad_unclosed.md"))
    assert isinstance(err, DocError) and "never closed" in err.message and err.line == 3


def test_extract_block_reports_kind_and_first_yaml_line() -> None:
    found = extract_block(fixture("valid/Woodwork.md"))
    assert isinstance(found, tuple)
    kind, text, first_line = found
    assert kind == "goal" and first_line == 6 and text.startswith("version: 1")


def test_yaml_syntax_error_reports_vault_line() -> None:  # Review Focus 2
    err = parse_doc("Goals/X.md", fixture("bad/bad_yaml.md"))
    assert isinstance(err, DocError)
    assert err.line == 8 and "YAML" in err.message


def test_bad_status_names_field_and_line() -> None:
    err = parse_doc("Goals/X.md", fixture("bad/bad_status.md"))
    assert isinstance(err, DocError)
    assert err.path == "track[1].status" and err.line == 12
    assert "done, current or next" in err.message


def test_block_that_is_not_a_mapping() -> None:
    err = parse_doc("Goals/X.md", "```argus-goal\n- a\n- b\n```\n")
    assert isinstance(err, DocError) and "fields" in err.message and err.line == 2


# --- goal fields ----------------------------------------------------------------------------


def test_missing_field_names_it_and_points_at_the_block() -> None:
    data = goal_data()
    del data["aim"]
    err = parse_doc("Goals/W.md", note("goal", data))
    assert isinstance(err, DocError) and err.path == "aim" and "missing" in err.message
    assert err.line == 4


def test_unknown_key_is_an_error() -> None:
    err = parse_goal(color="red")
    assert (
        isinstance(err, DocError) and err.path == "color" and "isn't a known field" in err.message
    )


def test_version_must_be_1() -> None:
    err = parse_goal(version=2)
    assert isinstance(err, DocError) and err.path == "version"


@pytest.mark.parametrize("tag", ["Get good", "[Projects]", "x" * 25, "9lives", ""])
def test_bad_tag(tag: str) -> None:
    err = parse_goal(tag=tag)
    assert isinstance(err, DocError) and err.path == "tag"


def test_bad_accent() -> None:
    err = parse_goal(accent="neon")
    assert isinstance(err, DocError) and err.path == "accent"


def test_goal_accents_match_the_web_accents() -> None:
    assert GOAL_ACCENTS == ACCENTS


def test_numbers_as_text_are_accepted() -> None:
    doc = parse_goal(aim=2048)
    assert isinstance(doc, GoalDoc) and doc.aim == "2048"


@pytest.mark.parametrize(
    ("statuses", "path"),
    [
        (["current", "current"], "track[1].status"),
        (["next", "current"], "track[1].status"),
        (["done", "next"], "track"),  # active, not all done, no current
        (["current", "done"], "track[1].status"),
    ],
)
def test_track_order_and_single_current(statuses: list[str], path: str) -> None:
    track = [{"id": f"m{i}", "title": f"M{i}", "status": s} for i, s in enumerate(statuses)]
    err = parse_goal(track=track)
    assert isinstance(err, DocError) and err.path == path


def test_all_done_track_needs_no_current() -> None:
    track = [
        {"id": "a", "title": "A", "status": "done"},
        {"id": "b", "title": "B", "status": "done"},
    ]
    assert isinstance(parse_goal(track=track), GoalDoc)


def test_paused_goal_needs_no_current() -> None:
    track = [
        {"id": "a", "title": "A", "status": "done"},
        {"id": "b", "title": "B", "status": "next"},
    ]
    assert isinstance(parse_goal(status="paused", track=track), GoalDoc)


def test_duplicate_milestone_ids() -> None:
    track = [
        {"id": "a", "title": "A", "status": "current"},
        {"id": "a", "title": "B", "status": "next"},
    ]
    err = parse_goal(track=track)
    assert isinstance(err, DocError) and err.path == "track[1].id"


def test_empty_track_is_an_error() -> None:
    err = parse_goal(track=[])
    assert isinstance(err, DocError) and err.path == "track"


def test_more_than_three_next_steps_is_an_error() -> None:
    err = parse_goal(next=[{"what": f"Step {i}"} for i in range(4)])
    assert isinstance(err, DocError) and err.path == "next" and "3" in err.message


def test_long_next_step_is_an_error() -> None:
    err = parse_goal(next=[{"what": "x" * 141}])
    assert isinstance(err, DocError) and err.path == "next[0].what" and "140" in err.message


def test_topic_cycle_is_an_error() -> None:
    topics = [
        {"id": "a", "title": "A", "after": ["b"]},
        {"id": "b", "title": "B", "after": ["a"]},
    ]
    err = parse_goal(topics=topics)
    assert isinstance(err, DocError) and err.path.startswith("topics[") and "cycle" in err.message


def test_topic_after_must_exist() -> None:
    err = parse_goal(topics=[{"id": "a", "title": "A", "after": ["zz"]}])
    assert isinstance(err, DocError) and err.path == "topics[0].after" and "zz" in err.message


def test_duplicate_topic_ids() -> None:
    err = parse_goal(topics=[{"id": "a", "title": "A"}, {"id": "a", "title": "B"}])
    assert isinstance(err, DocError) and err.path == "topics[1].id"


def test_bad_date_reads_plainly() -> None:
    err = parse_goal(since="next spring")
    assert isinstance(err, DocError) and err.path == "since" and "date" in err.message


def test_unquoted_yes_reads_as_text_error() -> None:
    err = parse_doc(
        "Goals/W.md", note("goal", goal_data()).replace("aim: Build a bookshelf", "aim: yes")
    )
    assert isinstance(err, DocError) and err.path == "aim" and "quotes" in err.message


# --- week files -----------------------------------------------------------------------------


def test_week_must_match_file_name() -> None:
    err = parse_doc("Goals/Weeks/2026-W47.md", fixture("valid/Weeks/2026-W46.md"))
    assert isinstance(err, DocError) and err.path == "week" and "2026-W47" in err.message


@pytest.mark.parametrize("week", ["2026-46", "2026-W54", "2027-W53", "W46"])
def test_bad_week_key(week: str) -> None:
    err = parse_doc(f"Goals/Weeks/{week}.md", note("week", week_data(week=week)))
    assert isinstance(err, DocError) and err.path == "week"


def test_week_53_exists_when_the_year_has_one() -> None:  # 2027 has no W53; 2020 did
    doc = parse_doc("Goals/Weeks/2020-W53.md", note("week", week_data(week="2020-W53")))
    assert isinstance(doc, WeekDoc)


def test_test_date_outside_week_is_an_error() -> None:
    tests = [{"goal": "Woodwork", "topic": "dovetails", "date": "2026-11-16", "score": 90}]
    err = parse_week(tests=tests)
    assert isinstance(err, DocError) and err.path == "tests[0].date" and "Nov 9" in err.message


@pytest.mark.parametrize("score", [101, -1, 72.5, "high"])
def test_bad_score(score: object) -> None:
    tests = [{"goal": "Woodwork", "topic": "dovetails", "date": "2026-11-14", "score": score}]
    err = parse_week(tests=tests)
    assert isinstance(err, DocError) and err.path == "tests[0].score"


def test_bad_change_kind() -> None:
    err = parse_week(change={"kind": "everything", "what": "x", "why": "y"})
    assert isinstance(err, DocError) and err.path == "change.kind"
    assert "none, pace, method or length" in err.message


def test_change_needs_a_why() -> None:
    err = parse_week(change={"kind": "pace", "what": "Faster"})
    assert isinstance(err, DocError) and err.path == "change.why"


def test_target_minutes_bounds() -> None:
    err = parse_week(goals={"Woodwork": {"target_minutes": 2000}})
    assert isinstance(err, DocError) and err.path == "goals.Woodwork.target_minutes"


def test_week_without_change_or_goals_is_valid() -> None:
    doc = parse_doc("Goals/Weeks/2026-W46.md", note("week", {"version": 1, "week": "2026-W46"}))
    assert isinstance(doc, WeekDoc) and doc.change is None and doc.goals == {} and doc.tests == ()


# --- across files ---------------------------------------------------------------------------


def _goal(tag: str) -> GoalDoc:
    doc = parse_doc(f"Goals/{tag}.md", note("goal", goal_data(tag=tag)))
    assert isinstance(doc, GoalDoc)
    return doc


def test_duplicate_tags_flag_the_later_file() -> None:
    problems = check_goal_set({"Goals/B.md": _goal("Woodwork"), "Goals/A.md": _goal("Woodwork")})
    assert list(problems) == ["Goals/B.md"]
    assert problems["Goals/B.md"].path == "tag" and "Goals/A.md" in problems["Goals/B.md"].message


def test_unique_tags_have_no_problems() -> None:
    assert check_goal_set({"Goals/A.md": _goal("Woodwork"), "Goals/B.md": _goal("Chess")}) == {}


def test_week_problems_name_unknown_goals_and_topics() -> None:
    week = parse_week(
        goals={"Woodwork": {"target_minutes": 60}, "Knitting": {"target_minutes": 30}},
        tests=[
            {"goal": "Woodwork", "topic": "nope", "date": "2026-11-14", "score": 70},
            {"goal": "Knitting", "topic": "purl", "date": "2026-11-14", "score": 70},
        ],
    )
    assert isinstance(week, WeekDoc)
    wood = parse_goal(topics=[{"id": "dovetails", "title": "Dovetails"}])
    assert isinstance(wood, GoalDoc)
    problems = week_problems(week, {"Woodwork": wood})
    assert [(p.path, p.message) for p in problems] == [
        ("goals.Knitting", "No goal file has the tag Knitting"),
        ("tests[0].topic", "Woodwork has no topic nope"),
        ("tests[1].goal", "No goal file has the tag Knitting"),
    ]
