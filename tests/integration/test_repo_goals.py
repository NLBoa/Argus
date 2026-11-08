"""Goal docs and goal sessions in the store (D079, migration 0015)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from argus.clock import FakeClock
from argus.domain.goals import DocError, GoalDoc, WeekDoc
from argus.domain.models import GoalSessionGone, GoalSessionRecord
from argus.store import repo

T0 = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)


def goal(tag: str = "Woodwork", aim: str = "Build a bookshelf") -> GoalDoc:
    return GoalDoc.model_validate(
        {
            "version": 1,
            "tag": tag,
            "aim": aim,
            "track": [{"id": "joints", "title": "Joints", "status": "current"}],
        }
    )


WEEK = WeekDoc.model_validate({"version": 1, "week": "2026-W46"})


def put(
    db: sqlite3.Connection,
    path: str,
    doc: GoalDoc | WeekDoc | None,
    err: DocError | None,
    now: datetime = T0,
    text_hash: str = "h1",
) -> None:
    kind = "week" if isinstance(doc, WeekDoc) or "/Weeks/" in path else "goal"
    repo.upsert_goal_doc(
        db, path=path, kind=kind, text_hash=text_hash, parsed=doc, error=err, now=now
    )


# --- docs -------------------------------------------------------------------------------------


def test_valid_doc_round_trips(db: sqlite3.Connection) -> None:
    put(db, "Goals/Woodwork.md", goal(), None)
    put(db, "Goals/Weeks/2026-W46.md", WEEK, None)
    docs = {d.path: d for d in repo.goal_docs(db)}
    assert docs["Goals/Woodwork.md"].good == goal() and docs["Goals/Woodwork.md"].error is None
    assert docs["Goals/Woodwork.md"].good_at == T0 and docs["Goals/Woodwork.md"].kind == "goal"
    assert docs["Goals/Weeks/2026-W46.md"].good == WEEK
    assert repo.goal_doc_hashes(db) == {"Goals/Woodwork.md": "h1", "Goals/Weeks/2026-W46.md": "h1"}
    assert repo.has_goal_docs(db)
    assert repo.goal_tags(db) == {"Woodwork"}


def test_bad_write_keeps_last_good(db: sqlite3.Connection) -> None:  # Review Focus 2
    put(db, "Goals/Woodwork.md", goal(), None)
    later = T0 + timedelta(days=7)
    put(db, "Goals/Woodwork.md", None, DocError(14, "track[1].status", "must be done"), later, "h2")
    (doc,) = repo.goal_docs(db)
    assert doc.good == goal() and doc.good_at == T0
    assert doc.error == DocError(14, "track[1].status", "must be done")
    assert doc.read_at == later
    put(db, "Goals/Woodwork.md", goal(aim="Fixed"), None, later, "h3")
    (doc,) = repo.goal_docs(db)
    assert doc.error is None and doc.good is not None and doc.good.aim == "Fixed"  # type: ignore[union-attr]


def test_never_valid_doc_has_only_the_error(db: sqlite3.Connection) -> None:
    put(db, "Goals/Chess.md", None, DocError(3, "", "YAML syntax: oops"))
    (doc,) = repo.goal_docs(db)
    assert doc.good is None and doc.error is not None
    assert repo.has_goal_docs(db)  # a file is there, so the page shows its error
    assert repo.goal_tags(db) == set()


def test_missing_file_hides_goal_and_returns(db: sqlite3.Connection) -> None:
    put(db, "Goals/Woodwork.md", goal(), None)
    put(db, "Goals/Chess.md", goal("Chess"), None)
    repo.mark_goal_docs_missing(db, {"Goals/Chess.md"}, T0 + timedelta(hours=1))
    assert [d.path for d in repo.goal_docs(db)] == ["Goals/Chess.md"]
    assert repo.goal_tags(db) == {"Chess"}
    repo.mark_goal_docs_missing(db, set(), T0 + timedelta(hours=2))
    assert not repo.has_goal_docs(db)
    put(db, "Goals/Woodwork.md", goal(), None, T0 + timedelta(hours=3))
    assert [d.path for d in repo.goal_docs(db)] == ["Goals/Woodwork.md"]


def test_week_docs_alone_do_not_make_a_page(db: sqlite3.Connection) -> None:
    put(db, "Goals/Weeks/2026-W46.md", WEEK, None)
    assert not repo.has_goal_docs(db)


def test_touch_updates_read_at_only(db: sqlite3.Connection) -> None:
    put(db, "Goals/Woodwork.md", goal(), None)
    repo.mark_goal_docs_missing(db, set(), T0 + timedelta(hours=1))
    repo.touch_goal_doc(db, "Goals/Woodwork.md", T0 + timedelta(hours=2))
    (doc,) = repo.goal_docs(db)
    assert doc.read_at == T0 + timedelta(hours=2) and doc.good_at == T0


# --- sessions ---------------------------------------------------------------------------------


def rec(
    sid: str,
    *,
    tag: str = "Woodwork",
    day: int = 12,
    completed: bool = False,
    done_when: str | None = None,
) -> GoalSessionRecord:
    start = datetime(2026, 11, day, 23, 30, tzinfo=UTC)
    return GoalSessionRecord(
        sid,
        tag,
        f"Session {sid}",
        start,
        start + timedelta(hours=1),
        60,
        completed,
        done_when,
        f"hash-{sid}-{completed}",
    )


def apply(
    db: sqlite3.Connection,
    records: list[GoalSessionRecord],
    gone: list[GoalSessionGone] | None = None,
    now: datetime = T0,
) -> int:
    return repo.apply_goal_sessions(db, records, gone or [], known_tags={"Woodwork"}, now=now)


def all_sessions(db: sqlite3.Connection) -> dict[str, object]:
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    return {s.external_id: s for s in repo.goal_sessions_between(db, start, end)}


def test_unknown_tag_not_persisted(db: sqlite3.Connection) -> None:
    assert apply(db, [rec("a"), rec("b", tag="CMSC351")]) == 1
    assert list(all_sessions(db)) == ["a"]


def test_upsert_keeps_done_when_when_the_fetch_has_none(db: sqlite3.Connection) -> None:
    apply(db, [rec("a", done_when="Two clean joints")])
    apply(db, [rec("a", completed=True)], now=T0 + timedelta(hours=1))
    s = all_sessions(db)["a"]
    assert s.done_when == "Two clean joints" and s.completed  # type: ignore[attr-defined]


def test_deleted_marks_gone_once(db: sqlite3.Connection) -> None:  # Review Focus 3
    apply(db, [rec("a")])
    apply(db, [], [GoalSessionGone("a", "deleted")], now=T0 + timedelta(hours=1))
    apply(db, [], [GoalSessionGone("a", "cancelled")], now=T0 + timedelta(hours=2))
    s = all_sessions(db)["a"]
    assert s.gone_at == T0 + timedelta(hours=1) and s.moved_at is None  # type: ignore[attr-defined]


def test_moved_marks_moved_not_gone(db: sqlite3.Connection) -> None:
    apply(db, [rec("a")])
    apply(db, [], [GoalSessionGone("a", "moved")], now=T0 + timedelta(hours=1))
    s = all_sessions(db)["a"]
    assert s.moved_at == T0 + timedelta(hours=1) and s.gone_at is None  # type: ignore[attr-defined]


def test_completed_never_gone(db: sqlite3.Connection) -> None:
    apply(db, [rec("a", completed=True)])
    apply(db, [], [GoalSessionGone("a", "deleted")], now=T0 + timedelta(hours=1))
    s = all_sessions(db)["a"]
    assert s.gone_at is None and s.completed  # type: ignore[attr-defined]


def test_completed_lookup_marks_done(db: sqlite3.Connection) -> None:
    apply(db, [rec("a")])
    apply(db, [], [GoalSessionGone("a", "completed")], now=T0 + timedelta(hours=1))
    assert all_sessions(db)["a"].completed  # type: ignore[attr-defined]


def test_reappearing_session_clears_moved(db: sqlite3.Connection) -> None:
    apply(db, [rec("a")])
    apply(db, [], [GoalSessionGone("a", "moved")], now=T0 + timedelta(hours=1))
    apply(db, [rec("a", day=13)], now=T0 + timedelta(hours=2))
    s = all_sessions(db)["a"]
    assert s.moved_at is None and s.start_at.day == 13  # type: ignore[attr-defined]


def test_gone_for_unknown_session_is_ignored(db: sqlite3.Connection) -> None:
    apply(db, [], [GoalSessionGone("zz", "deleted")])
    assert all_sessions(db) == {}


def test_open_session_ids_are_the_lookup_candidates(db: sqlite3.Connection) -> None:
    apply(
        db, [rec("open"), rec("done", completed=True), rec("gone"), rec("moved"), rec("old", day=1)]
    )
    apply(db, [], [GoalSessionGone("gone", "deleted"), GoalSessionGone("moved", "moved")])
    start, end = datetime(2026, 11, 9, 5, tzinfo=UTC), datetime(2026, 11, 30, 5, tzinfo=UTC)
    assert repo.open_goal_session_ids(db, start, end) == {"open": rec("open").start_at}


def test_between_is_half_open_and_sorted(db: sqlite3.Connection, clock: FakeClock) -> None:
    apply(db, [rec("b", day=13), rec("a", day=12)])
    start = datetime(2026, 11, 12, 23, 30, tzinfo=UTC)
    got = repo.goal_sessions_between(db, start, start + timedelta(days=1))
    assert [s.external_id for s in got] == ["a"]
