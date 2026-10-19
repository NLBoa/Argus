"""Announcements end to end with a mocked model: extract -> guards -> decide -> apply -> confirm."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from argus.ai.client import AIClient
from argus.announcements import attach_official, confirm, dismiss, scan_posts
from argus.clock import FakeClock
from argus.config import AIConfig, AppConfig
from argus.domain.models import AuthorRole, ItemKind, PostRecord, Source, SourceItem
from argus.store import repo
from argus.store.db import transaction
from tests.ai_fakes import FakeTransport, ok, recorded

FRI_NOON = datetime(2026, 10, 23, 16, tzinfo=UTC)  # Fri Oct 23 12:00 EDT
MIDTERM = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)  # Tue Nov 3 9:30 EST


def responder_for(table: dict[str, str]) -> Any:
    def respond(req: dict[str, Any]) -> Any:
        for marker, fixture in table.items():
            if marker in req["user"]:
                return ok(recorded(fixture))
        return ok({"findings": []})

    return respond


def post(
    ext: str, title: str, body: str, role: AuthorRole = AuthorRole.INSTRUCTOR, h: str | None = None
) -> PostRecord:
    return PostRecord(
        "piazza",
        ext,
        Source.PIAZZA,
        "CMSC351",
        title,
        body,
        datetime(2026, 10, 18, 15, tzinfo=UTC),
        h or f"h-{ext}-{body}",
        role,
        url=f"https://piazza.com/class/x/post/{ext}",
    )


@pytest.fixture
def setup(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock
) -> tuple[sqlite3.Connection, dict[str, int]]:
    with transaction(db):
        ids = repo.sync_courses(db, app_config, clock.now())
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_ICAL, "mid1", "CMSC351", "Midterm 1", ItemKind.EXAM, MIDTERM, "h"
                ),
                SourceItem(
                    Source.CANVAS_ICAL,
                    "q2",
                    "CMSC351",
                    "Quiz 2",
                    ItemKind.QUIZ,
                    clock.now() + timedelta(days=3),
                    "h",
                ),
            ],
            ids,
            clock.now(),
        )
        repo.apply_merge(db, ids, clock.now())
    return db, ids


def ai(db: sqlite3.Connection, clock: FakeClock, transport: FakeTransport | None) -> AIClient:
    return AIClient(db, AIConfig(), clock, transport)


def add_posts(
    db: sqlite3.Connection, ids: dict[str, int], clock: FakeClock, *posts: PostRecord
) -> None:
    with transaction(db):
        repo.upsert_posts(db, posts, ids, clock.now())


def by_title(db: sqlite3.Connection) -> dict[str, Any]:
    return {i.title: i for i in repo.list_items(db)}


async def test_staff_announcement_creates_announced_item_then_official_takes_over(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(
        db,
        ids,
        clock,
        post(
            "1", "Quiz 3 scheduled", "Quiz 3 will be on Friday, Oct 23, covering lectures 8 to 10."
        ),
    )
    t = FakeTransport(responder=responder_for({"Quiz 3 will be": "extract_quiz3_new"}))
    report = await scan_posts(db, ai(db, clock, t), app_config, clock)
    assert (report.analyzed, report.findings, report.applied) == (1, 1, 1)
    quiz = by_title(db)["Quiz 3"]
    assert quiz.announced
    assert quiz.due_at == datetime(2026, 10, 24, 3, 59, tzinfo=UTC)  # end of that local day
    [f] = repo.list_findings(db)
    assert (f.status, f.item_id) == ("applied", quiz.id)
    # The prompt carried context and the integrity rule.
    assert "Known items in CMSC351" in t.calls[0]["user"] and "Midterm 1" in t.calls[0]["user"]
    assert "never drafts answers" in t.calls[0]["system"]

    # Canvas lists it a day later with a real time: official wins, no conflict (same local day).
    with transaction(db):
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_ICAL, "q3", "CMSC351", "Quiz 3", ItemKind.QUIZ, FRI_NOON, "h"
                )
            ],
            ids,
            clock.now(),
        )
        assert attach_official(db) == 1
        repo.apply_merge(db, ids, clock.now())
    quiz = by_title(db)["Quiz 3"]
    assert not quiz.announced
    assert quiz.due_at == FRI_NOON
    assert quiz.date_conflict == ()
    assert len(repo.list_items(db)) == 3  # merged, not duplicated


async def test_date_change_of_official_item_waits_then_confirm_overrides(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(
        db,
        ids,
        clock,
        post("2", "Midterm moved", "Because of the event, Midterm 1 moves to Thursday, Nov 5."),
    )
    t = FakeTransport(responder=responder_for({"Midterm 1 moves": "extract_midterm_moved"}))
    report = await scan_posts(db, ai(db, clock, t), app_config, clock)
    assert report.waiting == 1
    midterm = by_title(db)["Midterm 1"]
    assert midterm.due_at == MIDTERM  # unchanged until the student decides
    [f] = repo.list_findings(db, ["conflict"])
    message = confirm(db, f.id, clock.now())
    assert message == "Using the announced date for Midterm 1: Thu 9:30am"
    midterm = by_title(db)["Midterm 1"]
    assert midterm.due_at == datetime(2026, 11, 5, 14, 30, tzinfo=UTC)  # same 9:30 am, new day
    assert midterm.due_source == Source.MANUAL
    with pytest.raises(LookupError):
        confirm(db, f.id, clock.now())

    # Review R1: when an official source later changes the date, the student's override yields.
    with transaction(db):
        repo.apply_merge(db, ids, clock.now())  # unchanged official date: the override stays
    assert by_title(db)["Midterm 1"].due_source == Source.MANUAL
    moved = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    with transaction(db):
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_ICAL, "mid1", "CMSC351", "Midterm 1", ItemKind.EXAM, moved, "h2"
                )
            ],
            ids,
            clock.now(),
        )
        repo.apply_merge(db, ids, clock.now())
    midterm = by_title(db)["Midterm 1"]
    assert (midterm.due_at, midterm.due_source) == (moved, Source.CANVAS_ICAL)
    assert db.execute("SELECT COUNT(*) FROM item_overrides").fetchone()[0] == 0


async def test_confirmed_cancellation_is_undone_when_canvas_redates_it(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    quiz = by_title(db)["Quiz 2"]
    with transaction(db):
        repo.set_override(
            db, quiz.id, due_at=None, cancelled=True, reason="Confirmed", now=clock.now()
        )
        repo.apply_merge(db, ids, clock.now())
    assert "Quiz 2" not in by_title(db)
    with transaction(db):
        repo.apply_merge(db, ids, clock.now())  # still hidden while nothing official changed
    assert "Quiz 2" not in by_title(db)
    with transaction(db):
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_ICAL,
                    "q2",
                    "CMSC351",
                    "Quiz 2",
                    ItemKind.QUIZ,
                    clock.now() + timedelta(days=5),
                    "h2",
                )
            ],
            ids,
            clock.now(),
        )
        repo.apply_merge(db, ids, clock.now())
    assert "Quiz 2" in by_title(db)


async def test_dismiss_keeps_official_date(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(
        db,
        ids,
        clock,
        post("2", "Midterm moved", "Because of the event, Midterm 1 moves to Thursday, Nov 5."),
    )
    await scan_posts(
        db,
        ai(
            db,
            clock,
            FakeTransport(responder=responder_for({"Midterm 1 moves": "extract_midterm_moved"})),
        ),
        app_config,
        clock,
    )
    [f] = repo.list_findings(db, ["conflict"])
    assert dismiss(db, f.id, clock.now()) == "Kept the official date for Midterm 1"
    assert by_title(db)["Midterm 1"].due_at == MIDTERM
    with pytest.raises(LookupError):
        dismiss(db, f.id, clock.now())


async def test_cancellation_needs_confirmation(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(db, ids, clock, post("3", "No quiz", "Quiz 2 is cancelled this week."))
    await scan_posts(
        db,
        ai(
            db,
            clock,
            FakeTransport(responder=responder_for({"Quiz 2 is cancelled": "extract_cancel"})),
        ),
        app_config,
        clock,
    )
    assert "Quiz 2" in by_title(db)
    [f] = repo.list_findings(db, ["pending"])
    assert confirm(db, f.id, clock.now()) == "Quiz 2 marked cancelled"
    assert "Quiz 2" not in by_title(db)


async def test_invented_date_is_dropped_and_scope_applied(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(
        db,
        ids,
        clock,
        post("4", "Exam scope", "Midterm 1 covers lectures 1 through 12. One cheat sheet allowed."),
    )
    await scan_posts(
        db,
        ai(
            db,
            clock,
            FakeTransport(responder=responder_for({"Midterm 1 covers": "extract_invented_date"})),
        ),
        app_config,
        clock,
    )
    [f] = repo.list_findings(db)
    assert f.date is None  # the post states no date, so none is kept
    assert f.status == "applied"
    row = db.execute("SELECT scope FROM items WHERE title = 'Midterm 1'").fetchone()
    assert row["scope"] == "Lectures 1 to 12"


async def test_ungrounded_quote_is_discarded(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(db, ids, clock, post("5", "Reminder", "Remember: quiz next Friday in discussion."))
    await scan_posts(
        db,
        ai(db, clock, FakeTransport(responder=responder_for({"Remember": "extract_ungrounded"}))),
        app_config,
        clock,
    )
    assert repo.list_findings(db) == []
    assert "Quiz 9" not in by_title(db)


async def test_student_posts_are_held_and_chatter_is_skipped(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(
        db,
        ids,
        clock,
        post(
            "6",
            "Heard about quiz 3",
            "Quiz 3 will be on Friday, Oct 23 according to my TA",
            role=AuthorRole.STUDENT,
        ),
        post("7", "Study group", "Anyone want to meet tonight?", role=AuthorRole.STUDENT),
    )
    t = FakeTransport(responder=responder_for({"Quiz 3 will be": "extract_quiz3_new"}))
    await scan_posts(db, ai(db, clock, t), app_config, clock)
    assert len(t.calls) == 1  # the study-group post never reaches the model
    [f] = repo.list_findings(db)
    assert f.status == "pending"
    assert "Quiz 3" not in by_title(db)
    assert confirm(db, f.id, clock.now()) == "Added Quiz 3"
    assert by_title(db)["Quiz 3"].announced


async def test_without_ai_posts_wait_and_are_retried(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(db, ids, clock, post("8", "Quiz 3", "Quiz 3 will be on Friday, Oct 23."))
    report = await scan_posts(db, ai(db, clock, None), app_config, clock)
    assert report.deferred == 1
    assert len(repo.posts_to_scan(db)) == 1
    later = await scan_posts(
        db,
        ai(
            db,
            clock,
            FakeTransport(responder=responder_for({"Quiz 3 will be": "extract_quiz3_new"})),
        ),
        app_config,
        clock,
    )
    assert later.applied == 1
    assert repo.posts_to_scan(db) == []


async def test_edited_post_supersedes_open_findings(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    db, ids = setup
    add_posts(
        db,
        ids,
        clock,
        post("2", "Midterm moved", "Because of the event, Midterm 1 moves to Thursday, Nov 5."),
    )
    t = FakeTransport(responder=responder_for({"Midterm 1 moves": "extract_midterm_moved"}))
    await scan_posts(db, ai(db, clock, t), app_config, clock)
    add_posts(
        db,
        ids,
        clock,
        post(
            "2",
            "Midterm moved",
            "Update: Midterm 1 moves to Thursday, Nov 5. Room TBA.",
            h="edited",
        ),
    )
    await scan_posts(db, ai(db, clock, t), app_config, clock)
    statuses = sorted(f.status for f in repo.list_findings(db))
    assert statuses == ["conflict", "superseded"]


async def test_refusal_marks_post_scanned_without_findings(
    setup: Any, app_config: AppConfig, clock: FakeClock
) -> None:
    from tests.ai_fakes import refused

    db, ids = setup
    add_posts(db, ids, clock, post("9", "Quiz", "Quiz 3 will be on Friday, Oct 23."))
    await scan_posts(db, ai(db, clock, FakeTransport([refused()])), app_config, clock)
    assert repo.list_findings(db) == []
    assert repo.posts_to_scan(db) == []
