"""SQLite connection, transactions, timestamp encoding, and the migration runner.

Connections run in autocommit mode (`isolation_level=None`). Writes go through
`transaction()`, which opens `BEGIN IMMEDIATE` (or a SAVEPOINT when nested), so a sync cycle
either lands completely or not at all. Open one connection per thread or request; SQLite
in WAL mode handles the concurrency.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from argus.clock import Clock, ensure_utc

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_savepoints = count()


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        name = f"sp{next(_savepoints)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def iso(dt: datetime) -> str:
    """Encode an instant for storage: UTC, second precision, trailing Z."""
    return ensure_utc(dt).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_or_none(dt: datetime | None) -> str | None:
    return None if dt is None else iso(dt)


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"stored timestamp without zone: {s!r}")
    return dt.astimezone(UTC)


def parse_iso_or_none(s: str | None) -> datetime | None:
    return None if s is None else parse_iso(s)


def available_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for p in directory.glob("*.sql"):
        m = _MIGRATION_NAME.match(p.name)
        if not m:
            raise ValueError(f"bad migration file name: {p.name}")
        found.append((int(m.group(1)), p))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"migrations must be numbered 1..n without gaps: {versions}")
    return found


def migrate(conn: sqlite3.Connection, clock: Clock, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations in order, each atomically. Returns the versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied: list[int] = []
    for version, path in available_migrations(directory):
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        # Safe to inline: the name matched _MIGRATION_NAME and the timestamp is generated here.
        record = (
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES "  # noqa: S608
            f"({version}, '{path.name}', '{iso(clock.now())}');"
        )
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\n{record}\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def open_db(path: Path | str, clock: Clock) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn, clock)
    return conn
