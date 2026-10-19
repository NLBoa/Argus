"""Live smoke test against the real sources. Opt-in only:

    ARGUS_LIVE=1 ARGUS_LIVE_CONFIG=config.yaml uv run pytest tests/integration/test_live_smoke.py -s

It reads credentials from the environment as production does, writes to a temporary
database, and prints per-source results without printing any item content.
"""

import os
from pathlib import Path

import pytest

from argus.clock import SystemClock
from argus.collectors.base import RunStatus
from argus.collectors.http import Fetcher, make_client
from argus.collectors.runner import SyncRunner
from argus.config import load_config, load_secrets
from argus.logging import redactor
from argus.store.db import open_db

pytestmark = pytest.mark.skipif(
    os.environ.get("ARGUS_LIVE") != "1",
    reason="live smoke test: set ARGUS_LIVE=1 (needs credentials)",
)


async def test_live_cycle(tmp_path: Path) -> None:
    config = load_config(Path(os.environ.get("ARGUS_LIVE_CONFIG", "config.yaml")))
    secrets = load_secrets()
    redactor.register(secrets.secret_values())
    clock = SystemClock()
    conn = open_db(tmp_path / "live.db", clock)
    async with make_client() as client:
        report = await SyncRunner(conn, config, secrets, clock, Fetcher(client)).run_cycle(
            force=True
        )
    for r in report.results:
        print(f"{r.source.value:<13} {r.status.value:<10} {r.record_count:>4}  {r.error or ''}")
    assert report.results, "no sources enabled: check credentials"
    assert all(r.status in (RunStatus.OK, RunStatus.UNCHANGED) for r in report.results)
