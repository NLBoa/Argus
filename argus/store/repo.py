"""Repository: every SQL statement Argus runs lives here. Functions take a connection.

Callers wrap multi-step writes in `db.transaction`. Functions never commit themselves.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from argus.config import RUN_CODE, RUN_TITLE, AppConfig
from argus.domain.changes import ChangeEvent, ChangeKind
from argus.domain.findings import ItemRef
from argus.domain.goals import DocError, GoalDoc, WeekDoc
from argus.domain.merge import merge_sources, resolve_item
from argus.domain.models import (
    AuthorRole,
    CourseInfo,
    GoalSessionGone,
    GoalSessionRecord,
    GradeRecord,
    Item,
    ItemKind,
    ItemLink,
    ItemStatus,
    MaterialPage,
    MaterialRecord,
    MeetingRecord,
    ModuleItemRecord,
    ModuleRecord,
    PostRecord,
    ResolvedItem,
    Source,
    SourceItem,
    SourceKey,
    StoredRun,
    Weekday,
)
from argus.domain.status import StatusFacts, derive_status
from argus.logging import get_logger
from argus.planner.goals import Session
from argus.planner.queue import Pref
from argus.planner.windows import WorkWindow
from argus.store.db import iso, iso_or_none, parse_iso, parse_iso_or_none, transaction

log = get_logger(__name__)

# ---------------------------------------------------------------- helpers


def _b(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _ob(v: object) -> bool | None:
    return None if v is None else bool(v)


def sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- courses


def sync_courses(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, *, running: bool = False
) -> dict[str, int]:
    """Upsert configured courses (plus Running while Runna is set up, D078); deactivate the rest.
    Returns code -> id."""
    ts = iso(now)
    courses = config.all_courses(running=running)
    for c in courses:
        conn.execute(
            """INSERT INTO courses (code, section, canvas_id, gradescope_id, piazza_network_id,
                                    accent, active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT (code) DO UPDATE SET
                 section = excluded.section, canvas_id = excluded.canvas_id,
                 gradescope_id = excluded.gradescope_id,
                 piazza_network_id = excluded.piazza_network_id,
                 accent = excluded.accent, active = 1, updated_at = excluded.updated_at""",
            (
                c.code,
                c.section,
                c.canvas_id,
                c.gradescope_id,
                c.piazza_network_id,
                c.accent,
                ts,
                ts,
            ),
        )
    if any(c.code == RUN_CODE for c in courses):
        conn.execute(
            "UPDATE courses SET title = ? WHERE code = ? AND title IS NOT ?",
            (RUN_TITLE, RUN_CODE, RUN_TITLE),
        )
    codes = [c.code for c in courses]
    marks = ",".join("?" * len(codes))
    conn.execute(
        f"UPDATE courses SET active = 0, updated_at = ? WHERE code NOT IN ({marks})",  # noqa: S608
        (ts, *codes),
    )
    return course_ids(conn)


def course_ids(conn: sqlite3.Connection, active_only: bool = True) -> dict[str, int]:
    if active_only:
        rows = conn.execute("SELECT code, id FROM courses WHERE active = 1")
    else:
        rows = conn.execute("SELECT code, id FROM courses")
    return {r["code"]: r["id"] for r in rows}


def update_course_info(conn: sqlite3.Connection, info: CourseInfo, now: datetime) -> None:
    conn.execute(
        "UPDATE courses SET title = COALESCE(?, title), instructors = ?, updated_at = ?"
        " WHERE code = ?",
        (info.title, json.dumps(list(info.instructors)), iso(now), info.course_code),
    )


# ---------------------------------------------------------------- meetings


def replace_meetings(
    conn: sqlite3.Connection,
    course_id: int,
    source: str,
    meetings: Sequence[MeetingRecord],
    now: datetime,
) -> None:
    conn.execute("DELETE FROM meetings WHERE course_id = ? AND source = ?", (course_id, source))
    for m in meetings:
        conn.execute(
            """INSERT INTO meetings (course_id, source, kind, days, start_local, end_local,
                                     building, room, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                course_id,
                source,
                m.kind,
                ",".join(str(int(d)) for d in m.days),
                m.start.strftime("%H:%M"),
                m.end.strftime("%H:%M"),
                m.building,
                m.room,
                iso(now),
            ),
        )


