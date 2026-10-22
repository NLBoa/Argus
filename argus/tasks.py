"""Tasks the student adds by hand: studying, reading, anything no source lists (D070).

A task is a `manual` source record, so it merges into an item, ranks in the queue, gets deadline
reminders, and lands in the vault like any other. Syncs never mark it absent: only the student
removes it. Its external id hashes course, title, and due time, so a double submit adds it once.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from argus import estimates
from argus.clock import ensure_utc
from argus.domain.models import Item, ItemKind, Source, SourceItem
from argus.store import repo
from argus.store.db import iso, transaction

MAX_TITLE = 200
MAX_NOTES = 4000


def add_task(
    conn: sqlite3.Connection,
    *,
    course_code: str,
    title: str,
    due_at: datetime,
    now: datetime,
    notes: str = "",
    hours: float | None = None,
) -> Item:
    """Add a task (or return the identical one already there). Raises ValueError, worded for
    the student, on bad input."""
    title = " ".join(title.split())
    notes = notes.strip()
    if not title:
        raise ValueError("Give the task a name.")
    if len(title) > MAX_TITLE:
        raise ValueError(f"Keep the name under {MAX_TITLE} characters.")
    if len(notes) > MAX_NOTES:
        raise ValueError(f"Keep the notes under {MAX_NOTES} characters.")
    if hours is not None and not 0 < hours <= 200:
        raise ValueError("hours must be between 0 and 200")
    ids = repo.course_ids(conn)
    if course_code not in ids:
        raise ValueError("Choose one of your courses.")
    due = iso(ensure_utc(due_at))
    payload = json.dumps([course_code, title, due, notes])
    record = SourceItem(
        source=Source.MANUAL,
        external_id=repo.sha256(json.dumps([course_code, title.casefold(), due]))[:16],
        course_code=course_code,
        title=title,
        kind=ItemKind.ASSIGNMENT,
        due_at=ensure_utc(due_at).replace(microsecond=0),
        raw_hash=repo.sha256(payload),
        description=notes or None,
    )
    with transaction(conn):
        repo.upsert_source_items(conn, [record], ids, now)
        repo.apply_merge(conn, ids, now)
        item_id = repo.item_for_source(conn, Source.MANUAL, record.external_id)
        assert item_id is not None  # apply_merge links every present record
        repo.rebuild_search_index(conn)
    item = repo.get_item(conn, item_id)
    assert item is not None
    if hours is not None:
        estimates.set_override(conn, item, hours, now)
    return item


def remove_task(conn: sqlite3.Connection, item_id: int, now: datetime) -> bool:
    """Remove a task the student added. False if there is no such live task (synced items
    can't be removed here: their source would only bring them back)."""
    item = repo.get_item(conn, item_id)
    if item is None or not item.added_by_you:
        return False
    with transaction(conn):
        conn.execute(
            "UPDATE item_sources SET present = 0, changed_at = ? WHERE item_id = ? AND source = ?",
            (iso(now), item_id, Source.MANUAL.value),
        )
        repo.apply_merge(conn, repo.course_ids(conn), now)
        repo.rebuild_search_index(conn)
    return True
