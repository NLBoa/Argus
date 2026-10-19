from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from argus.clock import FakeClock
from argus.logging import redactor

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# A fixed instant used across tests: Monday 2026-10-19 09:00 America/New_York (EDT).
DEFAULT_NOW = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-evals", action="store_true", help="run AI evals against the real API")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-evals"):
        return
    skip = pytest.mark.skip(reason="evals hit the real API; run with `make evals`")
    for item in items:
        if "evals" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(DEFAULT_NOW)


@pytest.fixture(autouse=True)
def _reset_redactor() -> Iterator[None]:
    yield
    redactor.clear()
