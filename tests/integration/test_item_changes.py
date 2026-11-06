"""apply_merge appends to the item change log (migration 0013, D073)."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from argus.clock import FakeClock
from argus.config import AppConfig
from argus.domain.changes import ChangeKind
from argus.domain.models import ItemKind, Source, SourceItem
from argus.store import repo
from argus.store.db import available_migrations, connect, migrate, transaction

T0 = datetime(2026, 10, 23, 3, 59, tzinfo=UTC)


def src(source: Source = Source.CANVAS_ICAL, ext: str = "1", **kw: object) -> SourceItem:
    base: dict[str, object] = {
        "source": source,
        "external_id": ext,
        "course_code": "CMSC351",
        "title": "Homework 3",
        "kind": ItemKind.ASSIGNMENT,
        "due_at": T0,
        "raw_hash": "h1",
    }
    base.update(kw)
    return SourceItem(**base)  # type: ignore[arg-type]


@pytest.fixture
def ids(db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock) -> dict[str, int]:
    with transaction(db):
        return repo.sync_courses(db, app_config, clock.now())


def merge(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock, *items: SourceItem
) -> None:
    with transaction(db):
        repo.upsert_source_items(db, items, ids, clock.now())
        repo.apply_merge(db, ids, clock.now())


def drop_all(db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock) -> None:
    with transaction(db):
        repo.mark_absent(
            db,
            Source.CANVAS_ICAL,
            course_id=ids["CMSC351"],
            present_external_ids=set(),
            now=clock.now(),
            guard=False,
        )
        repo.apply_merge(db, ids, clock.now())


def kinds(
    db: sqlite3.Connection, since: datetime
) -> list[tuple[ChangeKind, str | None, str | None]]:
    return [
        (
            e.change,
            e.old_due.isoformat() if e.old_due else None,
            e.new_due.isoformat() if e.new_due else None,
        )
        for e in repo.item_changes_since(db, since)
    ]


def test_create_move_remove_restore_are_logged(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    start = clock.now() - timedelta(seconds=1)
    merge(db, ids, clock, src())
    clock.advance(timedelta(minutes=1))
    merge(db, ids, clock, src(due_at=T0 + timedelta(days=1), raw_hash="h2"))
    clock.advance(timedelta(minutes=1))
    merge(db, ids, clock, src(due_at=T0 + timedelta(days=1), raw_hash="h2"))  # nothing changed
    drop_all(db, ids, clock)
    clock.advance(timedelta(minutes=1))
    merge(db, ids, clock, src(raw_hash="h3"))
    d0, d1 = T0.isoformat(), (T0 + timedelta(days=1)).isoformat()
    assert kinds(db, start) == [
        (ChangeKind.NEW, None, d0),
        (ChangeKind.DUE, d0, d1),
        (ChangeKind.REMOVED, d1, d1),
        (ChangeKind.RESTORED, None, d0),
    ]


def test_since_is_exclusive_and_filters_by_time(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    merge(db, ids, clock, src())
    created = clock.now()
    clock.advance(timedelta(minutes=5))
    merge(db, ids, clock, src(due_at=T0 + timedelta(hours=2), raw_hash="h2"))
    assert [k for k, _, _ in kinds(db, created)] == [ChangeKind.DUE]
    assert kinds(db, clock.now()) == []


def test_title_only_edits_are_not_logged(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    merge(db, ids, clock, src())
    after = clock.now()
    clock.advance(timedelta(minutes=1))
    merge(db, ids, clock, src(title="Homework 3 (updated)", raw_hash="h2"))
    assert kinds(db, after) == []


def test_student_override_date_is_logged(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    merge(db, ids, clock, src())
    [it] = repo.list_items(db)
    after = clock.now()
    clock.advance(timedelta(minutes=1))
    moved = T0 + timedelta(days=2)
    with transaction(db):
        repo.set_override(db, it.id, due_at=moved, cancelled=False, reason="test", now=clock.now())
        repo.apply_merge(db, ids, clock.now())
    assert kinds(db, after) == [(ChangeKind.DUE, T0.isoformat(), moved.isoformat())]


def test_cancellation_is_logged_as_removed(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    merge(db, ids, clock, src())
    [it] = repo.list_items(db)
    after = clock.now()
    clock.advance(timedelta(minutes=1))
    with transaction(db):
        repo.set_override(db, it.id, due_at=None, cancelled=True, reason="test", now=clock.now())
        repo.apply_merge(db, ids, clock.now())
    assert [k for k, _, _ in kinds(db, after)] == [ChangeKind.REMOVED]


def test_items_by_ids_includes_removed_items_with_their_last_links(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    merge(db, ids, clock, src(url="https://elms.example.edu/courses/1/assignments/1"))
    [it] = repo.list_items(db)
    drop_all(db, ids, clock)
    [gone] = repo.items_by_ids(db, [it.id])
    assert gone.id == it.id
    assert [link.url for link in gone.links] == ["https://elms.example.edu/courses/1/assignments/1"]
    assert repo.items_by_ids(db, []) == []


def test_latest_change_at(db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock) -> None:
    merge(db, ids, clock, src())
    [it] = repo.list_items(db)
    clock.advance(timedelta(minutes=3))
    merge(db, ids, clock, src(due_at=T0 + timedelta(hours=1), raw_hash="h2"))
    assert repo.latest_change_at(db, [it.id]) == {it.id: clock.now()}
    assert repo.latest_change_at(db, []) == {}


def test_migration_backfills_existing_items(tmp_path: Path, clock: FakeClock) -> None:
    conn = connect(tmp_path / "old.db")
    before = [p for v, p in available_migrations() if v < 13]
    migrate(conn, clock, directory=_dir_with(tmp_path, before))
    conn.execute(
        "INSERT INTO courses (code, section, created_at, updated_at) VALUES ('CMSC351', '0101', 'x', 'x')"
    )
    for title, removed in (("Live", None), ("Gone", "2026-10-18T00:00:00Z")):
        conn.execute(
            """INSERT INTO items (course_id, kind, title, due_at, removed_at, created_at, updated_at)
               VALUES (1, 'assignment', ?, '2026-10-23T03:59:00Z', ?, '2026-10-01T00:00:00Z', 'x')""",
            (title, removed),
        )
    migrate(conn, clock)
    rows = conn.execute(
        "SELECT item_id, change, old_due, new_due, at FROM item_changes ORDER BY item_id, at"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "new", None, "2026-10-23T03:59:00Z", "2026-10-01T00:00:00Z"),
        (2, "new", None, "2026-10-23T03:59:00Z", "2026-10-01T00:00:00Z"),
        (2, "removed", "2026-10-23T03:59:00Z", "2026-10-23T03:59:00Z", "2026-10-18T00:00:00Z"),
    ]
    conn.close()


def _dir_with(tmp_path: Path, files: list[Path]) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    for f in files:
        (d / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return d
