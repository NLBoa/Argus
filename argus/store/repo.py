"""Repository: every SQL statement Argus runs lives here. Functions take a connection.

Callers wrap multi-step writes in `db.transaction`. Functions never commit themselves.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from argus.config import AppConfig
from argus.domain.merge import merge_sources, resolve_item
from argus.domain.models import (
    AuthorRole,
    CourseInfo,
    Item,
    ItemKind,
    ItemLink,
    ItemStatus,
    MeetingRecord,
    PostRecord,
    ResolvedItem,
    Source,
    SourceItem,
    SourceKey,
    Weekday,
)
from argus.domain.status import StatusFacts, derive_status
from argus.store.db import iso, iso_or_none, parse_iso, parse_iso_or_none, transaction

# ---------------------------------------------------------------- helpers


def _b(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _ob(v: object) -> bool | None:
    return None if v is None else bool(v)


def sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- courses


def sync_courses(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> dict[str, int]:
    """Upsert configured courses; deactivate ones no longer configured. Returns code -> id."""
    ts = iso(now)
    for c in config.courses:
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
    codes = [c.code for c in config.courses]
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
        )
        if row is None:
            conn.execute(
                """INSERT INTO item_sources (course_id, title, kind, due_at, release_at,
                     late_due_at, all_day, points, submitted, graded, description,
                     question_count, url, raw_hash, source, external_id, present,
                     first_seen_at, last_seen_at, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (*values, it.source.value, it.external_id, ts, ts, ts),
            )
            changed += 1
        elif row["raw_hash"] != it.raw_hash or not row["present"]:
            conn.execute(
                """UPDATE item_sources SET course_id = ?, title = ?, kind = ?, due_at = ?,
                     release_at = ?, late_due_at = ?, all_day = ?, points = ?, submitted = ?,
                     graded = ?, description = ?, question_count = ?, url = ?, raw_hash = ?,
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


def mark_absent(
    conn: sqlite3.Connection,
    source: Source,
    *,
    course_id: int,
    present_external_ids: set[str],
    now: datetime,
    not_before: datetime | None = None,
) -> int:
    """After a complete fetch of (source, course), mark records it no longer lists as absent.

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
    for group in merge_sources(records, links):
        members = [by_key[k] for k in group.keys]
        resolved = resolve_item(members)
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
        r["id"]
        for r in conn.execute("SELECT id FROM items WHERE removed_at IS NULL")
        if r["id"] not in live
    ]
    for item_id in orphans:
        conn.execute(
            "UPDATE items SET removed_at = ?, updated_at = ? WHERE id = ?", (ts, ts, item_id)
        )
    return created


def refresh_statuses(conn: sqlite3.Connection, now: datetime) -> int:
    """Recompute every live item's status. Returns how many changed."""
    changed = 0
    rows = conn.execute(
        """SELECT id, kind, due_at, late_due_at, submitted, graded, user_done_at, started_at,
                  status FROM items WHERE removed_at IS NULL"""
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
    conn: sqlite3.Connection, item_ids: Sequence[int]
) -> dict[int, tuple[ItemLink, ...]]:
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    out: dict[int, list[ItemLink]] = {}
    for r in conn.execute(
        f"SELECT item_id, source, url FROM item_sources WHERE present = 1 AND item_id IN ({marks})"  # noqa: S608
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
             sent_at = CASE WHEN ? THEN ? ELSE sent_at END
           WHERE id = ?""",
        ("sent" if ok else "failed", channel, error, int(ok), iso(now), notification_id),
    )


def notification_keys(conn: sqlite3.Connection) -> set[str]:
    return {r["key"] for r in conn.execute("SELECT key FROM notification_keys")}


def retryable_notifications(
    conn: sqlite3.Connection, now: datetime, max_attempts: int = 3
) -> list[NotificationRow]:
    """Failed (or never-finished) sends that are still relevant."""
    rows = conn.execute(
        """SELECT * FROM notifications
           WHERE status IN ('failed', 'pending') AND attempts < ?
             AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY id""",
        (max_attempts, iso(now)),
    ).fetchall()
    return [_row_to_notification(r) for r in rows]


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
