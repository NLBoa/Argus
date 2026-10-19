"""Verifier eval (real API): the strong-tier verifier must catch seeded errors in study notes
without rejecting correct content. Gates: catch >= 90% of seeded errors, false rejects <= 10%."""

from __future__ import annotations

import pytest

from argus.ai.client import AIClient
from argus.ai.schemas import ConceptDraft
from argus.study import Source, check
from tests.evals.datasets.verification import CASES
from tests.evals.scoring import score_verification

pytestmark = pytest.mark.evals


async def test_verifier_catches_seeded_errors(live_ai: AIClient) -> None:
    from tests.evals.conftest import write_report

    results = []
    for case in CASES:
        sources = [
            Source(s["id"], s["title"], s["ref"], s["text"], "https://example.test")
            for s in case["sources"]
        ]
        out = await check(
            live_ai, ConceptDraft.model_validate(case["note"]), sources, use_cache=False
        )
        results.append((case, out))
    report = score_verification(results)
    path = write_report("verifier", report.summary())
    print(f"\nverifier eval report: {path}")
    failed = [name for name, ok in report.gates().items() if not ok]
    assert not failed, f"gates failed: {failed}; see {path}"
