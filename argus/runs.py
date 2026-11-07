"""Runna runs (D078): Runna's length for each run becomes its estimate.

The estimate is a versioned row with source `runna` (p50 = p80 = the length). A new version is
appended only when the length changes; the student's own estimate still wins, and runs are never
sent to the AI or calibrated (`estimates.py`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from argus.config import RUN_CODE, AppConfig
from argus.store import repo

FALLBACK_NOTE = "Runna gave no length; using the default"


def sync_run_estimates(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> int:
    """Append a `runna` estimate for each open run whose length is new. Returns how many."""
    if config.runna is None:
        return 0
    runs = [i for i in repo.list_items(conn, open_only=True) if i.course_code == RUN_CODE]
    details = repo.run_details(conn, [i.id for i in runs])
    latest = repo.latest_runna_estimates(conn, [i.id for i in runs])
    added = 0
    for item in runs:
        detail = details.get(item.id)
        minutes = detail.duration_minutes if detail is not None else None
        fallback = minutes is None
        if minutes is None:
            minutes = config.runna.default_minutes
        key = f"runna-default:{minutes}" if fallback else f"runna:{minutes}"
        if latest.get(item.id) == key:
            continue
        repo.insert_estimate(
            conn,
            item_id=item.id,
            source="runna",
            p50=minutes / 60,
            p80=minutes / 60,
            input_hash=key,
            now=now,
            confidence="low" if fallback else "high",
            assumptions=(FALLBACK_NOTE,) if fallback else (),
        )
        added += 1
    return added
