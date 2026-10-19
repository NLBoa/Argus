"""Evals talk to the real API. They run only with `make evals` (pytest --run-evals)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from argus.ai.client import AIClient, build_transport
from argus.clock import SystemClock
from argus.config import AIConfig
from argus.store.db import open_db

REPORTS = Path(__file__).parent / "reports"


@pytest.fixture(scope="session")
def live_ai(tmp_path_factory: pytest.TempPathFactory) -> Iterator[AIClient]:
    key = os.environ.get("ARGUS_ANTHROPIC_API_KEY")
    if not key:
        pytest.fail("make evals needs ARGUS_ANTHROPIC_API_KEY (evals call the real API)")
    clock = SystemClock()
    conn = open_db(tmp_path_factory.mktemp("evals") / "evals.db", clock)
    yield AIClient(
        conn,
        AIConfig(monthly_budget_usd=float(os.environ.get("ARGUS_EVAL_BUDGET_USD", "5"))),
        clock,
        build_transport(key),
    )
    spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM ai_calls").fetchone()
    print(f"\nevals: {spent[1]} calls, ${spent[0]:.4f}")


def write_report(name: str, payload: dict[str, object]) -> Path:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{name}-{datetime.now(UTC):%Y%m%dT%H%M}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
