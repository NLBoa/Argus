"""Golden record of deadline alerts over the simulated week.

Deadline alerts are a non-negotiable: the 24-hour and 3-hour warnings must keep their timing,
keys, and guards through any planner or UI change. This test replays the simulated week and
compares every DUE_24H and DUE_3H send (rule, item, due time, title, send time) with the
record in `tests/fixtures/sim/deadline_alerts.json`. Bodies are not compared: wording may
change (D056), timing may not.

To re-record (only for a deliberate, reviewed change to deadline rules):
    ARGUS_UPDATE_GOLDEN=1 uv run pytest tests/integration/test_deadline_golden.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from argus.config import AppConfig
from tests.integration.simweek import run_week

GOLDEN = Path(__file__).parents[1] / "fixtures" / "sim" / "deadline_alerts.json"
DEADLINE_RULES = ("due_24h", "due_3h")


def deadline_alerts(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        """SELECT n.rule, n.title, n.sent_at, k.key, i.title AS item, i.due_at
             FROM notifications n
             JOIN notification_keys k ON k.notification_id = n.id
             JOIN items i ON i.id = CAST(substr(k.key, 1, instr(k.key, ':') - 1) AS INTEGER)
            WHERE n.rule IN (?, ?) AND n.status = 'sent'
            ORDER BY n.sent_at, k.key""",
        DEADLINE_RULES,
    ).fetchall()
    return [
        {
            "rule": r["rule"],
            "item": r["item"],
            "due_at": r["due_at"],
            "key_due": r["key"].split(":", 2)[2],
            "title": r["title"],
            "sent_at": r["sent_at"],
        }
        for r in rows
    ]


async def test_deadline_alerts_match_golden(tmp_path: Any, app_config: AppConfig) -> None:
    conn, _ = await run_week(tmp_path, app_config)
    got = deadline_alerts(conn)
    assert got, "the simulated week should send deadline alerts"
    if os.environ.get("ARGUS_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=2) + "\n")
    assert got == json.loads(GOLDEN.read_text())
