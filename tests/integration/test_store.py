import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from argus.clock import FakeClock
from argus.config import AppConfig, CourseConfig
from argus.domain.models import (
    AuthorRole,
    CourseInfo,
    ItemKind,
    ItemStatus,
    MeetingRecord,
    PostRecord,
    Source,
    SourceItem,
    Weekday,
)
from argus.store import repo
from argus.store.db import (
    available_migrations,
    connect,
    iso,
    migrate,
    parse_iso,
    schema_version,
    transaction,
)
from tests.caps import EVERY_SOURCE

T0 = datetime(2026, 10, 23, 3, 59, tzinfo=UTC)


def item(source: Source = Source.CANVAS_ICAL, ext: str = "1", **kw: object) -> SourceItem:
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


# ---------------------------------------------------------------- db


def test_migrations_apply_once_and_enable_wal_and_fks(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    assert schema_version(db) == len(available_migrations())
    assert migrate(db, clock) == []
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO meetings (course_id, source, kind, days, start_local, end_local,"
            " updated_at)"
            " VALUES (999, 'manual', 'lecture', '0', '09:00', '10:00', 'x')"
        )


def test_failed_migration_rolls_back(tmp_path: Path, clock: FakeClock) -> None:
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE b (x INTEGER);\nTHIS IS NOT SQL;")
    conn = connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, clock, tmp_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables
    assert "b" not in tables
    assert schema_version(conn) == 1


@pytest.mark.parametrize(
    "names",
    [["0001_a.sql", "0003_c.sql"], ["0001_a.sql", "2_b.sql"], ["0002_a.sql"]],
)
def test_migration_naming_is_validated(tmp_path: Path, names: list[str]) -> None:
    for n in names:
        (tmp_path / n).write_text("SELECT 1;")
    with pytest.raises(ValueError):  # noqa: PT011
        available_migrations(tmp_path)


def test_transactions_and_savepoints(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(RuntimeError), transaction(db):
        db.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError
    with transaction(db):
        db.execute("INSERT INTO t VALUES (2)")
        with pytest.raises(RuntimeError), transaction(db):
            db.execute("INSERT INTO t VALUES (3)")
            raise RuntimeError
        with transaction(db):
            db.execute("INSERT INTO t VALUES (4)")
    assert [r[0] for r in db.execute("SELECT x FROM t ORDER BY x")] == [2, 4]


def test_iso_round_trip_and_naive_rejected() -> None:
    dt = datetime(2026, 10, 23, 3, 59, 12, 999, tzinfo=UTC)
    assert iso(dt) == "2026-10-23T03:59:12Z"
    assert parse_iso(iso(dt)) == dt.replace(microsecond=0)
    with pytest.raises(ValueError, match="without zone"):
        parse_iso("2026-10-23T03:59:00")


def test_memory_db_connects() -> None:
    conn = connect(":memory:")
    assert conn.execute("SELECT 1").fetchone()[0] == 1


# ---------------------------------------------------------------- courses & meetings


def test_sync_courses_deactivates_removed(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock
) -> None:
    with transaction(db):
        ids = repo.sync_courses(db, app_config, clock.now())
    assert set(ids) == {"CMSC351", "MATH240"}
    smaller = app_config.model_copy(
        update={"courses": [CourseConfig(code="CMSC351", section="0101")]}
    )
    with transaction(db):
        assert set(repo.sync_courses(db, smaller, clock.now())) == {"CMSC351"}
    assert set(repo.course_ids(db, active_only=False)) == {"CMSC351", "MATH240"}
    with transaction(db):
        assert set(repo.sync_courses(db, AppConfig(), clock.now())) == set()


def test_meetings_replace_and_list(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    m = MeetingRecord(
        "CMSC351", "lecture", (Weekday.TUE, Weekday.THU), time(9, 30), time(10, 45), "IRB", "1101"
    )
    with transaction(db):
        repo.replace_meetings(db, ids["CMSC351"], "umdio", [m, m], clock.now())
        repo.replace_meetings(db, ids["CMSC351"], "umdio", [m], clock.now())
        repo.update_course_info(db, CourseInfo("CMSC351", "Algorithms", ("Prof",)), clock.now())
    assert repo.list_meetings(db) == [m]
    row = db.execute("SELECT title, instructors FROM courses WHERE code = 'CMSC351'").fetchone()
    assert row["title"] == "Algorithms"
    assert row["instructors"] == '["Prof"]'


# ---------------------------------------------------------------- items


def test_upsert_counts_changes_and_marks_seen(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    with transaction(db):
        assert (
            repo.upsert_source_items(
                db, [item(), item(course_code="HIST289", ext="x")], ids, clock.now()
            )
            == 1
        )
        assert repo.upsert_source_items(db, [item()], ids, clock.now()) == 0
        assert (
            repo.upsert_source_items(db, [item(raw_hash="h2", title="HW 3 (v2)")], ids, clock.now())
            == 1
        )
    records, links = repo.present_source_items(db)
    assert [r.title for r in records] == ["HW 3 (v2)"]
    assert links == {}


def test_merge_creates_updates_and_removes(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    gs = item(Source.GRADESCOPE, "g1", title="HW3", due_at=T0 - timedelta(minutes=29), raw_hash="g")
    with transaction(db):
        repo.upsert_source_items(db, [item(), gs], ids, clock.now())
        created = repo.apply_merge(db, ids, clock.now())
    assert len(created) == 1
    [it] = repo.list_items(db)
    assert it.id == created[0]
    assert it.due_at == T0 - timedelta(minutes=29)
    assert it.due_source == Source.GRADESCOPE
    assert {link.source for link in it.links} == {Source.CANVAS_ICAL, Source.GRADESCOPE}
    assert [s for s, _ in it.date_conflict] == [Source.GRADESCOPE, Source.CANVAS_ICAL]

    # Student resolves the conflict; an unrelated re-merge keeps it resolved.
    db.execute("UPDATE items SET conflict_resolved_at = '2026-10-19T13:00:00Z'")
    with transaction(db):
        assert repo.apply_merge(db, ids, clock.now()) == []
    assert repo.get_item(db, it.id).date_conflict == ()  # type: ignore[union-attr]

    # Gradescope moves its date: a new disagreement must be resolved again.
    with transaction(db):
        moved = item(
            Source.GRADESCOPE, "g1", title="HW3", due_at=T0 - timedelta(minutes=40), raw_hash="g2"
        )
        repo.upsert_source_items(db, [moved], ids, clock.now())
        repo.apply_merge(db, ids, clock.now())
    again = repo.get_item(db, it.id)
    assert again is not None
    assert again.date_conflict != ()
    assert again.due_at == T0 - timedelta(minutes=40)

    # Every source disappears: the item is removed (soft), then restored when one returns.
    with transaction(db):
        repo.mark_absent(
            db,
            Source.CANVAS_ICAL,
            course_id=ids["CMSC351"],
            present_external_ids=set(),
            now=clock.now(),
            guard=False,  # these tests are about removal itself
        )
        repo.mark_absent(
            db,
            Source.GRADESCOPE,
            course_id=ids["CMSC351"],
            present_external_ids=set(),
            now=clock.now(),
            guard=False,  # these tests are about removal itself
        )
        repo.apply_merge(db, ids, clock.now())
    assert repo.list_items(db) == []
    with transaction(db):
        repo.upsert_source_items(db, [item()], ids, clock.now())
        assert repo.apply_merge(db, ids, clock.now()) == []
    assert [i.id for i in repo.list_items(db)] == [it.id]


def test_mark_absent_respects_window(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    old = item(ext="old", due_at=clock.now() - timedelta(days=40))
    undated = item(ext="undated", due_at=None)
    with transaction(db):
        repo.upsert_source_items(db, [old, undated, item(ext="new")], ids, clock.now())
        gone = repo.mark_absent(
            db,
            Source.CANVAS_ICAL,
            course_id=ids["CMSC351"],
            present_external_ids={"new"},
            now=clock.now(),
            not_before=clock.now() - timedelta(days=29),
        )
    assert gone == 1  # only the undated one: "old" fell out of the feed window
    records, _ = repo.present_source_items(db)
    assert {r.external_id for r in records} == {"old", "new"}


def test_statuses_and_list_filters(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    now = clock.now()
    with transaction(db):
        repo.upsert_source_items(
            db,
            [
                item(ext="past", title="HW 1", due_at=now - timedelta(days=1)),
                item(ext="soon", title="HW 2", due_at=now + timedelta(days=1)),
                item(ext="later", title="HW 5", due_at=now + timedelta(days=20)),
                item(ext="nodue", title="Reading", due_at=None),
                item(ext="done", title="HW 0", due_at=now + timedelta(days=2), submitted=True),
            ],
            ids,
            now,
        )
        repo.apply_merge(db, ids, now)
        assert (
            repo.refresh_statuses(db, now, tracking_sources=EVERY_SOURCE) == 2
        )  # past -> missed, done -> submitted
        assert repo.refresh_statuses(db, now, tracking_sources=EVERY_SOURCE) == 0
    status = {i.title: i.status for i in repo.list_items(db)}
    assert status == {
        "HW 1": ItemStatus.MISSED,
        "HW 2": ItemStatus.UPCOMING,
        "HW 0": ItemStatus.SUBMITTED,
        "HW 5": ItemStatus.UPCOMING,
        "Reading": ItemStatus.UPCOMING,
    }
    window = repo.list_items(db, due_after=now, due_before=now + timedelta(days=14), open_only=True)
    assert [i.title for i in window] == ["HW 2", "Reading"]
    dated = repo.list_items(db, due_after=now, include_undated=False)
    assert [i.title for i in dated] == ["HW 2", "HW 0", "HW 5"]
    assert [i.title for i in repo.list_items(db, include_undated=False)][-1] == "HW 5"
    assert repo.get_item(db, 9999) is None


# ---------------------------------------------------------------- posts & state & runs


def post(**kw: object) -> PostRecord:
    base: dict[str, object] = {
        "platform": "piazza",
        "external_id": "abc:1",
        "via": Source.PIAZZA,
        "course_code": "CMSC351",
        "title": "Midterm",
        "body": "Thursday",
        "posted_at": T0,
        "raw_hash": "p1",
        "author_role": AuthorRole.INSTRUCTOR,
    }
    base.update(kw)
    return PostRecord(**base)  # type: ignore[arg-type]


def test_posts_upsert_detects_edits(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock
) -> None:
    with transaction(db):
        [pid] = repo.upsert_posts(
            db, [post(), post(course_code="HIST289", external_id="z")], ids, clock.now()
        )
        assert repo.upsert_posts(db, [post()], ids, clock.now()) == []
        assert repo.upsert_posts(
            db, [post(body="Friday", raw_hash="p2", via=Source.PIAZZA_EMAIL)], ids, clock.now()
        ) == [pid]
    [stored] = repo.list_posts(db, "CMSC351")
    assert stored.body == "Friday"
    assert stored.via == Source.PIAZZA_EMAIL
    assert repo.list_posts(db) == [stored]


def test_source_state_round_trip(db: sqlite3.Connection, clock: FakeClock) -> None:
    s = repo.SourceState(payload_hash="h", etag='"e"', last_modified="lm", data="{}")
    with transaction(db):
        repo.set_source_state(db, "canvas_ical", "feed", s, clock.now())
        repo.set_source_state(db, "canvas_ical", "feed", s, clock.now())
    assert repo.all_source_state(db) == {("canvas_ical", "feed"): s}


def test_sync_runs_health_and_consecutive_failures(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    def run(status: str, error: str | None = None) -> None:
        clock.advance(timedelta(minutes=30))
        repo.record_sync_run(
            db,
            source="gradescope",
            started_at=clock.now(),
            finished_at=clock.now() + timedelta(seconds=2),
            status=status,
            error=error,
        )

    run("ok")
    run("error", "boom")
    run("skipped")
    run("auth_error", "bad password")
    assert repo.consecutive_failures(db, "gradescope") == 2
    health = repo.source_health(db, ["gradescope", "piazza"])
    g = health["gradescope"]
    assert g.last_status == "auth_error"
    assert g.last_error == "bad password"
    assert g.failures == 2
    assert g.last_success is not None
    assert g.last_attempt == clock.now()
    assert health["piazza"].last_attempt is None
    run("unchanged")
    assert repo.consecutive_failures(db, "gradescope") == 0
    assert repo.recent_sync_runs(db, "gradescope", limit=1)[0].status == "unchanged"
