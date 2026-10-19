import sqlite3
from datetime import timedelta

import pytest

from argus.clock import FakeClock
from argus.config import AppConfig
from argus.domain.models import (
    AuthorRole,
    ItemKind,
    ModuleItemRecord,
    ModuleRecord,
    PostRecord,
    Source,
    SourceItem,
)
from argus.store import repo
from argus.store.db import transaction


@pytest.fixture
def indexed(db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock) -> sqlite3.Connection:
    now = clock.now()
    with transaction(db):
        ids = repo.sync_courses(db, app_config, now)
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_ICAL,
                    "1",
                    "CMSC351",
                    "Homework 3",
                    ItemKind.ASSIGNMENT,
                    now + timedelta(days=2),
                    "h",
                    url="https://umd.instructure.com/x/1",
                    description="Prove the recurrence lemmas using induction.",
                ),
                SourceItem(
                    Source.CANVAS_ICAL,
                    "2",
                    "MATH240",
                    "Problem Set 6",
                    ItemKind.ASSIGNMENT,
                    now + timedelta(days=3),
                    "h",
                    description="Eigenvalues and eigenvectors of symmetric matrices.",
                ),
            ],
            ids,
            now,
        )
        repo.apply_merge(db, ids, now)
        repo.replace_modules(
            db,
            ids["CMSC351"],
            [
                ModuleRecord(
                    "CMSC351",
                    302,
                    "Week 2: Recurrences",
                    2,
                    (ModuleItemRecord("Lecture 4: the master theorem", "File", "https://x/4"),),
                )
            ],
            now,
        )
        repo.upsert_posts(
            db,
            [
                PostRecord(
                    "piazza",
                    "n:1",
                    Source.PIAZZA,
                    "CMSC351",
                    "Midterm logistics",
                    "The midterm covers recurrences. <script>alert(1)</script>",
                    now,
                    "p",
                    AuthorRole.INSTRUCTOR,
                    url="https://piazza.com/class/n/post/1",
                )
            ],
            ids,
            now,
        )
        assert repo.rebuild_search_index(db) == 4
    return db


def test_search_across_kinds_with_stemming(indexed: sqlite3.Connection) -> None:
    hits = repo.search(indexed, "recurrence")
    assert {h.kind for h in hits} == {
        "item",
        "module",
        "post",
    }  # "recurrences" stems to the same token
    module = next(h for h in hits if h.kind == "module")
    assert module.url and module.url.startswith("/courses/CMSC351#module-")


def test_prefix_and_title_weighting(indexed: sqlite3.Connection) -> None:
    hits = repo.search(indexed, "eigen")
    assert [h.title for h in hits] == ["Problem Set 6"]
    assert "<mark>" in hits[0].snippet


def test_course_filter(indexed: sqlite3.Connection) -> None:
    assert repo.search(indexed, "recurrence", course="MATH240") == []


def test_snippets_are_escaped(indexed: sqlite3.Connection) -> None:
    [hit] = repo.search(indexed, "midterm covers")
    assert "<script>" not in hit.snippet
    assert "&lt;script&gt;" in hit.snippet


@pytest.mark.parametrize(
    "hostile", ['"', "*", "NEAR(a b)", "-", "a OR", "title:x", "''", "\x00", "(((", "AND"]
)
def test_hostile_queries_never_raise(indexed: sqlite3.Connection, hostile: str) -> None:
    repo.search(indexed, hostile)


def test_empty_query(indexed: sqlite3.Connection) -> None:
    assert repo.search(indexed, "   ") == []
    assert repo.fts_query("!!") is None
    assert repo.fts_query("hw 3") == '"hw" "3"*'


def test_rebuild_drops_removed_items(indexed: sqlite3.Connection, clock: FakeClock) -> None:
    with transaction(indexed):
        repo.mark_absent(
            indexed,
            Source.CANVAS_ICAL,
            course_id=repo.course_ids(indexed)["MATH240"],
            present_external_ids=set(),
            now=clock.now(),
        )
        repo.apply_merge(indexed, repo.course_ids(indexed), clock.now())
        repo.rebuild_search_index(indexed)
    assert repo.search(indexed, "eigen") == []


def test_modules_replace_keeps_order_and_drops_missing(
    indexed: sqlite3.Connection, clock: FakeClock
) -> None:
    cid = repo.course_ids(indexed)["CMSC351"]
    with transaction(indexed):
        repo.replace_modules(
            indexed,
            cid,
            [ModuleRecord("CMSC351", 401, "Week 3", 3), ModuleRecord("CMSC351", 400, "Week 1", 1)],
            clock.now(),
        )
    assert [m.record.name for m in repo.list_modules(indexed, "CMSC351")] == ["Week 1", "Week 3"]
