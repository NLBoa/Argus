"""The vault cycle (every 15 minutes, and right after a proposal is approved).

Pull → read the confusion inbox → write every note through `VaultWriter` → commit → push.
Each run is recorded in `vault_runs`; three failed runs in a row raise a system alert. The
non-AI fallback is everything here: course pages, module notes listing materials, assignment
and exam notes, Today, and the weekly note need no AI; only concept notes and practice do.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from argus.capabilities import Capabilities, Feature
from argus.clock import Clock, local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind, MeetingRecord, Weekday
from argus.domain.status import awaiting_confirmation
from argus.domain.titles import normalize_title
from argus.estimates import effective_estimates
from argus.exams import PracticeRow, concept_readiness, practice_items
from argus.logging import get_logger
from argus.planning import blocks_between
from argus.store import repo
from argus.store.db import iso, parse_iso, transaction
from argus.vault import layout, render
from argus.vault.gitsync import GitSync, SyncResult
from argus.vault.writer import VaultWriter, _atomic_write

log = get_logger(__name__)
CYCLE_EVERY = timedelta(minutes=15)
NOTE_WINDOW = (timedelta(days=14), timedelta(days=60))  # items this far back / ahead get notes
_DAYS = {Weekday.MON: "Mon", Weekday.TUE: "Tue", Weekday.WED: "Wed", Weekday.THU: "Thu",
         Weekday.FRI: "Fri", Weekday.SAT: "Sat", Weekday.SUN: "Sun"}  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    {"the", "a", "an", "of", "and", "or", "in", "on", "to", "for", "is", "what", "how", "why"}
)


def _wall(hour: int, minute: int) -> str:
    h12 = hour % 12 or 12
    return (
        f"{h12}:{minute:02d} {'am' if hour < 12 else 'pm'}"
        if minute
        else f"{h12} {'am' if hour < 12 else 'pm'}"
    )


def _meeting(m: MeetingRecord) -> str:
    where = " ".join(x for x in (m.building, m.room) if x)
    days = "/".join(_DAYS[d] for d in m.days)
    line = f"{m.kind.capitalize()} {days} {_wall(m.start.hour, m.start.minute)} to {_wall(m.end.hour, m.end.minute)}"
    return f"{line}, {where}" if where else line


def item_note_path(item: Item) -> str | None:
    if item.kind is ItemKind.EXAM:
        return layout.exam_note(item.course_code, item.title)
    if item.kind in (ItemKind.ASSIGNMENT, ItemKind.QUIZ):
        return layout.assignment_note(item.course_code, item.title)
    return None


def _line(item: Item, status: str) -> render.ItemLine:
    url = next((link.url for link in item.links if link.url), None)
    return render.ItemLine(
        item.title, item.kind.value, item.due_at, status, item_note_path(item), url
    )


# ---------------------------------------------------------------- confusion inbox


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _STOP}


def match_concept(text: str, concepts: list[tuple[int, str, str]]) -> int | None:
    """The concept a confusion line is about: most of the concept name's words appear in it.
    `concepts` is (id, course code, name); a course code in the line narrows the search."""
    line = _words(text)
    codes = {c for _, c, _ in concepts if c.casefold() in text.casefold()}
    best: tuple[float, int] | None = None
    for cid, code, name in concepts:
        if codes and code not in codes:
            continue
        words = _words(normalize_title(name)) or _words(name)
        if not words:
            continue
        score = len(words & line) / len(words)
        if score >= 0.5 and (best is None or score > best[0]):
            best = (score, cid)
    return best[1] if best else None


def scan_inbox(conn: sqlite3.Connection, root: Path, now: datetime) -> list[int]:
    """Record new lines from Argus/Inbox. Argus never edits or deletes them. Returns new entry ids."""
    inbox = root / layout.INBOX_DIR
    if not inbox.is_dir():
        return []
    concepts = [
        (r["id"], r["code"], r["name"])
        for r in conn.execute(
            "SELECT k.id, c.code, k.name FROM concepts k JOIN courses c ON c.id = k.course_id"
            " WHERE c.active = 1 AND k.verified_at IS NOT NULL"
        )
    ]
    new: list[int] = []
    with transaction(conn):
        for path in sorted(inbox.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            if rel == layout.INBOX_README:
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                text = raw.strip().lstrip("-*").strip()
                if len(text) < 4 or text.startswith(("#", "---", "%%")):
                    continue
                h = hashlib.sha256(f"{rel}\0{text}".encode()).hexdigest()
                concept = match_concept(text, concepts)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO inbox_entries (path, text_hash, text, concept_id, processed_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (rel, h, text, concept, iso(now)),
                )
                if cur.rowcount and cur.lastrowid is not None:
                    new.append(cur.lastrowid)
                    if concept is not None:
                        conn.execute(
                            "UPDATE concepts SET priority = priority + 1 WHERE id = ?", (concept,)
                        )
    return new


def pending_practice(conn: sqlite3.Connection, limit: int = 3) -> list[tuple[int, int, str]]:
    """Matched inbox entries still waiting for targeted practice: (entry id, concept id, text)."""
    rows = conn.execute(
        "SELECT id, concept_id, text FROM inbox_entries WHERE concept_id IS NOT NULL AND practice_at IS NULL"
        " ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r["id"], r["concept_id"], r["text"]) for r in rows]


def mark_practiced(conn: sqlite3.Connection, entry_id: int, now: datetime) -> None:
    with transaction(conn):
        conn.execute("UPDATE inbox_entries SET practice_at = ? WHERE id = ?", (iso(now), entry_id))


# ---------------------------------------------------------------- building notes


@dataclass
class Builder:
    conn: sqlite3.Connection
    config: AppConfig
    clock: Clock
    writer: VaultWriter
    caps: Capabilities  # sections a missing source can't support are left out (D055)
    _logged: int = 0  # allowed edits already recorded

    def build(self) -> str:
        """Write every note. Returns the commit message."""
        now = self.clock.now()
        self.writer.expire()
        items = repo.list_items(
            self.conn, due_after=now - NOTE_WINDOW[0], due_before=now + NOTE_WINDOW[1]
        )
        for course in self.config.courses:
            if repo.get_course(self.conn, course.code) is not None:
                self._course(course.code, [i for i in items if i.course_code == course.code], now)
        self._today(items, now)
        self._record_allowed_edits(now)  # before the weekly note, so it lists this cycle's edits
        self._weekly(items, now)
        if self.caps.has(Feature.STUDY_GENERATION):  # the inbox promises targeted practice
            self._write_once(layout.INBOX_README, render.INBOX_README_TEXT)
        self._proposals()
        self._record_allowed_edits(now)
        written = len(set(self.writer.log.written))
        proposed = len(set(self.writer.log.proposed))
        msg = f"Argus: update {written} note{'s' if written != 1 else ''}"
        return msg + (f", propose changes to {proposed}" if proposed else "")

    # -- helpers

    def _status(self, item: Item, now: datetime) -> str:
        """The status a note shows. Past-due work no source reports submissions for is
        "not marked done", never "upcoming" or "missed" (D056)."""
        tracked = item.tracked_by(self.caps.tracking_sources)
        return (
            "not_marked_done"
            if awaiting_confirmation(item, now, tracked=tracked)
            else item.status.value
        )

    def _write_once(self, rel: str, text: str) -> None:
        """Files outside the region model (the inbox README, canvases): written only if absent or
        still exactly what Argus last wrote; a student-edited copy is left alone."""
        current = self.writer.read(rel)
        h = hashlib.sha256(text.encode()).hexdigest()
        stored = self.writer.stored_hashes(rel).get("file")
        if current == text:
            return
        if current is not None and (
            stored is None or hashlib.sha256(current.encode()).hexdigest() != stored
        ):
            return
        _atomic_write(self.writer.root / rel, text)
        self.writer.log.written.append(rel)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO vault_regions (path, region_id, hash, written_at) VALUES (?, 'file', ?, ?)"
                " ON CONFLICT (path, region_id) DO UPDATE SET hash = excluded.hash, written_at = excluded.written_at",
                (rel, h, iso(self.clock.now())),
            )

    def _concepts(self, code: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT k.*, COALESCE(m.name, '') AS module_name, COALESCE(m.position, 9999) AS module_pos
               FROM concepts k JOIN courses c ON c.id = k.course_id
               LEFT JOIN modules m ON m.course_id = k.course_id AND m.canvas_id = k.module_id
               WHERE c.code = ? AND k.verified_at IS NOT NULL ORDER BY module_pos, k.name""",
            (code,),
        ).fetchall()

    # -- per course

    def _course(self, code: str, items: list[Item], now: datetime) -> None:
        cfg = self.config.course(code)
        row = repo.get_course(self.conn, code)
        assert cfg is not None
        assert row is not None
        concepts = self._concepts(code)
        by_slug = {k["slug"]: layout.concept_note(code, k["name"]) for k in concepts}
        practice = practice_items(self.conn, [k["id"] for k in concepts])
        for k in concepts:
            mine = [p for p in practice if p.concept_id == k["id"] and p.kind == "question"]
            self.writer.write(
                by_slug[k["slug"]],
                render.concept_note(
                    render.ConceptData(
                        course=code,
                        module=k["module_name"] or None,
                        name=k["name"],
                        body=json.loads(k["body"]),
                        sources=json.loads(k["sources"]),
                        verified_at=parse_iso(k["verified_at"]),
                        practice=[(p.prompt, p.answer, p.source_ref) for p in mine],
                        related=[
                            by_slug[s]
                            for s in json.loads(k["related"])
                            if s in by_slug and by_slug[s] != by_slug[k["slug"]]
                        ],
                    )
                ),
            )
        modules = []
        course_caps = self.caps.course(code)
        for sm in repo.list_modules(self.conn, code) if course_caps.modules else ():
            path = layout.module_note(code, sm.record.name)
            files = [
                (i.title, i.url)
                for i in sm.record.items
                if i.url and i.type in ("File", "Page", "ExternalUrl")
            ]
            notes = [by_slug[k["slug"]] for k in concepts if k["module_id"] == sm.record.canvas_id]
            self.writer.write(path, render.module_note(code, sm.record.name, files, notes))
            modules.append((sm.record.name, path))
        estimates = effective_estimates(self.conn, now)
        # Study artifacts (flashcards, concept map, exam practice) only where they can exist.
        study = bool(concepts) or self.caps.has(Feature.STUDY_GENERATION)
        exams = []
        for item in items:
            note_path = item_note_path(item)
            if note_path is None:
                continue
            if item.kind is ItemKind.EXAM:
                self._exam(item, note_path, concepts, by_slug, study=study)
                exams.append((item.title, note_path))
                continue
            info = self.conn.execute(
                "SELECT scope, logistics FROM items WHERE id = ?", (item.id,)
            ).fetchone()
            est = estimates.get(item.id)
            self.writer.write(
                note_path,
                render.assignment_note(
                    render.AssignmentData(
                        course=code,
                        title=item.title,
                        kind=item.kind.value,
                        due_at=item.due_at,
                        status=self._status(item, now),
                        estimate=f"{est.p50:.1f} h (could take {est.p80:.1f} h)" if est else None,
                        links=[
                            (link.source.family.capitalize(), link.url)
                            for link in item.links
                            if link.url
                        ],
                        scope=info["scope"],
                        logistics=info["logistics"],
                    )
                ),
            )
        base = self.config.canvas.base_url.rstrip("/")
        links = []
        if cfg.canvas_id:
            links.append(("ELMS", f"{base}/courses/{cfg.canvas_id}"))
        if cfg.gradescope_id:
            links.append(("Gradescope", f"https://www.gradescope.com/courses/{cfg.gradescope_id}"))
        if cfg.piazza_network_id:
            links.append(("Piazza", f"https://piazza.com/class/{cfg.piazza_network_id}"))
        grade = None
        if course_caps.modules and row.grade_score is not None:
            grade = f"{row.grade_score:g}%" + (f" ({row.grade_letter})" if row.grade_letter else "")
        self.writer.write(
            layout.course_note(code),
            render.course_note(
                render.CourseData(
                    code=code,
                    title=row.title,
                    meetings=[
                        _meeting(m) for m in repo.list_meetings(self.conn) if m.course_code == code
                    ],
                    links=links,
                    items=[
                        _line(i, self._status(i, now))
                        for i in items
                        if i.kind is not ItemKind.EVENT and (i.due_at is None or i.due_at >= now)
                    ],
                    modules=modules,
                    exams=exams,
                    grade=grade,
                    concept_map=study,
                )
            ),
        )
        if not study:
            return
        flash_path = layout.flashcards(code)
        slugs = {k["id"]: k["slug"] for k in concepts}
        self.writer.write(
            flash_path,
            render.flashcards_note(
                code,
                [(p.prompt, p.answer, slugs[p.concept_id]) for p in practice],
                self.writer.read(flash_path),
            ),
        )
        positions = sorted({k["module_pos"] for k in concepts})
        self._write_once(
            layout.concept_map(code),
            render.concept_map_canvas(
                [
                    (by_slug[k["slug"]], k["name"], positions.index(k["module_pos"]))
                    for k in concepts
                ],
                [
                    (by_slug[k["slug"]], by_slug[s])
                    for k in concepts
                    for s in json.loads(k["related"])
                    if s in by_slug
                ],
            ),
        )

    def _exam(
        self,
        item: Item,
        path: str,
        concepts: list[sqlite3.Row],
        by_slug: dict[str, str],
        *,
        study: bool,
    ) -> None:
        row = self.conn.execute("SELECT * FROM exams WHERE item_id = ?", (item.id,)).fetchone()
        ids = json.loads(row["concept_ids"]) if row else []
        in_scope = [k for k in concepts if k["id"] in ids]
        ready = concept_readiness(self.conn, ids)
        sessions = self.conn.execute(
            "SELECT start_at, label FROM plan_blocks WHERE kind = 'review' AND item_id = ? ORDER BY start_at",
            (item.id,),
        ).fetchall()
        practice = [p for p in practice_items(self.conn, ids) if p.kind == "question"]
        self.writer.write(
            path,
            render.exam_note(
                render.ExamData(
                    course=item.course_code,
                    title=item.title,
                    at=item.due_at,
                    scope=row["scope"] if row else None,
                    logistics=row["logistics"] if row else None,
                    concepts=[
                        render.ExamConcept(k["name"], by_slug[k["slug"]], ready[k["id"]].value)
                        for k in in_scope
                    ],
                    sessions=[(parse_iso(s["start_at"]), s["label"] or "Review") for s in sessions],
                    practice=[(p.prompt, p.answer, p.concept) for p in _interleave(practice)][:20],
                    study=study,
                )
            ),
        )

    # -- daily and weekly

    def _today(self, items: list[Item], now: datetime) -> None:
        day = to_local(now).date()
        start, end = local_datetime(day, 0), local_datetime(day + timedelta(days=1), 0)
        by_id = {i.id: i for i in items}
        sessions = []
        for b in blocks_between(self.conn, start, end):
            item = by_id.get(b.item_id) if b.item_id is not None else None
            label = b.label or (f"{item.course_code}: {item.title}" if item else "Work session")
            sessions.append((b.start, b.end, label, item_note_path(item) if item else None))
        due = [
            _line(i, self._status(i, now))
            for i in items
            if i.due_at is not None and start <= i.due_at < end and i.kind is not ItemKind.EVENT
        ]
        self.writer.write(layout.REVIEW_TODAY, render.review_today(day, sessions, due))

    def _weekly(self, items: list[Item], now: datetime) -> None:
        day = to_local(now).date()
        monday = day - timedelta(days=day.weekday())
        start, end = local_datetime(monday, 0), local_datetime(monday + timedelta(days=7), 0)
        due = [
            _line(i, self._status(i, now))
            for i in items
            if i.due_at is not None and start <= i.due_at < end and i.kind is not ItemKind.EVENT
        ]
        changes = [
            f"{to_local(parse_iso(r['created_at'])):%a %b %d}: {layout.link(r['path'])}, {r['change']}"
            for r in self.conn.execute(
                "SELECT path, change, created_at FROM vault_edit_log WHERE created_at >= ? AND created_at < ?"
                " ORDER BY created_at, id",
                (iso(start), iso(end)),
            )
        ]
        self.writer.write(layout.weekly(day), render.weekly_note(day, due, changes))

    def _proposals(self) -> None:
        rows = [(p.id, p.path, p.reason, p.diff) for p in self.writer.proposals()]
        self.writer.write(
            layout.PROPOSALS, render.proposals_note(rows, self.config.notifications.dashboard_url)
        )

    def _record_allowed_edits(self, now: datetime) -> None:
        new, self._logged = (
            self.writer.log.allowed_edits[self._logged :],
            len(self.writer.log.allowed_edits),
        )
        entries = [
            (path, change)
            for path, changes in new
            if not path.startswith("Weekly/")  # the weekly note's own log would grow forever
            for change in changes
        ]
        if entries:
            with transaction(self.conn):
                self.conn.executemany(
                    "INSERT INTO vault_edit_log (path, change, created_at) VALUES (?, ?, ?)",
                    [(p, c, iso(now)) for p, c in entries],
                )


