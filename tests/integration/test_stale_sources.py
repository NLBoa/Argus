"""Stored data from a source that is no longer set up stops acting, not just showing (D055).

The demo is seeded with every source, then run with Piazza and the ELMS token removed: its
posts are not scanned by the AI (nothing they say may change a date) and their findings no
longer produce "Announced" texts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from argus.ai.client import AIClient
from argus.announcements import scan_posts
from argus.capabilities import derive
from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.notify.router import Router
from argus.reminders.engine import ReminderEngine
from argus.store import repo
from argus.store.db import open_db
from tests.ai_fakes import FakeTransport

NO_POSTS = demo_secrets(DemoProfile.NO_CANVAS_TOKEN).model_copy(
    update={"piazza_email": None, "piazza_password": None}
)


def seeded(tmp_path: Path, clock: FakeClock) -> sqlite3.Connection:
    conn = open_db(tmp_path / "stale.db", clock)
    seed_demo(conn, clock.now(), DemoProfile.FULL)
    return conn


async def test_posts_from_a_removed_source_are_not_scanned(
    tmp_path: Path, clock: FakeClock
) -> None:
    conn = seeded(tmp_path, clock)
    waiting = len(repo.posts_to_scan(conn, 500))
    assert waiting > 0  # the demo leaves some staff posts unscanned
    transport = FakeTransport([])  # any call would fail the test: no responses queued
    ai = AIClient(conn, DEMO_CONFIG.ai, clock, transport)
    report = await scan_posts(conn, ai, DEMO_CONFIG, clock, caps=derive(DEMO_CONFIG, NO_POSTS))
    assert report.scanned == 0 and transport.calls == []
    assert len(repo.posts_to_scan(conn, 500)) == waiting  # kept for when the source returns


def test_findings_from_a_removed_source_send_no_announced_texts(
    tmp_path: Path, clock: FakeClock
) -> None:
    conn = seeded(tmp_path, clock)
    full = ReminderEngine(
        conn, DEMO_CONFIG, clock, Router([]), caps=derive(DEMO_CONFIG, demo_secrets())
    )
    gone = ReminderEngine(conn, DEMO_CONFIG, clock, Router([]), caps=derive(DEMO_CONFIG, NO_POSTS))
    assert full.snapshot(clock.now()).announced  # the demo's Piazza findings
    assert gone.snapshot(clock.now()).announced == []
