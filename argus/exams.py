"""Exams: scope, concepts in scope, readiness from practice attempts, and practice."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.planner.review import Attempt, Readiness, ReviewConcept, readiness
from argus.store.db import iso, parse_iso, transaction

RESULTS = ("correct", "shaky", "incorrect")


def sync_exams(conn: sqlite3.Connection, now: datetime) -> int:
    """Keep an `exams` row for every upcoming exam: scope and logistics from announcements,
    and every verified concept of its course in scope (until scope mapping says otherwise)."""
    rows = conn.execute(
        """SELECT i.id, i.course_id, i.scope, i.logistics FROM items i
           WHERE i.kind = 'exam' AND i.removed_at IS NULL AND (i.due_at IS NULL OR i.due_at >= ?)""",
        (iso(now - timedelta(days=1)),),
    ).fetchall()
    with transaction(conn):
        for r in rows:
            concept_ids = [
                c["id"]
                for c in conn.execute(
                    "SELECT id FROM concepts WHERE course_id = ? AND verified_at IS NOT NULL ORDER BY module_id, name",
                    (r["course_id"],),
                )
            ]
            conn.execute(
                """INSERT INTO exams (item_id, scope, logistics, concept_ids, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (item_id) DO UPDATE SET scope = excluded.scope, logistics = excluded.logistics,
                     concept_ids = excluded.concept_ids, updated_at = excluded.updated_at""",
                (r["id"], r["scope"], r["logistics"], json.dumps(concept_ids), iso(now)),
            )
    return len(rows)


@dataclass(frozen=True)
class PracticeRow:
    id: int
    concept_id: int
    concept: str
    kind: str
    prompt: str
    answer: str
    source_ref: str
    last_result: str | None


def attempts_for(conn: sqlite3.Connection, item_ids: list[int]) -> list[Attempt]:
    if not item_ids:
        return []
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT practice_item_id, result, created_at FROM practice_attempts WHERE practice_item_id IN ({marks})",  # noqa: S608
        item_ids,
    ).fetchall()
    return [Attempt(r["practice_item_id"], r["result"], parse_iso(r["created_at"])) for r in rows]


def practice_items(conn: sqlite3.Connection, concept_ids: list[int]) -> list[PracticeRow]:
    if not concept_ids:
        return []
    marks = ",".join("?" * len(concept_ids))
    rows = conn.execute(
        f"""SELECT p.*, k.name AS concept,
                   (SELECT a.result FROM practice_attempts a WHERE a.practice_item_id = p.id
                    ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS last_result
            FROM practice_items p JOIN concepts k ON k.id = p.concept_id
            WHERE p.active = 1 AND p.verified = 1 AND p.concept_id IN ({marks})
            ORDER BY k.name, p.id""",  # noqa: S608
        concept_ids,
    ).fetchall()
    return [
        PracticeRow(
            r["id"],
            r["concept_id"],
            r["concept"],
            r["kind"],
            r["prompt"],
            r["answer"],
            r["source_ref"],
            r["last_result"],
        )
        for r in rows
    ]


def concept_readiness(conn: sqlite3.Connection, concept_ids: list[int]) -> dict[int, Readiness]:
    items = practice_items(conn, concept_ids)
    attempts = attempts_for(conn, [i.id for i in items])
    return {
        cid: readiness([i.id for i in items if i.concept_id == cid], attempts)
        for cid in concept_ids
    }


def review_concepts(conn: sqlite3.Connection, exam_item_id: int) -> list[ReviewConcept]:
    row = conn.execute(
        "SELECT concept_ids FROM exams WHERE item_id = ?", (exam_item_id,)
    ).fetchone()
    ids = json.loads(row["concept_ids"]) if row else []
    if not ids:
        return []
    ready = concept_readiness(conn, ids)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, name, module_id, priority FROM concepts WHERE id IN ({marks})",  # noqa: S608
        ids,
    ).fetchall()
    return [
        ReviewConcept(r["id"], r["name"], r["module_id"], ready[r["id"]], r["priority"])
        for r in rows
    ]


def record_attempt(
    conn: sqlite3.Connection, practice_item_id: int, result: str, now: datetime
) -> None:
    if result not in RESULTS:
        raise ValueError(f"result must be one of {RESULTS}")
    if (
        conn.execute("SELECT 1 FROM practice_items WHERE id = ?", (practice_item_id,)).fetchone()
        is None
    ):
        raise LookupError("No such practice item")
    with transaction(conn):
        conn.execute(
            "INSERT INTO practice_attempts (practice_item_id, result, created_at) VALUES (?, ?, ?)",
            (practice_item_id, result, iso(now)),
        )