def _interleave(practice: list[PracticeRow]) -> list[PracticeRow]:
    """Round-robin across concepts so the practice exam mixes topics (interleaving)."""
    groups: dict[int, list[PracticeRow]] = {}
    for p in practice:
        groups.setdefault(p.concept_id, []).append(p)
    out = []
    queues = list(groups.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


# ---------------------------------------------------------------- the cycle


def last_run(conn: sqlite3.Connection) -> datetime | None:
    r = conn.execute(
        "SELECT started_at FROM vault_runs ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return parse_iso(r[0]) if r else None


def due(conn: sqlite3.Connection, now: datetime) -> bool:
    last = last_run(conn)
    return last is None or now - last >= CYCLE_EVERY


def run_cycle(
    conn: sqlite3.Connection,
    config: AppConfig,
    clock: Clock,
    *,
    caps: Capabilities,
    before: Callable[[VaultWriter], None] | None = None,
) -> SyncResult:
    """One vault cycle. `before` runs after the pull and before the notes are written (used to
    apply an approved proposal to the latest version of the note)."""
    started = clock.now()
    root = config.paths.vault_dir
    writer = VaultWriter(root, conn, clock)

    def write() -> str:
        scan_inbox(conn, root, clock.now())
        if before is not None:
            before(writer)
        return Builder(conn, config, clock, writer, caps).build()

    lock_path = config.paths.data_dir / "vault.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the worker and the web app never run git at once
        try:
            result = GitSync(root).cycle(write)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    with transaction(conn):
        conn.execute(
            "INSERT INTO vault_runs (started_at, finished_at, status, error, commit_sha, files_written)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                iso(started),
                iso(clock.now()),
                result.status,
                result.error,
                result.commit,
                result.files,
            ),
        )
    if result.status != "ok":
        log.warning(
            "vault cycle did not finish", extra={"status": result.status, "error": result.error}
        )
    return result


@dataclass(frozen=True)
class VaultHealth:
    last_ok: datetime | None
    last_status: str | None
    last_error: str | None
    failures: int
    failing_since: datetime | None
    open_proposals: int


def health(conn: sqlite3.Connection) -> VaultHealth:
    runs = conn.execute(
        "SELECT * FROM vault_runs ORDER BY started_at DESC, id DESC LIMIT 50"
    ).fetchall()
    streak = []
    for r in runs:
        if r["status"] != "error":
            break
        streak.append(r)
    ok = next((r for r in runs if r["status"] == "ok"), None)
    open_count = conn.execute("SELECT COUNT(*) FROM proposals WHERE status = 'open'").fetchone()[0]
    return VaultHealth(
        last_ok=parse_iso(ok["finished_at"]) if ok else None,
        last_status=runs[0]["status"] if runs else None,
        last_error=runs[0]["error"] if runs else None,
        failures=len(streak),
        failing_since=parse_iso(streak[-1]["started_at"]) if streak else None,
        open_proposals=int(open_count),
    )
