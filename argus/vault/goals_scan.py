"""Read the goals folder (D079): `Goals/<Tag>.md` goal files and `Goals/Weeks/YYYY-Www.md` week
files. Argus only reads them. The vault cycle stores each file's last valid parse and its current
problem; `argus check-goals` uses the same reading to validate files before they're pushed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from argus.domain.goals import (
    DocError,
    GoalDoc,
    WeekDoc,
    check_goal_set,
    extract_block,
    parse_doc,
    week_problems,
)
from argus.logging import get_logger
from argus.store import repo
from argus.store.db import transaction

log = get_logger(__name__)

WEEKS = "Weeks"
MAX_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class ReadDoc:
    rel: str  # e.g. Goals/Projects.md
    kind: str  # "goal" | "week": from the file's folder
    text_hash: str
    result: GoalDoc | WeekDoc | DocError


@dataclass(frozen=True, slots=True)
class ScanSummary:
    read: int
    problems: int


def _files(base: Path) -> list[tuple[Path, str]]:
    if not base.is_dir() or base.is_symlink():
        return []
    out = [(p, "goal") for p in sorted(base.glob("*.md"))]
    weeks = base / WEEKS
    if weeks.is_dir() and not weeks.is_symlink():
        out += [(p, "week") for p in sorted(weeks.glob("*.md"))]
    return [(p, kind) for p, kind in out if p.is_file() and not p.is_symlink()]


def read_folder(base: Path, prefix: str) -> list[ReadDoc]:
    """Every goal and week file under `base` that has an argus block (or can't be read). `prefix`
    is the folder's vault-relative path, for `rel`. Never raises for a bad file."""
    out: list[ReadDoc] = []
    for path, kind in _files(base):
        rel = f"{prefix}/{path.relative_to(base).as_posix()}"
        try:
            raw = path.read_bytes()
        except OSError as e:
            log.warning("goal file unreadable", extra={"path": rel, "error": type(e).__name__})
            continue
        text_hash = hashlib.sha256(raw).hexdigest()
        if len(raw) > MAX_BYTES:
            log.warning("goal file too big; skipped", extra={"path": rel, "bytes": len(raw)})
            err = DocError(None, "", "This file is over 256 KB, so Argus skips it")
            out.append(ReadDoc(rel, kind, text_hash, err))
            continue
        text = raw.decode("utf-8", errors="replace")
        block = extract_block(text)
        if block is None:
            continue  # plain notes: no argus block
        result: GoalDoc | WeekDoc | DocError | None
        if isinstance(block, tuple) and block[0] != kind:
            where = f"{prefix}/{WEEKS}/" if block[0] == "week" else f"{prefix}/, not {WEEKS}/"
            result = DocError(block[2] - 1, "", f"{block[0].title()} files go in {where}")
        else:
            result = parse_doc(rel, text)
        if result is not None:
            out.append(ReadDoc(rel, kind, text_hash, result))
    dupes = check_goal_set({d.rel: d.result for d in out if isinstance(d.result, GoalDoc)})
    return [ReadDoc(d.rel, d.kind, d.text_hash, dupes.get(d.rel, d.result)) for d in out]


def scan_goals(conn: sqlite3.Connection, root: Path, folder: str, now: datetime) -> ScanSummary:
    """Record the goals folder's files. A bad file keeps its last valid version beside the
    error; a file that left the vault (or lost its block) is marked missing."""
    docs = read_folder(root / folder, folder)
    problems = 0
    with transaction(conn):
        for d in docs:
            if isinstance(d.result, DocError):
                problems += 1
                log.warning(
                    "goal file has a problem",
                    extra={"path": d.rel, "line": d.result.line, "field": d.result.path},
                )
                repo.upsert_goal_doc(
                    conn,
                    path=d.rel,
                    kind=d.kind,
                    text_hash=d.text_hash,
                    parsed=None,
                    error=d.result,
                    now=now,
                )
            else:
                repo.upsert_goal_doc(
                    conn,
                    path=d.rel,
                    kind=d.kind,
                    text_hash=d.text_hash,
                    parsed=d.result,
                    error=None,
                    now=now,
                )
        repo.mark_goal_docs_missing(conn, {d.rel for d in docs}, now)
    return ScanSummary(read=len(docs), problems=problems)


@dataclass(frozen=True, slots=True)
class CheckReport:
    docs: list[ReadDoc]
    problems: list[tuple[str, DocError]]  # (rel, problem), in path order


def check_dir(base: Path, prefix: str) -> CheckReport:
    """Validate a goals folder the way the Pi will read it, plus the checks the page does at
    build time (week files naming unknown goals or topics)."""
    docs = read_folder(base, prefix)
    problems = [(d.rel, d.result) for d in docs if isinstance(d.result, DocError)]
    goals = {d.result.tag: d.result for d in docs if isinstance(d.result, GoalDoc)}
    for d in docs:
        if isinstance(d.result, WeekDoc):
            problems += [(d.rel, p) for p in week_problems(d.result, goals)]
    return CheckReport(docs, sorted(problems, key=lambda p: p[0]))
