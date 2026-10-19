"""Extraction eval gates: zero invented dates, recall >= 95%, every date change handled."""

from __future__ import annotations

from datetime import datetime

import pytest

from argus.ai.client import AIClient
from argus.ai.extract import extract
from argus.domain.findings import ItemRef, PostContext, decide, match_item, normalize
from argus.domain.models import AuthorRole, ItemKind, PostRecord, Source
from tests.evals.datasets.extraction import POSTS
from tests.evals.scoring import PostResult, score

pytestmark = pytest.mark.evals
COURSES = ["CMSC351", "MATH240", "ENGL393"]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def run_post(ai: AIClient, p: dict) -> PostResult:  # type: ignore[type-arg]
    known = [
        ItemRef(i + 1, p["course"], k["title"], ItemKind(k["kind"]), _dt(k["due"]), True)
        for i, k in enumerate(p["known"])
    ]
    record = PostRecord(
        "piazza",
        p["id"],
        Source.PIAZZA,
        p["course"],
        p["title"],
        p["body"],
        _dt(p["posted"]),
        "h",
        AuthorRole(p["role"]),
    )
    result = await extract(ai, record, COURSES, known)
    ctx = PostContext(
        p["course"], AuthorRole(p["role"]), False, _dt(p["posted"]), p["title"], p["body"]
    )
    findings, actions = [], []
    for out in result.value.findings if result.value else []:
        f = normalize(out, ctx)
        if f is not None:
            findings.append(f)
            actions.append(decide(f, ctx, match_item(f, known)).action)
    return PostResult(p, findings, actions)


async def test_extraction_gates(live_ai: AIClient) -> None:
    from tests.evals.conftest import write_report

    results = [await run_post(live_ai, p) for p in POSTS]
    report = score(results)
    path = write_report("extraction", report.summary())
    print(f"\nextraction eval: {report.summary()}\nreport: {path}")
    assert report.gates() == dict.fromkeys(report.gates(), True), report.summary()
