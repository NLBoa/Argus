"""Loaders for the study views: Exam prep (with self-graded practice) and Proposals."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from argus.clock import Clock, to_local
from argus.config import AppConfig
from argus.domain.models import ItemKind
from argus.exams import PracticeRow, concept_readiness, practice_items
from argus.planner.review import Readiness
from argus.planning import compute
from argus.store import repo
from argus.vault import layout
from argus.vault.proposals import EXPIRY
from argus.vault.service import item_note_path
from argus.vault.writer import VaultWriter
from argus.web.format import clock_time, due_text, hours, relative
from argus.web.viewmodels import CourseView, DiffView, PracticeView, course_views, diff_view

PRACTICE_PER_VISIT = 10
_READINESS_ORDER = {
    Readiness.SHAKY: 0,
    Readiness.UNTESTED: 1,
    Readiness.SOLID: 2,
}  # as review_order
_LAST_ORDER = {None: 0, "incorrect": 1, "shaky": 2, "correct": 3}


def obsidian_link(config: AppConfig, rel: str | None) -> str | None:
    """An obsidian:// link, only for notes that exist in the vault."""
    if rel is None or not (config.paths.vault_dir / rel).is_file():
        return None
    return layout.obsidian_uri(config.paths.obsidian_vault, rel)


@dataclass(frozen=True)
class ExamSummary:
    id: int
    title: str
    course: CourseView
    when: str
    relative: str
    counts: dict[str, int]
    practice: int


@dataclass(frozen=True)
class ConceptLine:
    name: str
    readiness: str
    obsidian: str | None


@dataclass(frozen=True)
class SessionLine:
    when: str
    label: str
    past: bool


@dataclass(frozen=True)
class ExamPage:
    id: int
    title: str
    course: CourseView
    when: str | None
    relative: str | None
    scope: str | None
    logistics: str | None
    counts: dict[str, int]
    concepts: list[ConceptLine]
    sessions: list[SessionLine]  # study time reserved in work windows (D062)
    practice: list[PracticeView]
    obsidian: str | None
    study_short: str | None = None  # study time that doesn't fit before the exam


def _concept_ids(conn: sqlite3.Connection, item_id: int) -> list[int]:
    row = conn.execute("SELECT concept_ids FROM exams WHERE item_id = ?", (item_id,)).fetchone()
    return list(json.loads(row["concept_ids"])) if row else []


def _counts(ready: dict[int, Readiness]) -> dict[str, int]:
    return {r.value: sum(1 for v in ready.values() if v is r) for r in Readiness}


def load_exams(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> list[ExamSummary]:
    courses = course_views(config)
    out = []
    for item in repo.list_items(conn, due_after=now, include_undated=False, open_only=True):
        if item.kind is not ItemKind.EXAM or item.due_at is None or item.course_code not in courses:
            continue
        ids = _concept_ids(conn, item.id)
        out.append(
            ExamSummary(
                item.id,
                item.title,
                courses[item.course_code],
                due_text(item.due_at, now),
                relative(item.due_at, now),
                _counts(concept_readiness(conn, ids)),
                len(practice_items(conn, ids)),
            )
        )
    return out


def practice_view(row: PracticeRow, course: CourseView, result: str | None = None) -> PracticeView:
    return PracticeView(row.id, course, row.concept, row.prompt, row.answer, row.source_ref, result)


def practice_order(rows: list[PracticeRow], ready: dict[int, Readiness]) -> list[PracticeRow]:
    """Weakest concepts first, interleaved across concepts. Within a concept: never tried,
    then missed, then shaky, then correct."""
    by_concept: dict[int, list[PracticeRow]] = {}
    for r in sorted(rows, key=lambda r: (_LAST_ORDER[r.last_result], r.id)):
        by_concept.setdefault(r.concept_id, []).append(r)
    queues = [
        by_concept[c]
        for c in sorted(
            by_concept, key=lambda c: (_READINESS_ORDER[ready.get(c, Readiness.UNTESTED)], c)
        )
    ]
    out: list[PracticeRow] = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def load_exam(
    conn: sqlite3.Connection,
    config: AppConfig,
    now: datetime,
    item_id: int,
    *,
    tracking_sources: frozenset[str],
) -> ExamPage | None:
    item = repo.get_item(conn, item_id)
    courses = course_views(config)
    if item is None or item.kind is not ItemKind.EXAM or item.course_code not in courses:
        return None
    course = courses[item.course_code]
    ids = _concept_ids(conn, item.id)
    ready = concept_readiness(conn, ids)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            f"SELECT id, name FROM concepts WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
            ids,
        )
    }
    concepts = sorted(
        (
            ConceptLine(
                names[c],
                ready[c].value,
                obsidian_link(config, layout.concept_note(item.course_code, names[c])),
            )
            for c in ids
            if c in names
        ),
        key=lambda c: (_READINESS_ORDER[Readiness(c.readiness)], c.name),
    )
    plan = compute(conn, config, now, tracking_sources=tracking_sources)
    sessions = [
        SessionLine(
            f"{to_local(sp.start):%a %b} {to_local(sp.start).day}, {clock_time(sp.start)}",
            f"{hours(plan.study.quota[sp.window_id][item.id])} of study",
            sp.end <= now,
        )
        for sp in plan.spans
        if item.id in plan.study.quota.get(sp.window_id, {})
    ]
    short = plan.study.shortfall.get(item.id)
    row = conn.execute(
        "SELECT scope, logistics FROM exams WHERE item_id = ?", (item.id,)
    ).fetchone()
    practice = practice_order(practice_items(conn, ids), ready)[:PRACTICE_PER_VISIT]
    return ExamPage(
        id=item.id,
        title=item.title,
        course=course,
        when=due_text(item.due_at, now) if item.due_at else None,
        relative=relative(item.due_at, now) if item.due_at else None,
        scope=row["scope"] if row else None,
        logistics=row["logistics"] if row else None,
        counts=_counts(ready),
        concepts=concepts,
        sessions=sessions,
        practice=[practice_view(p, course) for p in practice],
        obsidian=obsidian_link(config, item_note_path(item)),
        study_short=hours(short) if short else None,
    )


def practice_card(
    conn: sqlite3.Connection, config: AppConfig, practice_id: int, result: str
) -> PracticeView | None:
    row = conn.execute(
        "SELECT p.concept_id, c.code FROM practice_items p JOIN concepts k ON k.id = p.concept_id"
        " JOIN courses c ON c.id = k.course_id WHERE p.id = ?",
        (practice_id,),
    ).fetchone()
    if row is None:
        return None
    courses = course_views(config)
    item = next((p for p in practice_items(conn, [row["concept_id"]]) if p.id == practice_id), None)
    if item is None or row["code"] not in courses:
        return None
    return practice_view(item, courses[row["code"]], result)


@dataclass(frozen=True)
class ProposalView:
    id: int
    path: str
    reason: str
    created: str
    expires: str
    diff: DiffView
    obsidian: str | None


def load_proposals(conn: sqlite3.Connection, config: AppConfig, clock: Clock) -> list[ProposalView]:
    now = clock.now()
    writer = VaultWriter(config.paths.vault_dir, conn, clock)
    return [
        ProposalView(
            p.id,
            p.path,
            p.reason,
            relative(p.created_at, now),
            relative(p.created_at + EXPIRY, now),
            diff_view(p.path, p.before, p.after),
            obsidian_link(config, p.path),
        )
        for p in writer.proposals()
    ]


def open_proposal_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM proposals WHERE status = 'open'").fetchone()[0])
