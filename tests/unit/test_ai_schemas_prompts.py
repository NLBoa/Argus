from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from argus.ai import prompts
from argus.ai.budget import PRICES, UNKNOWN_MODEL_PRICE, cost_usd, month_bounds, month_key
from argus.ai.schemas import EstimateOut, ExtractionOut, FindingOut, PlanNoteOut, strict_schema

INTEGRITY = "Argus never drafts answers or solutions to graded work."


def _walk_objects(node: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for v in node.values():
            found += _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            found += _walk_objects(v)
    return found


@pytest.mark.parametrize("model", [ExtractionOut, EstimateOut, PlanNoteOut])
def test_strict_schema_is_closed_inlined_and_plain(model: type) -> None:
    schema = strict_schema(model)
    text = str(schema)
    assert "$ref" not in text and "$defs" not in text
    for banned in ("minimum", "maxLength", "minItems", "'title'", "pattern", "'format'"):
        assert banned not in text
    for obj in _walk_objects(schema):
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_finding_date_validation() -> None:
    base = {
        "kind": "new_assessment",
        "course": "CMSC351",
        "title": "Quiz 3",
        "scope": None,
        "quote": "q",
        "confidence": 0.9,
    }
    assert FindingOut(**base, date="2026-11-05").date == "2026-11-05"
    assert FindingOut(**base, date="2026-11-05T09:30:00-05:00").date
    assert FindingOut(**base, date="").date is None
    for bad in ["Nov 5", "2026-11-05T09:30:00", "2026-13-01"]:
        with pytest.raises(ValidationError):
            FindingOut(**base, date=bad)
    with pytest.raises(ValidationError):
        FindingOut(**{**base, "confidence": 1.5}, date=None)


def test_estimate_requires_ordered_percentiles() -> None:
    good = {
        "whole_task": True,
        "missing": "",
        "p50_hours": 3,
        "p80_hours": 4.5,
        "confidence": "medium",
        "breakdown": [{"step": "read", "hours": 1}],
        "assumptions": ["a"],
        "reasoning": "r",
    }
    assert EstimateOut(**good).p80_hours == 4.5
    with pytest.raises(ValidationError, match="p80_hours must be"):
        EstimateOut(**{**good, "p80_hours": 2})
    with pytest.raises(ValidationError):
        EstimateOut(**{**good, "p50_hours": 0})
    with pytest.raises(ValidationError):
        EstimateOut(**{**good, "extra": 1})


# ---------------------------------------------------------------- prompts


def test_every_prompt_states_integrity_rule() -> None:
    all_prompts = prompts.all_prompts()
    assert {p.name for p in all_prompts} >= {"extract", "estimate", "plan_note"}
    for p in all_prompts:
        assert INTEGRITY in p.system, p.id


def test_extract_prompt_renders() -> None:
    r = prompts.load("extract").render(
        courses=["CMSC351", "MATH240"],
        course="CMSC351",
        known_items=[{"title": "Quiz 2", "kind": "quiz", "due": "Tue Oct 27"}],
        author="an instructor",
        posted_local="Sunday, October 18, 2026 at 11:00 am",
        title="Quiz 3 moved",
        body="Quiz 3 is now on Friday.",
    )
    assert r.id == "extract.v1"
    assert "Courses: CMSC351, MATH240" in r.user
    assert "- Quiz 2 (quiz, due Tue Oct 27)" in r.user
    assert "Quiz 3 is now on Friday." in r.user
    assert "{{" not in r.user and "{%" not in r.user


def test_estimate_prompt_renders_with_and_without_history() -> None:
    p = prompts.load("estimate")
    r = p.render(
        course="CMSC351",
        difficulty_note=None,
        title="HW4",
        kind="assignment",
        points=30,
        question_count=None,
        due="Sat",
        past=[],
        instructions=None,
    )
    assert "none recorded yet" in r.user and "(no instructions available)" in r.user
    r2 = p.render(
        course="CMSC351",
        difficulty_note="proofs",
        title="HW4",
        kind="assignment",
        points=None,
        question_count=5,
        due=None,
        past=[{"title": "HW2", "kind": "assignment", "actual": 3.5, "estimate": 3}],
        instructions="Prove it.",
    )
    assert (
        "student's note: proofs" in r2.user
        and "5 questions" in r2.user
        and "3.5 h actual, estimated 3 h" in r2.user
    )


def test_plan_note_renders() -> None:
    r = prompts.load("plan_note").render(facts=["3 sessions planned before Thursday"])
    assert "- 3 sessions planned before Thursday" in r.user


def test_missing_variable_fails_loudly() -> None:
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        prompts.load("plan_note").render()


def test_bad_prompt_files_rejected(tmp_path: Any) -> None:
    bad = tmp_path / "x.v1.md"
    bad.write_text("# system\nonly system")
    with pytest.raises(ValueError, match="exactly"):
        prompts._parse(bad)
    with pytest.raises(ValueError, match="bad prompt file"):
        prompts._parse(tmp_path / "notaprompt.md")
    with pytest.raises(FileNotFoundError):
        prompts.load("does_not_exist")


# ---------------------------------------------------------------- budget math


def test_cost_usd() -> None:
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 100_000, 0, 0) == pytest.approx(3.0)
    assert cost_usd("claude-haiku-5-5", 2000, 500, 10_000, 1000) == pytest.approx(
        (200 + 250 + 100 + 125) / 1e6
    )
    assert cost_usd("claude-unknown", 1_000_000, 0, 0, 0) == UNKNOWN_MODEL_PRICE[0]
    assert set(PRICES) == {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}


def test_month_bounds_are_local() -> None:
    # 02:00 UTC on Nov 1 is still October in New York.
    start, end = month_bounds(datetime(2026, 11, 1, 2, tzinfo=UTC))
    assert start == datetime(2026, 10, 1, 4, tzinfo=UTC)
    assert end == datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert month_key(datetime(2026, 11, 1, 2, tzinfo=UTC)) == "2026-10"
    _, dec_end = month_bounds(datetime(2026, 12, 15, tzinfo=UTC))
    assert dec_end == datetime(2027, 1, 1, 5, tzinfo=UTC)
