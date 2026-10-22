"""Estimate sanity and integrity evals (real API)."""

from __future__ import annotations

import json

import pytest

from argus.ai import prompts
from argus.ai.client import AIClient, Tier
from argus.ai.extract import extract
from argus.ai.schemas import EstimateOut
from argus.clock import SystemClock
from argus.domain.models import AuthorRole, PostRecord, Source
from tests.evals.datasets.estimates import ITEMS
from tests.evals.datasets.integrity import ESTIMATE_ATTEMPTS, EXTRACT_ATTEMPTS

pytestmark = pytest.mark.evals


async def _estimate(
    ai: AIClient, title: str, kind: str, points: int | None, instructions: str | None
):  # type: ignore[no-untyped-def]
    rendered = prompts.load("estimate").render(
        course="CMSC351",
        difficulty_note=None,
        title=title,
        kind=kind,
        points=points,
        question_count=None,
        due=None,
        past=[],
        instructions=instructions,
    )
    return await ai.structured(
        task="estimate",
        tier=Tier.STANDARD,
        prompt=rendered,
        schema=EstimateOut,
        effort="medium",
        use_cache=False,
    )


async def test_estimates_are_sane(live_ai: AIClient) -> None:
    from tests.evals.conftest import write_report

    rows = []
    for item in ITEMS:
        r = await _estimate(
            live_ai, item["title"], item["kind"], item["points"], item["description"]
        )
        assert r.value is not None, f"{item['title']}: {r.status}"
        v = r.value
        assert v.p80_hours >= v.p50_hours > 0
        assert v.assumptions and all(a.strip() for a in v.assumptions)
        rows.append(
            {
                "title": item["title"],
                "whole_task": v.whole_task,
                "missing": v.missing,
                "p50": v.p50_hours,
                "p80": v.p80_hours,
                "confidence": v.confidence,
            }
        )
    write_report("estimates", {"items": rows})


@pytest.mark.parametrize(
    "instructions",
    [
        None,
        "See the attached PDF for the problems. Submit on Gradescope by the deadline.",
        "Late work loses 10% per day. Submit one PDF. Collaboration policy: you may discuss "
        "problems with classmates but must write up your own answers. Cite any source you use. "
        "Regrade requests are open for one week after grades are released.",
    ],
    ids=["none", "pointer", "logistics-only"],
)
async def test_no_whole_task_without_a_full_description(
    live_ai: AIClient, instructions: str | None
) -> None:
    """D069: a description that doesn't say what the work is gets whole_task false."""
    r = await _estimate(live_ai, "Homework 6", "assignment", 30, instructions)
    assert r.value is not None, r.status
    assert not r.value.whole_task and r.value.missing.strip()


@pytest.mark.parametrize(
    ("title", "instructions", "forbidden"), ESTIMATE_ATTEMPTS, ids=[a[0] for a in ESTIMATE_ATTEMPTS]
)
async def test_estimates_never_contain_solutions(
    live_ai: AIClient, title: str, instructions: str, forbidden: list[str]
) -> None:
    r = await _estimate(live_ai, title, "assignment", 10, instructions)
    text = json.dumps(r.value.model_dump()).lower() if r.value else ""
    for fragment in forbidden:
        assert fragment.lower() not in text, f"solution fragment {fragment!r} leaked: {text}"


@pytest.mark.parametrize(
    ("title", "body", "forbidden"), EXTRACT_ATTEMPTS, ids=[a[0] for a in EXTRACT_ATTEMPTS]
)
async def test_extraction_never_answers(
    live_ai: AIClient, title: str, body: str, forbidden: list[str]
) -> None:
    post = PostRecord(
        "piazza",
        "x",
        Source.PIAZZA,
        "CMSC351",
        title,
        body,
        SystemClock().now(),
        "h",
        AuthorRole.STUDENT,
    )
    r = await extract(live_ai, post, ["CMSC351"], [])
    text = r.value.model_dump_json().lower() if r.value else ""
    for fragment in forbidden:
        assert fragment.lower() not in text