def list_meetings(conn: sqlite3.Connection) -> list[MeetingRecord]:
    rows = conn.execute(
        """SELECT c.code, m.* FROM meetings m JOIN courses c ON c.id = m.course_id
           WHERE c.active = 1 ORDER BY c.code, m.start_local"""
    )
    return [
        MeetingRecord(
            course_code=r["code"],
            kind=r["kind"],
            days=tuple(Weekday(int(d)) for d in r["days"].split(",") if d),
            start=time.fromisoformat(r["start_local"]),
            end=time.fromisoformat(r["end_local"]),
            building=r["building"],
            room=r["room"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------- source state


@dataclass(frozen=True, slots=True)
class SourceState:
    payload_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    data: str | None = None


def all_source_state(conn: sqlite3.Connection) -> dict[tuple[str, str], SourceState]:
    return {
        (r["source"], r["key"]): SourceState(
            r["payload_hash"], r["etag"], r["last_modified"], r["data"]
        )
        for r in conn.execute("SELECT * FROM source_state")
    }


def set_source_state(
    conn: sqlite3.Connection, source: str, key: str, state: SourceState, now: datetime
) -> None:
    conn.execute(
        """INSERT INTO source_state (source, key, payload_hash, etag, last_modified, data,
                                    updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (source, key) DO UPDATE SET payload_hash = excluded.payload_hash,
             etag = excluded.etag, last_modified = excluded.last_modified, data = excluded.data,
             updated_at = excluded.updated_at""",
        (source, key, state.payload_hash, state.etag, state.last_modified, state.data, iso(now)),
    )


# ---------------------------------------------------------------- source items


def upsert_source_items(
    conn: sqlite3.Connection, items: Iterable[SourceItem], ids: dict[str, int], now: datetime
) -> int:
    """Insert or update source records. Returns how many were new or changed."""
    ts = iso(now)
    changed = 0
    for it in items:
        course_id = ids.get(it.course_code)
        if course_id is None:
            continue
        row = conn.execute(
            "SELECT raw_hash, present FROM item_sources WHERE source = ? AND external_id = ?",
            (it.source.value, it.external_id),
        ).fetchone()
        values = (
            course_id,
            it.title,
            it.kind.value,
            iso_or_none(it.due_at),
            iso_or_none(it.release_at),
            iso_or_none(it.late_due_at),
            int(it.all_day),
            it.points,
            _b(it.submitted),
            _b(it.graded),
            it.description,
            it.question_count,
            it.url,
            it.raw_hash,
            it.duration_minutes,
            iso_or_none(it.preferred_start),
            it.completion_id,
        )
        if row is None:
            conn.execute(
                """INSERT INTO item_sources (course_id, title, kind, due_at, release_at,
                     late_due_at, all_day, points, submitted, graded, description,
                     question_count, url, raw_hash, duration_minutes, preferred_start,
                     completion_id, source, external_id, present, first_seen_at, last_seen_at,
                     changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (*values, it.source.value, it.external_id, ts, ts, ts),
            )
            changed += 1
        elif row["raw_hash"] != it.raw_hash or not row["present"]:
            conn.execute(
                """UPDATE item_sources SET course_id = ?, title = ?, kind = ?, due_at = ?,
                     release_at = ?, late_due_at = ?, all_day = ?, points = ?, submitted = ?,
                     graded = ?, description = ?, question_count = ?, url = ?, raw_hash = ?,
                     duration_minutes = ?, preferred_start = ?, completion_id = ?,
                     present = 1, last_seen_at = ?, changed_at = ?
                   WHERE source = ? AND external_id = ?""",
                (*values, ts, ts, it.source.value, it.external_id),
            )
            changed += 1
        else:
            conn.execute(
                "UPDATE item_sources SET last_seen_at = ? WHERE source = ? AND external_id = ?",
                (ts, it.source.value, it.external_id),
            )
    return changed


@dataclass(frozen=True, slots=True)
class RunDetail:
    """What Runna says about a run (D078): its length and a good time to start."""

    duration_minutes: int | None
    preferred_start: datetime | None


def run_details(conn: sqlite3.Connection, item_ids: Sequence[int]) -> dict[int, RunDetail]:
    """Runna details by item id, from each item's present Runna record."""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        "SELECT item_id, duration_minutes, preferred_start FROM item_sources"  # noqa: S608
        f" WHERE source = ? AND present = 1 AND item_id IN ({marks})",
        (Source.RUNNA_ICAL.value, *item_ids),
    )
    return {
        r["item_id"]: RunDetail(r["duration_minutes"], parse_iso_or_none(r["preferred_start"]))
        for r in rows
    }


def stored_runs(
    conn: sqlite3.Connection, *, since: datetime, exclude: Iterable[str]
) -> list[StoredRun]:
    """Stored Runna records due at or after `since`, except those in `exclude` (this fetch's)."""
    skip = set(exclude)
    rows = conn.execute(
        """SELECT s.external_id, s.title, s.due_at, s.description, s.duration_minutes,
                  s.preferred_start, s.completion_id, i.status
           FROM item_sources s LEFT JOIN items i ON i.id = s.item_id
           WHERE s.source = ? AND s.due_at >= ?""",
        (Source.RUNNA_ICAL.value, iso(since)),
    )
    return [
        StoredRun(
            external_id=r["external_id"],
            title=r["title"],
            due_at=parse_iso_or_none(r["due_at"]),
            description=r["description"],
            duration_minutes=r["duration_minutes"],
            preferred_start=parse_iso_or_none(r["preferred_start"]),
            open=r["status"] is None or ItemStatus(r["status"]).is_open,
            completion_id=r["completion_id"],
        )
        for r in rows
        if r["external_id"] not in skip
    ]


class SuspiciousListingError(Exception):
    """A complete fetch dropped most (or all) upcoming records at once: more likely a broken
    page or login than real deletions (review R6). Nothing is marked absent."""


def mark_absent(
    conn: sqlite3.Connection,
    source: Source,
    *,
    course_id: int,
    present_external_ids: set[str],
    now: datetime,
    not_before: datetime | None = None,
    guard: bool = True,
) -> int:
    """After a complete fetch of (source, course), mark records it no longer lists as absent.
    With `guard`, a listing that drops all or most upcoming records raises instead (R6).

    Records due before `not_before` are left alone: they fell out of the source's window
    (the Canvas feed keeps about 30 days of history), they were not deleted.
    """
    rows = conn.execute(
        "SELECT external_id, due_at FROM item_sources"
        " WHERE source = ? AND course_id = ? AND present = 1",
        (source.value, course_id),
    ).fetchall()
    cutoff = iso(not_before) if not_before else None
    gone = [
        r["external_id"]
        for r in rows
        if r["external_id"] not in present_external_ids
        and not (cutoff and r["due_at"] is not None and r["due_at"] < cutoff)
    ]
    upcoming = {r["external_id"] for r in rows if r["due_at"] is None or r["due_at"] >= iso(now)}
    vanishing = upcoming.intersection(gone)
    if (
        guard
        and vanishing
        and (
            not present_external_ids or (len(vanishing) >= 3 and len(vanishing) * 2 > len(upcoming))
        )
    ):
        raise SuspiciousListingError(
            f"{source.value} listed {len(present_external_ids)} records;"
            f" {len(vanishing)} upcoming ones vanished at once"
        )
    for ext in gone:
        conn.execute(
            "UPDATE item_sources SET present = 0, changed_at = ?"
            " WHERE source = ? AND external_id = ?",
            (iso(now), source.value, ext),
        )
    return len(gone)


def _row_to_source_item(r: sqlite3.Row) -> SourceItem:
    return SourceItem(
        source=Source(r["source"]),
        external_id=r["external_id"],
        course_code=r["code"],
        title=r["title"],
        kind=ItemKind(r["kind"]),
        due_at=parse_iso_or_none(r["due_at"]),
        raw_hash=r["raw_hash"],
        url=r["url"],
        release_at=parse_iso_or_none(r["release_at"]),
        late_due_at=parse_iso_or_none(r["late_due_at"]),
        all_day=bool(r["all_day"]),
        points=r["points"],
        submitted=_ob(r["submitted"]),
        graded=_ob(r["graded"]),
        description=r["description"],
        question_count=r["question_count"],
    )


def present_source_items(
    conn: sqlite3.Connection,
) -> tuple[list[SourceItem], dict[SourceKey, int]]:
    rows = conn.execute(
        """SELECT s.*, c.code FROM item_sources s JOIN courses c ON c.id = s.course_id
           WHERE s.present = 1 AND c.active = 1"""
    ).fetchall()
    records = [_row_to_source_item(r) for r in rows]
    links = {
        SourceKey(Source(r["source"]), r["external_id"]): int(r["item_id"])
        for r in rows
        if r["item_id"] is not None
    }
    return records, links


# ---------------------------------------------------------------- items


def _conflict_json(resolved: ResolvedItem) -> str | None:
    if not resolved.date_conflict:
        return None
    return json.dumps([[s.value, iso(d)] for s, d in resolved.date_conflict])


_ITEM_FIELDS = (
    "course_id",
    "kind",
    "title",
    "due_at",
    "due_source",
    "release_at",
    "late_due_at",
    "points",
    "submitted",
    "graded",
    "description",
    "description_hash",
    "date_conflict",
)


def _update_item_if_changed(
    conn: sqlite3.Connection, item_id: int, fields: tuple[object, ...], ts: str
) -> None:
    cols = ", ".join(_ITEM_FIELDS)
    row = conn.execute(
        f"SELECT {cols}, removed_at, conflict_resolved_at FROM items WHERE id = ?",  # noqa: S608
        (item_id,),
    ).fetchone()
    current = tuple(row[c] for c in _ITEM_FIELDS)
    if current == fields and row["removed_at"] is None:
        return
    due = fields[_ITEM_FIELDS.index("due_at")]
    new_due = due if isinstance(due, str) else None
    if row["removed_at"] is not None:
        _record_change(conn, item_id, ChangeKind.RESTORED, old=None, new=new_due, at=ts)
    elif row["due_at"] != new_due:
        _record_change(conn, item_id, ChangeKind.DUE, old=row["due_at"], new=new_due, at=ts)
    # A changed disagreement must be resolved again.
    resolved_at = row["conflict_resolved_at"] if row["date_conflict"] == fields[-1] else None
    assignments = ", ".join(f"{c} = ?" for c in _ITEM_FIELDS)
    conn.execute(
        f"UPDATE items SET {assignments}, conflict_resolved_at = ?, removed_at = NULL,"  # noqa: S608
        " updated_at = ? WHERE id = ?",
        (*fields, resolved_at, ts, item_id),
    )


def apply_merge(conn: sqlite3.Connection, ids: dict[str, int], now: datetime) -> list[int]:
    """Group present source records into items, create or update them, and mark orphans
    removed. Returns the ids of new items."""
    ts = iso(now)
    records, links = present_source_items(conn)
    by_key = {r.key: r for r in records}
    created: list[int] = []
    live: set[int] = set()
    student = overrides(conn)
    for group in merge_sources(records, links):
        members = [by_key[k] for k in group.keys]
        resolved = resolve_item(members)
        override = student.get(group.item_id) if group.item_id else None
        override_due, cancelled = (
            (override.due_at, override.cancelled) if override else (None, False)
        )
        if override is not None and group.item_id is not None:
            official = iso_or_none(resolved.due_at) or "none"
            if override.basis is None:
                conn.execute(
                    "UPDATE item_overrides SET basis = ? WHERE item_id = ?",
                    (official, group.item_id),
                )
            elif override.basis != official:
                # An official source changed the date after the student's decision: official wins again.
                conn.execute("DELETE FROM item_overrides WHERE item_id = ?", (group.item_id,))
                log.info(
                    "override dropped: official date changed", extra={"item_id": group.item_id}
                )
                override_due, cancelled = None, False
        if cancelled:
            continue  # the student confirmed a cancellation: the item stays hidden
        if override_due is not None:
            # The student's confirmed date wins; the disagreement stays recorded.
            resolved = replace(resolved, due_at=override_due, due_source=Source.MANUAL)
        course_id = ids[resolved.course_code]
        fields = (
            course_id,
            resolved.kind.value,
            resolved.title,
            iso_or_none(resolved.due_at),
            resolved.due_source.value if resolved.due_source else None,
            iso_or_none(resolved.release_at),
            iso_or_none(resolved.late_due_at),
            resolved.points,
            _b(resolved.submitted),
            _b(resolved.graded),
            resolved.description,
            sha256(resolved.description) if resolved.description else None,
            _conflict_json(resolved),
        )
        if group.item_id is None:
            cur = conn.execute(
                """INSERT INTO items (course_id, kind, title, due_at, due_source, release_at,
                     late_due_at, points, submitted, graded, description, description_hash,
                     date_conflict, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*fields, ts, ts),
            )
            item_id = int(cur.lastrowid or 0)
            created.append(item_id)
            _record_change(
                conn, item_id, ChangeKind.NEW, old=None, new=iso_or_none(resolved.due_at), at=ts
            )
        else:
            item_id = group.item_id
            _update_item_if_changed(conn, item_id, fields, ts)
        live.add(item_id)
        for k in group.keys:
            conn.execute(
                "UPDATE item_sources SET item_id = ? WHERE source = ? AND external_id = ?",
                (item_id, k.source.value, k.external_id),
            )
    orphans = [
        (r["id"], r["due_at"])
        for r in conn.execute("SELECT id, due_at FROM items WHERE removed_at IS NULL")
        if r["id"] not in live
    ]
    for item_id, due in orphans:
        conn.execute(
            "UPDATE items SET removed_at = ?, updated_at = ? WHERE id = ?", (ts, ts, item_id)
        )
        _record_change(conn, item_id, ChangeKind.REMOVED, old=due, new=due, at=ts)
    return created


def _record_change(
    conn: sqlite3.Connection,
    item_id: int,
    change: ChangeKind,
    *,
    old: str | None,
    new: str | None,
    at: str,
) -> None:
    """Append to the item change log (D073). Called only from apply_merge, the one place
    items are created, moved, removed, or restored."""
    conn.execute(
        "INSERT INTO item_changes (item_id, change, old_due, new_due, at) VALUES (?, ?, ?, ?, ?)",
        (item_id, change.value, old, new, at),
    )


def item_changes_since(conn: sqlite3.Connection, since: datetime) -> list[ChangeEvent]:
    """Change-log events strictly after `since`, oldest first."""
    rows = conn.execute(
        "SELECT item_id, change, old_due, new_due, at FROM item_changes WHERE at > ? ORDER BY at, id",
        (iso(since),),
    )
    return [
        ChangeEvent(
            item_id=r["item_id"],
            change=ChangeKind(r["change"]),
            old_due=parse_iso_or_none(r["old_due"]),
            new_due=parse_iso_or_none(r["new_due"]),
            at=parse_iso(r["at"]),
        )
        for r in rows
    ]


def latest_change_at(conn: sqlite3.Connection, item_ids: Sequence[int]) -> dict[int, datetime]:
    """When each item last appeared in the change log."""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    return {
        r[0]: parse_iso(r[1])
        for r in conn.execute(
            f"SELECT item_id, MAX(at) FROM item_changes WHERE item_id IN ({marks}) GROUP BY item_id",  # noqa: S608
            tuple(item_ids),
        )
    }


def refresh_statuses(
    conn: sqlite3.Connection, now: datetime, *, tracking_sources: frozenset[str]
) -> int:
    """Recompute every live item's status. Returns how many changed.

    `tracking_sources` are the enabled sources that report submissions
    (`Capabilities.tracking_sources`). An item with no present record from one of them is
    never MISSED (D056)."""
    changed = 0
    marks = ",".join("?" * len(tracking_sources)) or "NULL"
    rows = conn.execute(
        f"""SELECT i.id, i.kind, i.due_at, i.late_due_at, i.submitted, i.graded, i.user_done_at,
                   i.started_at, i.status,
                   EXISTS (SELECT 1 FROM item_sources s WHERE s.item_id = i.id AND s.present = 1
                           AND s.source IN ({marks})) AS tracked
              FROM items i WHERE i.removed_at IS NULL""",  # noqa: S608 - placeholders only
        tuple(sorted(tracking_sources)),
    ).fetchall()
    for r in rows:
        status = derive_status(
            StatusFacts(
                kind=ItemKind(r["kind"]),
                due_at=parse_iso_or_none(r["due_at"]),
                late_due_at=parse_iso_or_none(r["late_due_at"]),
                submitted=_ob(r["submitted"]),
                graded=_ob(r["graded"]),
                user_done=r["user_done_at"] is not None,
                started=r["started_at"] is not None,
                tracked=bool(r["tracked"]),
            ),
            now,
        )
        if status.value != r["status"]:
            conn.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, iso(now), r["id"]),
            )
            changed += 1
    return changed


def _row_to_item(r: sqlite3.Row, links: tuple[ItemLink, ...]) -> Item:
    conflict: tuple[tuple[Source, datetime], ...] = ()
    if r["date_conflict"] and not r["conflict_resolved_at"]:
        conflict = tuple((Source(s), parse_iso(d)) for s, d in json.loads(r["date_conflict"]))
    return Item(
        id=r["id"],
        course_code=r["code"],
        kind=ItemKind(r["kind"]),
        title=r["title"],
        due_at=parse_iso_or_none(r["due_at"]),
        status=ItemStatus(r["status"]),
        due_source=Source(r["due_source"]) if r["due_source"] else None,
        release_at=parse_iso_or_none(r["release_at"]),
        late_due_at=parse_iso_or_none(r["late_due_at"]),
        points=r["points"],
        submitted=_ob(r["submitted"]),
        graded=_ob(r["graded"]),
        description=r["description"],
        date_conflict=conflict,
        links=links,
        created_at=parse_iso(r["created_at"]),
    )


def _links_for(
    conn: sqlite3.Connection, item_ids: Sequence[int], *, present_only: bool = True
) -> dict[int, tuple[ItemLink, ...]]:
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    present = "present = 1 AND " if present_only else ""
    out: dict[int, list[ItemLink]] = {}
    for r in conn.execute(
        f"SELECT item_id, source, url FROM item_sources WHERE {present}item_id IN ({marks})"  # noqa: S608
        " ORDER BY source",
        tuple(item_ids),
    ):
        out.setdefault(r["item_id"], []).append(ItemLink(Source(r["source"]), r["url"]))
    return {k: tuple(v) for k, v in out.items()}


def list_items(
    conn: sqlite3.Connection,
    *,
    due_after: datetime | None = None,
    due_before: datetime | None = None,
    open_only: bool = False,
    include_undated: bool = True,
) -> list[Item]:
    clauses = ["i.removed_at IS NULL", "c.active = 1"]
    params: list[object] = []
    window: list[str] = []
    if due_after is not None:
        window.append("i.due_at >= ?")
        params.append(iso(due_after))
    if due_before is not None:
        window.append("i.due_at < ?")
        params.append(iso(due_before))
    if window:
        dated = " AND ".join(window)
        clauses.append(f"(({dated}) OR i.due_at IS NULL)" if include_undated else f"({dated})")
    elif not include_undated:
        clauses.append("i.due_at IS NOT NULL")
    if open_only:
        clauses.append("i.status IN ('upcoming', 'in_progress')")
    rows = conn.execute(
        "SELECT i.*, c.code FROM items i JOIN courses c ON c.id = i.course_id WHERE "  # noqa: S608
        + " AND ".join(clauses)
        + " ORDER BY i.due_at IS NULL, i.due_at, c.code, i.title",
        params,
    ).fetchall()
    links = _links_for(conn, [r["id"] for r in rows])
    return [_row_to_item(r, links.get(r["id"], ())) for r in rows]


def items_by_ids(conn: sqlite3.Connection, item_ids: Sequence[int]) -> list[Item]:
    """These items, removed ones included. A removed item has no present source record, so it
    carries the links its records had when they were last seen."""
    if not item_ids:
        return []
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        "SELECT i.*, c.code FROM items i JOIN courses c ON c.id = i.course_id"  # noqa: S608
        f" WHERE i.id IN ({marks}) ORDER BY i.due_at IS NULL, i.due_at, c.code, i.title",
        tuple(item_ids),
    ).fetchall()
    links = _links_for(conn, [r["id"] for r in rows])
    gone = [r["id"] for r in rows if r["id"] not in links]
    links.update(_links_for(conn, gone, present_only=False))
    return [_row_to_item(r, links.get(r["id"], ())) for r in rows]


def finding_platforms(conn: sqlite3.Connection) -> dict[int, str]:
    """Per item: the platform (`piazza`, `canvas`) of the post its present finding record
    came from."""
    rows = conn.execute(
        """SELECT s.item_id, p.platform FROM item_sources s
             JOIN findings f ON s.external_id = 'finding:' || f.id
             JOIN posts p ON p.id = f.post_id
            WHERE s.source = 'finding' AND s.present = 1 AND s.item_id IS NOT NULL"""
    )
    return {r["item_id"]: r["platform"] for r in rows}


def active_course_titles(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Active course codes and their titles, by code."""
    return {
        r["code"]: r["title"]
        for r in conn.execute("SELECT code, title FROM courses WHERE active = 1 ORDER BY code")
    }


def get_item(conn: sqlite3.Connection, item_id: int) -> Item | None:
    r = conn.execute(
        "SELECT i.*, c.code FROM items i JOIN courses c ON c.id = i.course_id WHERE i.id = ?",
        (item_id,),
    ).fetchone()
    if r is None:
        return None
    return _row_to_item(r, _links_for(conn, [item_id]).get(item_id, ()))


# ---------------------------------------------------------------- posts


def upsert_posts(
    conn: sqlite3.Connection, posts: Iterable[PostRecord], ids: dict[str, int], now: datetime
) -> list[int]:
    """Insert or update posts by (platform, external_id). Returns ids of new or edited posts."""
    ts = iso(now)
    touched: list[int] = []
    for p in posts:
        course_id = ids.get(p.course_code)
        if course_id is None:
            continue
        row = conn.execute(
            "SELECT id, content_hash FROM posts WHERE platform = ? AND external_id = ?",
            (p.platform, p.external_id),
        ).fetchone()
        values = (
            course_id,
            p.via.value,
            p.kind,
            p.author_role.value,
            p.title,
            p.body,
            p.url,
            int(p.pinned),
            iso(p.posted_at),
            iso_or_none(p.edited_at),
            p.raw_hash,
        )
        if row is None:
            cur = conn.execute(
                """INSERT INTO posts (course_id, via, kind, author_role, title, body, url, pinned,
                     posted_at, edited_at, content_hash, platform, external_id, first_seen_at,
                     updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*values, p.platform, p.external_id, ts, ts),
            )
            touched.append(int(cur.lastrowid or 0))
        elif row["content_hash"] != p.raw_hash:
            conn.execute(
                """UPDATE posts SET course_id = ?, via = ?, kind = ?, author_role = ?, title = ?,
                     body = ?, url = ?, pinned = ?, posted_at = ?, edited_at = ?,
                     content_hash = ?, updated_at = ?
                   WHERE id = ?""",
                (*values, ts, row["id"]),
            )
            touched.append(int(row["id"]))
    return touched


def list_posts(conn: sqlite3.Connection, course_code: str | None = None) -> list[PostRecord]:
    sql = "SELECT p.*, c.code FROM posts p JOIN courses c ON c.id = p.course_id"
    params: tuple[object, ...] = ()
    if course_code:
        sql += " WHERE c.code = ?"
        params = (course_code,)
    rows = conn.execute(sql + " ORDER BY p.posted_at DESC", params).fetchall()
    return [
        PostRecord(
            platform=r["platform"],
            external_id=r["external_id"],
            via=Source(r["via"]),
            course_code=r["code"],
            title=r["title"],
            body=r["body"],
            posted_at=parse_iso(r["posted_at"]),
            raw_hash=r["content_hash"],
            author_role=AuthorRole(r["author_role"]),
            kind=r["kind"],
            url=r["url"],
            edited_at=parse_iso_or_none(r["edited_at"]),
            pinned=bool(r["pinned"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------- sync runs


@dataclass(frozen=True, slots=True)
class SyncRun:
    source: str
    started_at: datetime
    finished_at: datetime
    status: str
    error: str | None
    records: int


def record_sync_run(
    conn: sqlite3.Connection,
    *,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    error: str | None = None,
    records: int = 0,
) -> None:
    duration = int((finished_at - started_at).total_seconds() * 1000)
    conn.execute(
        """INSERT INTO sync_runs (source, started_at, finished_at, status, error, records,
             duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source, iso(started_at), iso(finished_at), status, error, records, max(duration, 0)),
    )


def recent_sync_runs(conn: sqlite3.Connection, source: str, limit: int = 20) -> list[SyncRun]:
    rows = conn.execute(
        "SELECT * FROM sync_runs WHERE source = ? ORDER BY started_at DESC, id DESC LIMIT ?",
        (source, limit),
    ).fetchall()
    return [
        SyncRun(
            r["source"],
            parse_iso(r["started_at"]),
            parse_iso(r["finished_at"]),
            r["status"],
            r["error"],
            r["records"],
        )
        for r in rows
    ]


def consecutive_failures(conn: sqlite3.Connection, source: str) -> int:
    n = 0
    for run in recent_sync_runs(conn, source, limit=50):
        if run.status in ("ok", "unchanged"):
            break
        if run.status in ("error", "auth_error"):
            n += 1
    return n


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source: str
    last_attempt: datetime | None
    last_success: datetime | None
    last_status: str | None
    last_error: str | None
    failures: int
    failing_since: datetime | None = None  # start of the current failure streak


def source_health(conn: sqlite3.Connection, sources: Iterable[str]) -> dict[str, SourceHealth]:
    out: dict[str, SourceHealth] = {}
    for s in sources:
        runs = recent_sync_runs(conn, s, limit=50)
        ok = next((r for r in runs if r.status in ("ok", "unchanged")), None)
        last = runs[0] if runs else None
        streak = []
        for r in runs:
            if r.status in ("ok", "unchanged"):
                break
            if r.status in ("error", "auth_error"):
                streak.append(r)
        out[s] = SourceHealth(
            source=s,
            last_attempt=last.started_at if last else None,
            last_success=ok.finished_at if ok else None,
            last_status=last.status if last else None,
            last_error=last.error if last else None,
            failures=len(streak),
            failing_since=streak[-1].started_at if streak else None,
        )
    return out


# ---------------------------------------------------------------- student actions


def set_user_done(conn: sqlite3.Connection, item_id: int, done: bool, now: datetime) -> bool:
    """Mark (or unmark) an item done by the student. Returns False if no such item."""
    cur = conn.execute(
        "UPDATE items SET user_done_at = ?, updated_at = ? WHERE id = ? AND removed_at IS NULL",
        (iso(now) if done else None, iso(now), item_id),
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------- time logs


@dataclass(frozen=True, slots=True)
class TimeLog:
    id: int
    item_id: int
    started_at: datetime
    ended_at: datetime | None
    source: str
    window_id: str | None = None  # checkin: the window it answers for
    created_at: datetime | None = None

    def hours(self, now: datetime) -> float:
        end = self.ended_at or now
        return max(0.0, (end - self.started_at).total_seconds() / 3600)


def _row_to_log(r: sqlite3.Row) -> TimeLog:
    return TimeLog(
        r["id"],
        r["item_id"],
        parse_iso(r["started_at"]),
        parse_iso_or_none(r["ended_at"]),
        r["source"],
        r["window_id"],
        parse_iso_or_none(r["created_at"]),
    )


def running_timer(conn: sqlite3.Connection) -> TimeLog | None:
    r = conn.execute("SELECT * FROM time_logs WHERE ended_at IS NULL").fetchone()
    return _row_to_log(r) if r else None


def start_timer(conn: sqlite3.Connection, item_id: int, now: datetime) -> TimeLog:
    """Start timing an item, stopping any other running timer first."""
    stop_timer(conn, now)
    conn.execute(
        "INSERT INTO time_logs (item_id, started_at, source, created_at) VALUES (?, ?, 'timer', ?)",
        (item_id, iso(now), iso(now)),
    )
    conn.execute(
        "UPDATE items SET started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
        (iso(now), iso(now), item_id),
    )
    timer = running_timer(conn)
    assert timer is not None
    return timer


def stop_timer(conn: sqlite3.Connection, now: datetime) -> TimeLog | None:
    timer = running_timer(conn)
    if timer is None:
        return None
    conn.execute(
        "UPDATE time_logs SET ended_at = ? WHERE id = ?",
        (iso(max(now, timer.started_at)), timer.id),
    )
    return TimeLog(
        timer.id, timer.item_id, timer.started_at, max(now, timer.started_at), timer.source
    )


def log_time(
    conn: sqlite3.Connection, item_id: int, hours: float, now: datetime, source: str = "manual"
) -> None:
    """Record time already spent (e.g. `LOG n h`), ending now."""
    if not 0 < hours <= 24:
        raise ValueError("hours must be between 0 and 24")
    start = now - timedelta(hours=hours)
    conn.execute(
        """INSERT INTO time_logs (item_id, started_at, ended_at, source, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (item_id, iso(start), iso(now), source, iso(now)),
    )
    conn.execute(
        "UPDATE items SET started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
        (iso(start), iso(now), item_id),
    )


def time_logs_for(conn: sqlite3.Connection, item_id: int) -> list[TimeLog]:
    rows = conn.execute(
        "SELECT * FROM time_logs WHERE item_id = ? ORDER BY started_at", (item_id,)
    ).fetchall()
    return [_row_to_log(r) for r in rows]


def all_time_logs(conn: sqlite3.Connection) -> list[TimeLog]:
    rows = conn.execute("SELECT * FROM time_logs ORDER BY started_at, id").fetchall()
    return [_row_to_log(r) for r in rows]


def time_logs_created_between(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[TimeLog]:
    rows = conn.execute(
        "SELECT * FROM time_logs WHERE created_at >= ? AND created_at < ?",
        (iso(start), iso(end)),
    ).fetchall()
    return [_row_to_log(r) for r in rows]


def checkin(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    window_id: str,
    window_start: datetime,
    hours: float,
    now: datetime,
) -> bool:
    """The student's answer to "did you work on it?" for one window (D060): `hours` of work
    (0 for "None"), recorded once per item and window. Returns False if already answered."""
    if not 0 <= hours <= 24:
        raise ValueError("hours must be between 0 and 24")
    cur = conn.execute(
        """INSERT OR IGNORE INTO time_logs (item_id, started_at, ended_at, source, window_id, created_at)
           VALUES (?, ?, ?, 'checkin', ?, ?)""",
        (
            item_id,
            iso(window_start),
            iso(window_start + timedelta(hours=hours)),
            window_id,
            iso(now),
        ),
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------- notifications


@dataclass(frozen=True, slots=True)
class NotificationRow:
    id: int
    key: str
    rule: str
    status: str
    title: str
    body: str
    item_ids: tuple[int, ...]
    priority: int
    counts_toward_cap: bool
    attempts: int
    channel: str | None
    created_at: datetime
    sent_at: datetime | None
    expires_at: datetime | None = None
    error: str | None = None


def _row_to_notification(r: sqlite3.Row) -> NotificationRow:
    return NotificationRow(
        id=r["id"],
        key=r["key"],
        rule=r["rule"],
        status=r["status"],
        title=r["title"],
        body=r["body"],
        item_ids=tuple(json.loads(r["item_ids"])),
        priority=r["priority"],
        counts_toward_cap=bool(r["counts_toward_cap"]),
        attempts=r["attempts"],
        channel=r["channel"],
        created_at=parse_iso(r["created_at"]),
        sent_at=parse_iso_or_none(r["sent_at"]),
        expires_at=parse_iso_or_none(r["expires_at"]),
        error=r["error"],
    )


def claim_notification(
    conn: sqlite3.Connection,
    *,
    keys: Sequence[str],
    rule: str,
    title: str,
    body: str,
    item_ids: Sequence[int],
    priority: int,
    counts_toward_cap: bool,
    now: datetime,
    expires_at: datetime | None = None,
) -> int | None:
    """Insert a pending notification covering every key, atomically.

    Returns its id, or None if any key was already claimed (then nothing is written).
    """
    if not keys:
        raise ValueError("a notification needs at least one idempotency key")
    marks = ",".join("?" * len(keys))
    taken = conn.execute(
        f"SELECT 1 FROM notification_keys WHERE key IN ({marks}) LIMIT 1",  # noqa: S608
        tuple(keys),
    ).fetchone()
    if taken:
        return None
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO notifications (key, rule, status, title, body, item_ids, priority,
                 counts_toward_cap, expires_at, created_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (
                keys[0],
                rule,
                title,
                body,
                json.dumps(list(item_ids)),
                priority,
                int(counts_toward_cap),
                iso_or_none(expires_at),
                iso(now),
            ),
        )
        nid = int(cur.lastrowid or 0)
        conn.executemany(
            "INSERT INTO notification_keys (key, notification_id) VALUES (?, ?)",
            [(k, nid) for k in keys],
        )
    return nid


def finish_notification(
    conn: sqlite3.Connection,
    notification_id: int,
    *,
    ok: bool,
    channel: str | None,
    error: str | None,
    now: datetime,
) -> None:
    conn.execute(
        """UPDATE notifications SET status = ?, channel = ?, error = ?, attempts = attempts + 1,
             sent_at = CASE WHEN ? THEN ? ELSE sent_at END, last_attempt_at = ?
           WHERE id = ?""",
        ("sent" if ok else "failed", channel, error, int(ok), iso(now), iso(now), notification_id),
    )


def notification_keys(conn: sqlite3.Connection) -> set[str]:
    return {r["key"] for r in conn.execute("SELECT key FROM notification_keys")}


RETRY_BACKOFF_MAX = timedelta(minutes=15)
RETRY_MAX_ATTEMPTS = 100  # with no expiry (system alerts): about a day of retries


def retry_delay(attempts: int) -> timedelta:
    """1, 2, 4, 8 minutes, then every 15 minutes."""
    return min(timedelta(minutes=2 ** max(attempts - 1, 0)), RETRY_BACKOFF_MAX)


def retryable_notifications(conn: sqlite3.Connection, now: datetime) -> list[NotificationRow]:
    """Failed (or never-finished) sends that are still relevant and due for another try.
    They are retried until they expire (review R4), not a fixed three times."""
    rows = conn.execute(
        """SELECT * FROM notifications
           WHERE status IN ('failed', 'pending') AND attempts < ?
             AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY id""",
        (RETRY_MAX_ATTEMPTS, iso(now)),
    ).fetchall()
    due = []
    for r in rows:
        last = parse_iso_or_none(r["last_attempt_at"])
        if r["attempts"] == 0 or last is None or now - last >= retry_delay(r["attempts"]):
            due.append(_row_to_notification(r))
    return due


def failing_deadline_sends(conn: sqlite3.Connection, now: datetime) -> int:
    """Deadline reminders still undelivered after 3 tries: delivery itself is broken."""
    return int(
        conn.execute(
            """SELECT COUNT(*) FROM notifications WHERE status = 'failed' AND attempts >= 3
               AND rule IN ('due_24h', 'due_3h') AND (expires_at IS NULL OR expires_at > ?)""",
            (iso(now),),
        ).fetchone()[0]
    )


def notifications_sent_between(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[NotificationRow]:
    rows = conn.execute(
        """SELECT * FROM notifications WHERE status = 'sent' AND sent_at >= ? AND sent_at < ?
           ORDER BY sent_at""",
        (iso(start), iso(end)),
    ).fetchall()
    return [_row_to_notification(r) for r in rows]


def recent_notifications(conn: sqlite3.Connection, limit: int = 20) -> list[NotificationRow]:
    rows = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_notification(r) for r in rows]


# ---------------------------------------------------------------- rule mutes


def set_rule_mute(
    conn: sqlite3.Connection, rule: str, muted: bool, now: datetime, until: datetime | None = None
) -> None:
    if muted:
        conn.execute(
            """INSERT INTO rule_mutes (rule, muted_until, created_at) VALUES (?, ?, ?)
               ON CONFLICT (rule) DO UPDATE SET muted_until = excluded.muted_until""",
            (rule, iso_or_none(until), iso(now)),
        )
    else:
        conn.execute("DELETE FROM rule_mutes WHERE rule = ?", (rule,))


def muted_rules(conn: sqlite3.Connection, now: datetime) -> set[str]:
    out: set[str] = set()
    for r in conn.execute("SELECT rule, muted_until FROM rule_mutes"):
        until = parse_iso_or_none(r["muted_until"])
        if until is None or until > now:
            out.add(r["rule"])
    return out


def first_success_at(conn: sqlite3.Connection) -> datetime | None:
    """When the very first successful sync finished (the end of the initial import)."""
    r = conn.execute(
        "SELECT MIN(finished_at) FROM sync_runs WHERE status IN ('ok', 'unchanged')"
    ).fetchone()
    return parse_iso_or_none(r[0])


# ---------------------------------------------------------------- modules & grades


def replace_modules(
    conn: sqlite3.Connection, course_id: int, modules: Sequence[ModuleRecord], now: datetime
) -> None:
    """Make the stored modules for a course exactly `modules` (keeping digests by canvas id)."""
    keep = {m.canvas_id for m in modules}
    for r in conn.execute(
        "SELECT canvas_id FROM modules WHERE course_id = ?", (course_id,)
    ).fetchall():
        if r["canvas_id"] not in keep:
            conn.execute(
                "DELETE FROM modules WHERE course_id = ? AND canvas_id = ?",
                (course_id, r["canvas_id"]),
            )
    for m in modules:
        items = json.dumps(
            [
                {
                    "title": i.title,
                    "type": i.type,
                    "url": i.url,
                    "indent": i.indent,
                    "api_url": i.api_url,
                }
                for i in m.items
            ]
        )
        conn.execute(
            """INSERT INTO modules (course_id, canvas_id, name, position, state, unlock_at, items,
                                    updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (course_id, canvas_id) DO UPDATE SET name = excluded.name,
                 position = excluded.position, state = excluded.state,
                 unlock_at = excluded.unlock_at, items = excluded.items,
                 updated_at = excluded.updated_at""",
            (
                course_id,
                m.canvas_id,
                m.name,
                m.position,
                m.state,
                iso_or_none(m.unlock_at),
                items,
                iso(now),
            ),
        )


@dataclass(frozen=True, slots=True)
class StoredModule:
    record: ModuleRecord
    digest: str | None


def list_modules(conn: sqlite3.Connection, course_code: str) -> list[StoredModule]:
    rows = conn.execute(
        """SELECT m.*, c.code FROM modules m JOIN courses c ON c.id = m.course_id
           WHERE c.code = ? ORDER BY m.position, m.name""",
        (course_code,),
    ).fetchall()
    return [
        StoredModule(
            ModuleRecord(
                course_code=r["code"],
                canvas_id=r["canvas_id"],
                name=r["name"],
                position=r["position"],
                items=tuple(
                    ModuleItemRecord(
                        i["title"],
                        i["type"],
                        i.get("url"),
                        int(i.get("indent") or 0),
                        i.get("api_url"),
                    )
                    for i in json.loads(r["items"])
                ),
                unlock_at=parse_iso_or_none(r["unlock_at"]),
                state=r["state"],
            ),
            r["digest"],
        )
        for r in rows
    ]


def set_grade(conn: sqlite3.Connection, grade: GradeRecord, now: datetime) -> None:
    conn.execute(
        "UPDATE courses SET grade_score = ?, grade_letter = ?, grade_updated_at = ? WHERE code = ?",
        (grade.score, grade.letter, iso(now), grade.course_code),
    )


@dataclass(frozen=True, slots=True)
class CourseRow:
    code: str
    section: str
    title: str | None
    instructors: tuple[str, ...]
    canvas_id: int | None
    gradescope_id: str | None
    piazza_network_id: str | None
    grade_score: float | None
    grade_letter: str | None
    grade_updated_at: datetime | None


def get_course(conn: sqlite3.Connection, code: str) -> CourseRow | None:
    r = conn.execute("SELECT * FROM courses WHERE code = ? AND active = 1", (code,)).fetchone()
    if r is None:
        return None
    return CourseRow(
        code=r["code"],
        section=r["section"],
        title=r["title"],
        instructors=tuple(json.loads(r["instructors"] or "[]")),
        canvas_id=r["canvas_id"],
        gradescope_id=r["gradescope_id"],
        piazza_network_id=r["piazza_network_id"],
        grade_score=r["grade_score"],
        grade_letter=r["grade_letter"],
        grade_updated_at=parse_iso_or_none(r["grade_updated_at"]),
    )


# ---------------------------------------------------------------- search


@dataclass(frozen=True, slots=True)
class SearchHit:
    kind: str
    ref_id: str
    course: str
    title: str
    snippet: str  # HTML-safe: escaped text with <mark> around matches
    url: str | None


_MARK_OPEN, _MARK_CLOSE = "\x02", "\x03"


def rebuild_search_index(conn: sqlite3.Connection) -> int:
    """Rebuild the FTS index from items, modules, and posts. Returns the row count."""
    conn.execute("DELETE FROM search_index")
    conn.execute(
        """INSERT INTO search_index (title, body, kind, ref_id, course, url)
           SELECT i.title, COALESCE(i.description, ''), 'item', i.id, c.code,
                  (SELECT s.url FROM item_sources s WHERE s.item_id = i.id AND s.present = 1
                     AND s.url IS NOT NULL ORDER BY s.source LIMIT 1)
           FROM items i JOIN courses c ON c.id = i.course_id
           WHERE i.removed_at IS NULL AND c.active = 1"""
    )
    for r in conn.execute(
        """SELECT m.id, m.name, m.items, c.code FROM modules m
           JOIN courses c ON c.id = m.course_id WHERE c.active = 1"""
    ).fetchall():
        body = "; ".join(i["title"] for i in json.loads(r["items"]))
        conn.execute(
            "INSERT INTO search_index (title, body, kind, ref_id, course, url)"
            " VALUES (?, ?, 'module', ?, ?, ?)",
            (r["name"], body, r["id"], r["code"], f"/courses/{r['code']}#module-{r['id']}"),
        )
    conn.execute(
        """INSERT INTO search_index (title, body, kind, ref_id, course, url)
           SELECT p.title, p.body, 'post', p.id, c.code, p.url
           FROM posts p JOIN courses c ON c.id = p.course_id WHERE c.active = 1"""
    )
    return int(conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0])


def fts_query(text: str) -> str | None:
    """User text -> a safe FTS5 query: every word quoted, the last one as a prefix."""
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in text).split() if w]
    if not words:
        return None
    quoted = [f'"{w}"' for w in words[:12]]
    quoted[-1] += "*"
    return " ".join(quoted)


def search(
    conn: sqlite3.Connection, text: str, *, course: str | None = None, limit: int = 50
) -> list[SearchHit]:
    from html import escape

    q = fts_query(text)
    if q is None:
        return []
    # Only module constants are interpolated; user text is bound as a parameter.
    sql = (
        "SELECT kind, ref_id, course, title, url,"  # noqa: S608
        f" snippet(search_index, 1, '{_MARK_OPEN}', '{_MARK_CLOSE}', '...', 14) AS snip"
        " FROM search_index WHERE search_index MATCH ?"
    )
    params: list[object] = [q]
    if course:
        sql += " AND course = ?"
        params.append(course)
    sql += " ORDER BY bm25(search_index, 4.0, 1.0) LIMIT ?"
    params.append(limit)
    hits = []
    for r in conn.execute(sql, params).fetchall():
        snip = escape(r["snip"] or "").replace(_MARK_OPEN, "<mark>").replace(_MARK_CLOSE, "</mark>")
        hits.append(SearchHit(r["kind"], str(r["ref_id"]), r["course"], r["title"], snip, r["url"]))
    return hits


# ---------------------------------------------------------------- findings & overrides


@dataclass(frozen=True, slots=True)
class StoredPost:
    id: int
    record: PostRecord
    scanned_hash: str | None


def posts_to_scan(conn: sqlite3.Connection, limit: int = 20) -> list[StoredPost]:
    rows = conn.execute(
        """SELECT p.*, c.code FROM posts p JOIN courses c ON c.id = p.course_id
           WHERE c.active = 1 AND (p.scanned_hash IS NULL OR p.scanned_hash != p.content_hash)
           ORDER BY p.posted_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [StoredPost(r["id"], _row_to_post(r), r["scanned_hash"]) for r in rows]


def _row_to_post(r: sqlite3.Row) -> PostRecord:
    return PostRecord(
        platform=r["platform"],
        external_id=r["external_id"],
        via=Source(r["via"]),
        course_code=r["code"],
        title=r["title"],
        body=r["body"],
        posted_at=parse_iso(r["posted_at"]),
        raw_hash=r["content_hash"],
        author_role=AuthorRole(r["author_role"]),
        kind=r["kind"],
        url=r["url"],
        edited_at=parse_iso_or_none(r["edited_at"]),
        pinned=bool(r["pinned"]),
    )


def mark_scanned(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute("UPDATE posts SET scanned_hash = content_hash WHERE id = ?", (post_id,))


def get_post(conn: sqlite3.Connection, post_id: int) -> StoredPost | None:
    r = conn.execute(
        "SELECT p.*, c.code FROM posts p JOIN courses c ON c.id = p.course_id WHERE p.id = ?",
        (post_id,),
    ).fetchone()
    return StoredPost(r["id"], _row_to_post(r), r["scanned_hash"]) if r else None


def item_refs(conn: sqlite3.Connection, course_code: str | None = None) -> list[ItemRef]:
    """Live items with whether any official (Canvas/Gradescope) record backs them."""
    sql = """SELECT i.id, c.code, i.title, i.kind, i.due_at,
                    EXISTS (SELECT 1 FROM item_sources s WHERE s.item_id = i.id AND s.present = 1
                              AND s.source IN ('canvas_ical', 'canvas_api', 'gradescope')) AS official
             FROM items i JOIN courses c ON c.id = i.course_id
             WHERE i.removed_at IS NULL AND c.active = 1"""
    params: tuple[object, ...] = ()
    if course_code:
        sql += " AND c.code = ?"
        params = (course_code,)
    return [
        ItemRef(
            r["id"],
            r["code"],
            r["title"],
            ItemKind(r["kind"]),
            parse_iso_or_none(r["due_at"]),
            bool(r["official"]),
        )
        for r in conn.execute(sql, params)
    ]


@dataclass(frozen=True, slots=True)
class FindingRow:
    id: int
    post_id: int
    course_code: str
    kind: str
    title: str
    date: datetime | None
    date_only: bool
    scope: str | None
    quote: str
    confidence: float
    item_id: int | None
    status: str
    reason: str
    created_at: datetime


def insert_finding(
    conn: sqlite3.Connection,
    *,
    post_id: int,
    course_id: int,
    kind: str,
    title: str,
    date: datetime | None,
    date_only: bool,
    scope: str | None,
    quote: str,
    confidence: float,
    item_id: int | None,
    status: str,
    reason: str,
    now: datetime,
) -> int:
    cur = conn.execute(
        """INSERT INTO findings (post_id, course_id, kind, title, date, date_only, scope, quote, confidence,
                                 item_id, status, reason, created_at, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            post_id,
            course_id,
            kind,
            title,
            iso_or_none(date),
            int(date_only),
            scope,
            quote,
            confidence,
            item_id,
            status,
            reason,
            iso(now),
            iso(now) if status in ("applied", "linked", "stored") else None,
        ),
    )
    return int(cur.lastrowid or 0)


def _row_to_finding(r: sqlite3.Row) -> FindingRow:
    return FindingRow(
        id=r["id"],
        post_id=r["post_id"],
        course_code=r["code"],
        kind=r["kind"],
        title=r["title"],
        date=parse_iso_or_none(r["date"]),
        date_only=bool(r["date_only"]),
        scope=r["scope"],
        quote=r["quote"],
        confidence=r["confidence"],
        item_id=r["item_id"],
        status=r["status"],
        reason=r["reason"],
        created_at=parse_iso(r["created_at"]),
    )


def get_finding(conn: sqlite3.Connection, finding_id: int) -> FindingRow | None:
    r = conn.execute(
        "SELECT f.*, c.code FROM findings f JOIN courses c ON c.id = f.course_id WHERE f.id = ?",
        (finding_id,),
    ).fetchone()
    return _row_to_finding(r) if r else None


def list_findings(
    conn: sqlite3.Connection, statuses: Sequence[str] | None = None
) -> list[FindingRow]:
    sql = "SELECT f.*, c.code FROM findings f JOIN courses c ON c.id = f.course_id"
    params: tuple[object, ...] = ()
    if statuses:
        sql += f" WHERE f.status IN ({','.join('?' * len(statuses))})"
        params = tuple(statuses)
    return [
        _row_to_finding(r)
        for r in conn.execute(sql + " ORDER BY f.created_at DESC, f.id DESC", params)
    ]


def set_finding_status(
    conn: sqlite3.Connection,
    finding_id: int,
    status: str,
    now: datetime,
    item_id: int | None = None,
) -> None:
    conn.execute(
        "UPDATE findings SET status = ?, resolved_at = ?, item_id = COALESCE(?, item_id) WHERE id = ?",
        (status, iso(now), item_id, finding_id),
    )


def supersede_open_findings(conn: sqlite3.Connection, post_id: int, now: datetime) -> None:
    """An edited post replaces its unresolved findings."""
    conn.execute(
        "UPDATE findings SET status = 'superseded', resolved_at = ?"
        " WHERE post_id = ? AND status IN ('pending', 'conflict')",
        (iso(now), post_id),
    )


def add_finding_source(
    conn: sqlite3.Connection,
    *,
    finding_id: int,
    course_id: int,
    title: str,
    kind: ItemKind,
    due_at: datetime | None,
    date_only: bool,
    url: str | None,
    item_id: int | None,
    now: datetime,
) -> None:
    """A FINDING source record; with `item_id`, it attaches to that item (and supersedes its older ones)."""
    ts = iso(now)
    if item_id is not None:
        conn.execute(
            "UPDATE item_sources SET present = 0, changed_at = ? WHERE item_id = ? AND source = 'finding'",
            (ts, item_id),
        )
    conn.execute(
        """INSERT INTO item_sources (item_id, source, external_id, course_id, title, kind, due_at, all_day, url,
                                     raw_hash, present, first_seen_at, last_seen_at, changed_at)
           VALUES (?, 'finding', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (
            item_id,
            f"finding:{finding_id}",
            course_id,
            title,
            kind.value,
            iso_or_none(due_at),
            int(date_only),
            url,
            f"finding:{finding_id}",
            ts,
            ts,
            ts,
        ),
    )


def item_for_source(conn: sqlite3.Connection, source: Source, external_id: str) -> int | None:
    r = conn.execute(
        "SELECT item_id FROM item_sources WHERE source = ? AND external_id = ?",
        (source.value, external_id),
    ).fetchone()
    return int(r[0]) if r and r[0] is not None else None


def set_item_info(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    scope: str | None = None,
    logistics: str | None = None,
) -> None:
    if scope is not None:
        conn.execute("UPDATE items SET scope = ? WHERE id = ?", (scope, item_id))
    if logistics is not None:
        conn.execute("UPDATE items SET logistics = ? WHERE id = ?", (logistics, item_id))


def set_override(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    due_at: datetime | None,
    cancelled: bool,
    reason: str,
    now: datetime,
) -> None:
    conn.execute(
        """INSERT INTO item_overrides (item_id, due_at, cancelled, reason, created_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (item_id) DO UPDATE SET due_at = COALESCE(excluded.due_at, due_at),
             cancelled = MAX(cancelled, excluded.cancelled), reason = excluded.reason,
             created_at = excluded.created_at, basis = NULL""",
        (item_id, iso_or_none(due_at), int(cancelled), reason, iso(now)),
    )


@dataclass(frozen=True, slots=True)
class Override:
    due_at: datetime | None
    cancelled: bool
    basis: str | None  # the official due date (ISO or 'none') it was made against


def overrides(conn: sqlite3.Connection) -> dict[int, Override]:
    return {
        r["item_id"]: Override(parse_iso_or_none(r["due_at"]), bool(r["cancelled"]), r["basis"])
        for r in conn.execute("SELECT item_id, due_at, cancelled, basis FROM item_overrides")
    }


def unlinked_official_records(conn: sqlite3.Connection) -> list[SourceItem]:
    rows = conn.execute(
        """SELECT s.*, c.code FROM item_sources s JOIN courses c ON c.id = s.course_id
           WHERE s.present = 1 AND s.item_id IS NULL AND s.source IN ('canvas_ical', 'canvas_api', 'gradescope')"""
    ).fetchall()
    return [_row_to_source_item(r) for r in rows]


def announced_only_items(conn: sqlite3.Connection) -> list[ItemRef]:
    return [r for r in item_refs(conn) if not r.official and _has_finding_source(conn, r.id)]


def _has_finding_source(conn: sqlite3.Connection, item_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM item_sources WHERE item_id = ? AND source = 'finding' AND present = 1",
            (item_id,),
        ).fetchone()
        is not None
    )


def link_source(conn: sqlite3.Connection, key: SourceKey, item_id: int) -> None:
    conn.execute(
        "UPDATE item_sources SET item_id = ? WHERE source = ? AND external_id = ?",
        (item_id, key.source.value, key.external_id),
    )


# ---------------------------------------------------------------- estimates


@dataclass(frozen=True, slots=True)
class EstimateRow:
    id: int
    item_id: int
    version: int
    source: str  # ai | default | user
    p50: float
    p80: float
    confidence: str | None
    breakdown: tuple[tuple[str, float], ...]
    assumptions: tuple[str, ...]
    reasoning: str | None
    input_hash: str
    model: str | None
    prompt_version: str | None
    created_at: datetime
    retracted_at: datetime | None


def _row_to_estimate(r: sqlite3.Row) -> EstimateRow:
    return EstimateRow(
        id=r["id"],
        item_id=r["item_id"],
        version=r["version"],
        source=r["source"],
        p50=r["p50_hours"],
        p80=r["p80_hours"],
        confidence=r["confidence"],
        breakdown=tuple((b["step"], float(b["hours"])) for b in json.loads(r["breakdown"])),
        assumptions=tuple(json.loads(r["assumptions"])),
        reasoning=r["reasoning"],
        input_hash=r["input_hash"],
        model=r["model"],
        prompt_version=r["prompt_version"],
        created_at=parse_iso(r["created_at"]),
        retracted_at=parse_iso_or_none(r["retracted_at"]),
    )


def latest_runna_estimates(conn: sqlite3.Connection, item_ids: Sequence[int]) -> dict[int, str]:
    """Each item's newest `runna` estimate's input hash (D078)."""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        "SELECT item_id, input_hash FROM estimates e"  # noqa: S608
        f" WHERE source = 'runna' AND item_id IN ({marks}) AND version = ("
        "   SELECT MAX(version) FROM estimates WHERE item_id = e.item_id AND source = 'runna')",
        tuple(item_ids),
    )
    return {r["item_id"]: r["input_hash"] for r in rows}


def insert_estimate(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    source: str,
    p50: float,
    p80: float,
    input_hash: str,
    now: datetime,
    confidence: str | None = None,
    breakdown: Sequence[tuple[str, float]] = (),
    assumptions: Sequence[str] = (),
    reasoning: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> int:
    """Append a new estimate version. Estimates are never overwritten."""
    version = int(
        conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM estimates WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
    )
    conn.execute(
        """INSERT INTO estimates (item_id, version, source, p50_hours, p80_hours, confidence, breakdown, assumptions,
                                  reasoning, input_hash, model, prompt_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            version,
            source,
            p50,
            max(p80, p50),
            confidence,
            json.dumps([{"step": s, "hours": h} for s, h in breakdown]),
            json.dumps(list(assumptions)),
            reasoning,
            input_hash,
            model,
            prompt_version,
            iso(now),
        ),
    )
    return version


def estimates_for(conn: sqlite3.Connection, item_id: int) -> list[EstimateRow]:
    rows = conn.execute(
        "SELECT * FROM estimates WHERE item_id = ? ORDER BY version", (item_id,)
    ).fetchall()
    return [_row_to_estimate(r) for r in rows]


def latest_estimates(
    conn: sqlite3.Connection,
) -> dict[int, tuple[EstimateRow | None, EstimateRow | None]]:
    """item_id -> (latest model/default estimate, active user override)."""
    out: dict[int, tuple[EstimateRow | None, EstimateRow | None]] = {}
    for r in conn.execute("SELECT * FROM estimates ORDER BY item_id, version"):
        e = _row_to_estimate(r)
        base, user = out.get(e.item_id, (None, None))
        if e.source == "user":
            user = None if e.retracted_at else e
        else:
            base = e
        out[e.item_id] = (base, user)
    return out


def retract_user_estimates(conn: sqlite3.Connection, item_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE estimates SET retracted_at = ? WHERE item_id = ? AND source = 'user' AND retracted_at IS NULL",
        (iso(now), item_id),
    )


@dataclass(frozen=True, slots=True)
class EstimateMiss:
    """The AI gave no estimate for this input (D069)."""

    item_id: int
    input_hash: str
    outcome: str  # incomplete | failed
    reason: str
    prompt_version: str | None
    created_at: datetime


def record_estimate_miss(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    input_hash: str,
    outcome: str,
    reason: str,
    prompt_version: str | None,
    now: datetime,
) -> None:
    conn.execute(
        """INSERT INTO estimate_misses (item_id, input_hash, outcome, reason, prompt_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (item_id) DO UPDATE SET input_hash = excluded.input_hash, outcome = excluded.outcome,
             reason = excluded.reason, prompt_version = excluded.prompt_version, created_at = excluded.created_at""",
        (item_id, input_hash, outcome, reason, prompt_version, iso(now)),
    )


def clear_estimate_miss(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM estimate_misses WHERE item_id = ?", (item_id,))


def estimate_misses(conn: sqlite3.Connection) -> dict[int, EstimateMiss]:
    return {
        r["item_id"]: EstimateMiss(
            r["item_id"],
            r["input_hash"],
            r["outcome"],
            r["reason"],
            r["prompt_version"],
            parse_iso(r["created_at"]),
        )
        for r in conn.execute("SELECT * FROM estimate_misses")
    }


def logged_hours(conn: sqlite3.Connection, now: datetime) -> dict[int, float]:
    """Progress per item: every logged hour, a running timer up to now included."""
    return _hours(conn, now, include_legacy=True)


def actual_hours(conn: sqlite3.Connection, now: datetime) -> dict[int, float]:
    """Time the student actually reported, for calibration: not `legacy` credit carried over
    from planned sessions (D060)."""
    return _hours(conn, now, include_legacy=False)


def _hours(conn: sqlite3.Connection, now: datetime, *, include_legacy: bool) -> dict[int, float]:
    out: dict[int, float] = {}
    rows = conn.execute(
        "SELECT item_id, started_at, ended_at FROM time_logs WHERE ? OR source <> 'legacy'",
        (include_legacy,),
    )
    for r in rows:
        end = parse_iso_or_none(r["ended_at"]) or now
        out[r["item_id"]] = out.get(r["item_id"], 0.0) + max(
            0.0, (end - parse_iso(r["started_at"])).total_seconds() / 3600
        )
    return out


@dataclass(frozen=True, slots=True)
class FinishedRow:
    item_id: int
    course_code: str
    kind: ItemKind
    title: str
    p50: float
    actual: float


def finished_history(conn: sqlite3.Connection, now: datetime) -> list[FinishedRow]:
    """Finished items with logged time: the evidence for calibration and fallbacks."""
    hours = actual_hours(conn, now)
    latest = latest_estimates(conn)
    out = []
    for r in conn.execute(
        """SELECT i.id, i.title, i.kind, c.code FROM items i JOIN courses c ON c.id = i.course_id
           WHERE i.status IN ('submitted', 'graded')"""
    ):
        actual = hours.get(r["id"], 0.0)
        if actual <= 0:
            continue
        base, _ = latest.get(r["id"], (None, None))
        out.append(
            FinishedRow(
                r["id"],
                r["code"],
                ItemKind(r["kind"]),
                r["title"],
                base.p50 if base else 0.0,
                actual,
            )
        )
    return out


# ---------------------------------------------------------------- materials


def upsert_materials(
    conn: sqlite3.Connection,
    materials: Iterable[MaterialRecord],
    ids: dict[str, int],
    now: datetime,
) -> int:
    changed = 0
    for m in materials:
        course_id = ids.get(m.course_code)
        if course_id is None:
            continue
        pages = json.dumps([{"ref": p.ref, "text": p.text} for p in m.pages])
        cur = conn.execute(
            """INSERT INTO materials (course_id, module_id, title, kind, url, pages, content_hash, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (course_id, url) DO UPDATE SET module_id = excluded.module_id, title = excluded.title,
                 kind = excluded.kind, pages = excluded.pages, content_hash = excluded.content_hash,
                 fetched_at = excluded.fetched_at
               WHERE materials.content_hash != excluded.content_hash""",
            (course_id, m.module_id, m.title, m.kind, m.url, pages, m.content_hash, iso(now)),
        )
        changed += cur.rowcount
    return changed


def list_materials(
    conn: sqlite3.Connection, course_code: str, module_id: int | None = None
) -> list[MaterialRecord]:
    sql = (
        "SELECT m.*, c.code FROM materials m JOIN courses c ON c.id = m.course_id WHERE c.code = ?"
    )
    params: list[object] = [course_code]
    if module_id is not None:
        sql += " AND m.module_id = ?"
        params.append(module_id)
    return [
        MaterialRecord(
            course_code=r["code"],
            module_id=r["module_id"],
            title=r["title"],
            kind=r["kind"],
            url=r["url"],
            pages=tuple(MaterialPage(p["ref"], p["text"]) for p in json.loads(r["pages"])),
            content_hash=r["content_hash"],
        )
        for r in conn.execute(sql + " ORDER BY m.module_id, m.title", params)
    ]


# ---------------------------------------------------------------- work windows and the queue


def _plan_state_row(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        """INSERT INTO plan_state (id, inputs_hash, explanation, planned_at)
           VALUES (1, '', '', ?) ON CONFLICT (id) DO NOTHING""",
        (iso(now),),
    )


def work_windows(conn: sqlite3.Connection) -> list[WorkWindow]:
    """The student's work windows (one row per start day, grouped back into windows)."""
    grouped: dict[tuple[str, str], set[int]] = {}
    for r in conn.execute("SELECT day, start_time, end_time FROM work_windows ORDER BY id"):
        grouped.setdefault((r["start_time"], r["end_time"]), set()).add(int(r["day"]))
    return [
        WorkWindow(frozenset(days), time.fromisoformat(start), time.fromisoformat(end))
        for (start, end), days in grouped.items()
    ]


def set_work_windows(
    conn: sqlite3.Connection, windows: Sequence[WorkWindow], now: datetime
) -> None:
    conn.execute("DELETE FROM work_windows")
    for w in windows:
        for day in sorted(w.days):
            conn.execute(
                "INSERT INTO work_windows (day, start_time, end_time, created_at) VALUES (?, ?, ?, ?)",
                (day, f"{w.start:%H:%M}", f"{w.end:%H:%M}", iso(now)),
            )


def seed_work_windows(
    conn: sqlite3.Connection, windows: Sequence[WorkWindow], now: datetime
) -> bool:
    """First run only: copy config.yaml's work windows into the table (D057). Marked as seeded
    only when config gave at least one window, so adding them to config later still works."""
    _plan_state_row(conn, now)
    seeded = conn.execute("SELECT windows_seeded_at FROM plan_state WHERE id = 1").fetchone()[0]
    if seeded is not None or not windows:
        return False
    if conn.execute("SELECT 1 FROM work_windows LIMIT 1").fetchone() is None:
        set_work_windows(conn, windows, now)
    conn.execute("UPDATE plan_state SET windows_seeded_at = ? WHERE id = 1", (iso(now),))
    return True


def mark_windows_set(conn: sqlite3.Connection, now: datetime) -> None:
    """The student set their windows in Settings: config.yaml never seeds them again (D057)."""
    _plan_state_row(conn, now)
    conn.execute("UPDATE plan_state SET windows_seeded_at = ? WHERE id = 1", (iso(now),))


def queue_prefs(conn: sqlite3.Connection) -> dict[str, Pref]:
    return {
        r["entry"]: Pref(
            r["entry"],
            r["pin_rank"],
            r["pin_kind"],
            parse_iso_or_none(r["pin_until"]),
            r["skip_window"],
        )
        for r in conn.execute("SELECT * FROM queue_prefs")
    }


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_setting(conn: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, iso(now)),
    )


def save_queue_pref(conn: sqlite3.Connection, pref: Pref, now: datetime) -> None:
    """Store the student's say over one queue entry (D058); a row with nothing left is removed."""
    if pref.pin_rank is None and pref.skip_window is None:
        conn.execute("DELETE FROM queue_prefs WHERE entry = ?", (pref.entry_id,))
        return
    conn.execute(
        """INSERT INTO queue_prefs (entry, item_id, pin_rank, pin_kind, pin_until, skip_window, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (entry) DO UPDATE SET pin_rank = excluded.pin_rank, pin_kind = excluded.pin_kind,
             pin_until = excluded.pin_until, skip_window = excluded.skip_window,
             updated_at = excluded.updated_at""",
        (
            pref.entry_id,
            int(pref.entry_id.split(":", 1)[1]),
            pref.pin_rank,
            pref.pin_kind,
            iso_or_none(pref.pin_until),
            pref.skip_window,
            iso(now),
        ),
    )


def latest_remaining_marks(conn: sqlite3.Connection) -> dict[int, tuple[float, datetime]]:
    """Per item, the student's latest "about N h left" and when it was given."""
    out: dict[int, tuple[float, datetime]] = {}
    for r in conn.execute("SELECT item_id, hours, at FROM remaining_marks ORDER BY at, id"):
        out[r["item_id"]] = (float(r["hours"]), parse_iso(r["at"]))
    return out


def add_remaining_mark(
    conn: sqlite3.Connection, item_id: int, hours: float, now: datetime, source: str
) -> None:
    if not 0 <= hours <= 200:
        raise ValueError("hours must be between 0 and 200")
    conn.execute(
        "INSERT INTO remaining_marks (item_id, hours, at, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (item_id, hours, iso(now), source, iso(now)),
    )


@dataclass(frozen=True, slots=True)
class WindowPlan:
    window_id: str
    starts_at: datetime
    ends_at: datetime
    allocation: tuple[tuple[str, float], ...]  # (entry, hours) in queue order


def _row_to_window_plan(r: sqlite3.Row) -> WindowPlan:
    return WindowPlan(
        r["window_id"],
        parse_iso(r["starts_at"]),
        parse_iso(r["ends_at"]),
        tuple((str(e), float(h)) for e, h in json.loads(r["allocation"])),
    )


def window_plan(conn: sqlite3.Connection, window_id: str) -> WindowPlan | None:
    r = conn.execute("SELECT * FROM window_plans WHERE window_id = ?", (window_id,)).fetchone()
    return _row_to_window_plan(r) if r else None


def latest_ended_window_plan(conn: sqlite3.Connection, now: datetime) -> WindowPlan | None:
    r = conn.execute(
        "SELECT * FROM window_plans WHERE ends_at <= ? ORDER BY ends_at DESC LIMIT 1", (iso(now),)
    ).fetchone()
    return _row_to_window_plan(r) if r else None


def save_window_plan(
    conn: sqlite3.Connection,
    window_id: str,
    *,
    starts_at: datetime,
    ends_at: datetime,
    allocation: Sequence[tuple[str, float]],
    now: datetime,
) -> None:
    """Keep a window's split up to date until it ends; after that it is history (D061)."""
    if now >= ends_at:
        return
    conn.execute(
        """INSERT INTO window_plans (window_id, starts_at, ends_at, allocation, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (window_id) DO UPDATE SET starts_at = excluded.starts_at,
             ends_at = excluded.ends_at, allocation = excluded.allocation,
             updated_at = excluded.updated_at""",
        (
            window_id,
            iso(starts_at),
            iso(ends_at),
            json.dumps([[e, round(h, 4)] for e, h in allocation]),
            iso(now),
        ),
    )


def convert_legacy_blocks(conn: sqlite3.Connection, now: datetime) -> int | None:
    """Once, after 0009 (D060): planned work sessions that had passed counted as progress for
    items with no logged time (D030). Carry that over as `legacy` time so remaining hours don't
    jump at the switch. Returns the rows added, or None if already done."""
    _plan_state_row(conn, now)
    if conn.execute("SELECT legacy_converted_at FROM plan_state WHERE id = 1").fetchone()[0]:
        return None
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plan_blocks_legacy'"
    ).fetchone()
    added = 0
    if exists:
        cur = conn.execute(
            """INSERT INTO time_logs (item_id, started_at, ended_at, source, created_at)
               SELECT b.item_id, b.start_at, MIN(b.end_at, ?), 'legacy', ?
                 FROM plan_blocks_legacy b
                WHERE b.kind = 'work' AND b.item_id IS NOT NULL AND b.start_at < ?
                  AND b.item_id NOT IN (SELECT item_id FROM time_logs WHERE source <> 'legacy')""",
            (iso(now), iso(now), iso(now)),
        )
        added = cur.rowcount
    conn.execute(
        "UPDATE plan_state SET legacy_converted_at = ?, model_since = COALESCE(model_since, ?)"
        " WHERE id = 1",
        (iso(now), iso(now)),
    )
    return added


# ---------------------------------------------------------------- goals (D079)


@dataclass(frozen=True, slots=True)
class StoredGoalDoc:
    path: str
    kind: str  # "goal" | "week"
    good: GoalDoc | WeekDoc | None  # the last valid parse
    good_at: datetime | None
    error: DocError | None  # the file's current problem
    read_at: datetime


def upsert_goal_doc(
    conn: sqlite3.Connection,
    *,
    path: str,
    kind: str,
    text_hash: str,
    parsed: GoalDoc | WeekDoc | None,
    error: DocError | None,
    now: datetime,
) -> None:
    """Record a read of a goal or week file. A valid parse replaces the stored version; an error
    keeps the last valid one beside it, so one bad write never blanks the page."""
    if parsed is not None:
        key = parsed.tag if isinstance(parsed, GoalDoc) else parsed.week
        conn.execute(
            """INSERT INTO goal_docs (path, kind, doc_key, text_hash, good_json, good_at, error,
                                      error_line, error_path, read_at, missing_since)
               VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL)
               ON CONFLICT (path) DO UPDATE SET kind = excluded.kind, doc_key = excluded.doc_key,
                 text_hash = excluded.text_hash, good_json = excluded.good_json,
                 good_at = excluded.good_at, error = NULL, error_line = NULL, error_path = NULL,
                 read_at = excluded.read_at, missing_since = NULL""",
            (path, kind, key, text_hash, parsed.model_dump_json(), iso(now), iso(now)),
        )
        return
    assert error is not None
    conn.execute(
        """INSERT INTO goal_docs (path, kind, text_hash, error, error_line, error_path, read_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (path) DO UPDATE SET text_hash = excluded.text_hash,
             error = excluded.error, error_line = excluded.error_line,
             error_path = excluded.error_path, read_at = excluded.read_at, missing_since = NULL""",
        (path, kind, text_hash, error.message, error.line, error.path, iso(now)),
    )


def goal_doc_errors(conn: sqlite3.Connection) -> list[tuple[str, DocError]]:
    """Current problems in goal and week files, without parsing their stored versions (the
    status line asks on every page)."""
    rows = conn.execute(
        """SELECT path, error, error_line, error_path FROM goal_docs
           WHERE error IS NOT NULL AND missing_since IS NULL ORDER BY path"""
    )
    return [(r["path"], DocError(r["error_line"], r["error_path"] or "", r["error"])) for r in rows]


def mark_goal_docs_missing(conn: sqlite3.Connection, present: set[str], now: datetime) -> None:
    stored = [
        r["path"] for r in conn.execute("SELECT path FROM goal_docs WHERE missing_since IS NULL")
    ]
    gone = [p for p in stored if p not in present]
    conn.executemany(
        "UPDATE goal_docs SET missing_since = ? WHERE path = ?", [(iso(now), p) for p in gone]
    )


def _goal_doc(row: sqlite3.Row) -> StoredGoalDoc:
    good: GoalDoc | WeekDoc | None = None
    if row["good_json"] is not None:
        model: type[GoalDoc] | type[WeekDoc] = GoalDoc if row["kind"] == "goal" else WeekDoc
        try:
            good = model.model_validate_json(row["good_json"])
        except ValueError:
            log.warning("stored goal doc no longer parses", extra={"path": row["path"]})
    error = (
        DocError(row["error_line"], row["error_path"] or "", row["error"])
        if row["error"] is not None
        else None
    )
    return StoredGoalDoc(
        path=row["path"],
        kind=row["kind"],
        good=good,
        good_at=parse_iso_or_none(row["good_at"]),
        error=error,
        read_at=parse_iso(row["read_at"]),
    )


def goal_docs(conn: sqlite3.Connection) -> list[StoredGoalDoc]:
    """Goal and week files currently in the vault, by path."""
    rows = conn.execute("SELECT * FROM goal_docs WHERE missing_since IS NULL ORDER BY path")
    return [_goal_doc(r) for r in rows]


def has_goal_docs(conn: sqlite3.Connection) -> bool:
    """Any goal file in the vault, valid or not: the goals page exists (D055, D079)."""
    row = conn.execute(
        "SELECT 1 FROM goal_docs WHERE kind = 'goal' AND missing_since IS NULL LIMIT 1"
    ).fetchone()
    return row is not None


def goal_tags(conn: sqlite3.Connection) -> set[str]:
    """Tags of goal files with a valid version: FlowSavvy sessions are kept only for these."""
    rows = conn.execute(
        """SELECT doc_key FROM goal_docs
           WHERE kind = 'goal' AND missing_since IS NULL AND good_json IS NOT NULL"""
    )
    return {r["doc_key"] for r in rows}


def apply_goal_sessions(
    conn: sqlite3.Connection,
    records: Iterable[GoalSessionRecord],
    gone: Iterable[GoalSessionGone],
    *,
    known_tags: set[str],
    now: datetime,
) -> int:
    """Store FlowSavvy sessions for known goal tags, and what lookups said about missing ones.
    Absence alone never drops a session (D079): only a `GoalSessionGone` does."""
    kept = [r for r in records if r.tag in known_tags]
    conn.executemany(
        """INSERT INTO goal_sessions (external_id, tag, what, start_at, end_at, minutes, completed,
                                      done_when, first_seen, last_seen, raw_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (external_id) DO UPDATE SET tag = excluded.tag, what = excluded.what,
             start_at = excluded.start_at, end_at = excluded.end_at, minutes = excluded.minutes,
             completed = excluded.completed,
             done_when = COALESCE(excluded.done_when, goal_sessions.done_when),
             last_seen = excluded.last_seen, gone_at = NULL, moved_at = NULL,
             raw_hash = excluded.raw_hash""",
        [
            (
                r.external_id,
                r.tag,
                r.what,
                iso(r.start_at),
                iso(r.end_at),
                r.minutes,
                int(r.completed),
                r.done_when,
                iso(now),
                iso(now),
                r.raw_hash,
            )
            for r in kept
        ],
    )
    for g in gone:
        if g.reason == "completed":
            conn.execute(
                "UPDATE goal_sessions SET completed = 1 WHERE external_id = ?", (g.external_id,)
            )
        elif g.reason == "moved":
            conn.execute(
                """UPDATE goal_sessions SET moved_at = COALESCE(moved_at, ?)
                   WHERE external_id = ? AND completed = 0 AND gone_at IS NULL""",
                (iso(now), g.external_id),
            )
        else:
            conn.execute(
                """UPDATE goal_sessions SET gone_at = COALESCE(gone_at, ?), moved_at = NULL
                   WHERE external_id = ? AND completed = 0""",
                (iso(now), g.external_id),
            )
    return len(kept)


def open_goal_session_ids(
    conn: sqlite3.Connection, start: datetime, end: datetime, *, tags: set[str] | None = None
) -> dict[str, datetime]:
    """Open sessions starting in [start, end) for `tags` (all when None): the ones to look up
    if a fetch no longer lists them. A removed goal's sessions are never looked up."""
    rows = conn.execute(
        """SELECT external_id, start_at, tag FROM goal_sessions
           WHERE completed = 0 AND gone_at IS NULL AND moved_at IS NULL
             AND start_at >= ? AND start_at < ? ORDER BY start_at""",
        (iso(start), iso(end)),
    )
    return {
        r["external_id"]: parse_iso(r["start_at"]) for r in rows if tags is None or r["tag"] in tags
    }


def goal_sessions_between(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[Session]:
    rows = conn.execute(
        """SELECT * FROM goal_sessions WHERE start_at >= ? AND start_at < ?
           ORDER BY start_at, external_id""",
        (iso(start), iso(end)),
    )
    return [
        Session(
            external_id=r["external_id"],
            tag=r["tag"],
            what=r["what"],
            start_at=parse_iso(r["start_at"]),
            end_at=parse_iso(r["end_at"]),
            minutes=r["minutes"],
            completed=bool(r["completed"]),
            done_when=r["done_when"],
            gone_at=parse_iso_or_none(r["gone_at"]),
            moved_at=parse_iso_or_none(r["moved_at"]),
        )
        for r in rows
    ]
