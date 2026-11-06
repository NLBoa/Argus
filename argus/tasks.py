"""Tasks the student adds by hand: studying, reading, anything no source lists (D070).

A task is a `manual` source record, so it merges into an item, ranks in the queue, gets deadline
reminders, and lands in the vault like any other. Syncs never mark it absent: only the student
removes it. Its external id hashes course, title, and due time, so a double submit adds it once.
An exam the sources don't list is added the same way with `kind=EXAM` (D075), so it gets exam
study time in the work windows before it. Either can be edited later (D076): the record is
rewritten in place, so the item keeps its id and everything hung on it.
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
KINDS = (ItemKind.ASSIGNMENT, ItemKind.EXAM)


def _noun(kind: ItemKind) -> str:
    return "exam" if kind is ItemKind.EXAM else "task"


def _record(
    ids: dict[str, int],
    *,
    course_code: str,
    title: str,
    due_at: datetime,
    notes: str,
    hours: float | None,
    kind: ItemKind,
) -> SourceItem:
    """The manual record for what the student typed. Raises ValueError, worded for them."""
    title = " ".join(title.split())
    notes = notes.strip()
    if kind not in KINDS:
        raise ValueError(f"Can't add an item of kind {kind.value}.")
    if not title:
        raise ValueError(f"Give the {_noun(kind)} a name.")
    if len(title) > MAX_TITLE:
        raise ValueError(f"Keep the name under {MAX_TITLE} characters.")
    if len(notes) > MAX_NOTES:
        raise ValueError(f"Keep the notes under {MAX_NOTES} characters.")
    if hours is not None and not 0 < hours <= 200:
        raise ValueError("hours must be between 0 and 200")
    if course_code not in ids:
        raise ValueError("Choose one of your courses.")
    due = iso(ensure_utc(due_at))
    payload = json.dumps([course_code, title, due, notes])
    # Tasks keep their pre-D075 ids; other kinds add the kind, so a same-named task stays apart.
    key = [course_code, title.casefold(), due] + (
        [] if kind is ItemKind.ASSIGNMENT else [kind.value]
    )
    return SourceItem(
        source=Source.MANUAL,
        external_id=repo.sha256(json.dumps(key))[:16],
        course_code=course_code,
        title=title,
        kind=kind,
        due_at=ensure_utc(due_at).replace(microsecond=0),
        raw_hash=repo.sha256(payload),
        description=notes or None,
    )


def add_task(
    conn: sqlite3.Connection,
    *,
    course_code: str,
    title: str,
    due_at: datetime,
    now: datetime,
    notes: str = "",
    hours: float | None = None,
    kind: ItemKind = ItemKind.ASSIGNMENT,
) -> Item:
    """Add a task or an exam (or return the identical one already there). Raises ValueError,
    worded for the student, on bad input."""
    ids = repo.course_ids(conn)
    record = _record(
        ids,
        course_code=course_code,
        title=title,
        due_at=due_at,
        notes=notes,
        hours=hours,
        kind=kind,
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


def edit_task(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    course_code: str,
    title: str,
    due_at: datetime,
    now: datetime,
    notes: str = "",
    hours: float | None = None,
) -> Item | None:
    """Change a task or exam the student added (D076): name, course, due time, notes, hours.

    None if there is no such live item of theirs. The manual record is rewritten in place, so the
    item keeps its id (time logs, estimates, queue choices, sent reminders, its vault note). Its
    external id is recomputed from the new details, as `add_task` would, so adding the old
    details again makes a new item and adding the new ones is a no-op. Hours set the student's
    estimate (a new version only when it differs); blank retracts it. Raises ValueError, worded
    for the student, on bad input, and changes nothing then."""
    item = repo.get_item(conn, item_id)
    if item is None or not item.added_by_you or item.kind not in KINDS:
        return None
    ids = repo.course_ids(conn)
    record = _record(
        ids,
        course_code=course_code,
        title=title,
        due_at=due_at,
        notes=notes,
        hours=hours,
        kind=item.kind,
    )
    with transaction(conn):
        row = conn.execute(
            "SELECT id, external_id FROM item_sources"
            " WHERE item_id = ? AND source = ? AND present = 1 ORDER BY id LIMIT 1",
            (item_id, Source.MANUAL.value),
        ).fetchone()
        assert row is not None  # added_by_you: a present manual record links here
        if row["external_id"] != record.external_id:
            _take_key(conn, row["id"], record.external_id, item.kind)
        repo.upsert_source_items(conn, [record], ids, now)
        repo.apply_merge(conn, ids, now)
        repo.rebuild_search_index(conn)
        edited = repo.get_item(conn, item_id)
        assert edited is not None
        current = your_hours(conn, item_id)
        if hours is None:
            if current is not None:
                estimates.clear_override(conn, item_id, now)
        elif current != hours:
            estimates.set_override(conn, edited, hours, now)
    return edited


def your_hours(conn: sqlite3.Connection, item_id: int) -> float | None:
    """The student's own estimate in force (the latest, unless retracted), or None."""
    yours = [e for e in repo.estimates_for(conn, item_id) if e.source == "user"]
    return yours[-1].p50 if yours and yours[-1].retracted_at is None else None


def _take_key(conn: sqlite3.Connection, source_id: int, key: str, kind: ItemKind) -> None:
    """Give the record at `source_id` the external id `key`. Another live record with that key
    is the same item added twice: refused. A deleted one's leftover record is dropped (its item
    stays removed), since only one record may hold a key."""
    clash = conn.execute(
        "SELECT id, present FROM item_sources WHERE source = ? AND external_id = ?",
        (Source.MANUAL.value, key),
    ).fetchone()
    if clash is not None and clash["present"]:
        raise ValueError(f"You already added this {_noun(kind)}.")
    if clash is not None:
        conn.execute("DELETE FROM item_sources WHERE id = ?", (clash["id"],))
    conn.execute("UPDATE item_sources SET external_id = ? WHERE id = ?", (key, source_id))


def remove_task(conn: sqlite3.Connection, item_id: int, now: datetime) -> bool:
    """Remove a task or exam the student added. False if there is no such live task (synced items
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
