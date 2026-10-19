"""The study pipeline end to end: materials → generate → verify → exam prep and review plan →
vault notes through git, plus the confusion inbox, proposals, and vault failure alerts.
All AI traffic goes through FakeTransport; the vault is a temp git repo without a remote."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from argus.ai.client import AIClient
from argus.clock import FakeClock, local_datetime
from argus.config import AppConfig
from argus.domain.models import (
    ItemKind,
    MaterialPage,
    MaterialRecord,
    ModuleItemRecord,
    ModuleRecord,
    Source,
    SourceItem,
)
from argus.exams import concept_readiness, practice_items, record_attempt, sync_exams
from argus.notify.router import Router
from argus.planning import replan
from argus.reminders.engine import ReminderEngine
from argus.reminders.rules import Rule, candidates, start_nudges
from argus.store import repo
from argus.store.db import transaction
from argus.study import ensure_study, targeted_practice
from argus.vault import layout
from argus.vault import service as vault
from argus.vault.regions import parse, replace_region
from tests.ai_fakes import FakeTransport, ok
from tests.caps import EVERY_SOURCE, tracked_caps

SLIDE_3 = "The master theorem solves recurrences of the form T(n) = aT(n/b) + f(n)."
SLIDE_4 = "Example: T(n) = 2T(n/2) + n gives Theta(n log n), case 2."
MASTER = {
    "name": "Master theorem",
    "idea": [{"text": "It solves T(n) = aT(n/b) + f(n).", "sources": ["S1"]}],
    "diagram": "graph TD\n  A[f vs n^log_b a] --> B[case]",
    "worked_example": [{"text": "2T(n/2) + n is Theta(n log n).", "sources": ["S1"]}],
    "why_it_works": [],
    "common_mistakes": [{"text": "It applies to every recurrence.", "sources": ["S1"]}],
    "practice": [
        {
            "kind": "question",
            "prompt": "Solve T(n) = 2T(n/2) + n.",
            "answer": "Theta(n log n)",
            "sources": ["S2"],
        },
        {"kind": "flashcard", "prompt": "Case 2 result?", "answer": "Theta(n)", "sources": ["S2"]},
    ],
    "related": ["Recursion trees"],
}
AKRA = {
    "name": "Akra-Bazzi",
    "idea": [{"text": "Akra-Bazzi generalizes it.", "sources": ["S2"]}],
    "diagram": None,
    "worked_example": [],
    "why_it_works": [],
    "common_mistakes": [],
    "practice": [{"kind": "question", "prompt": "Q?", "answer": "A", "sources": ["S2"]}],
    "related": [],
}
VERIFY_MASTER = {
    "claims": [
        {"id": "C1", "supported": True, "citation_ok": True, "note": ""},
        {"id": "C2", "supported": True, "citation_ok": False, "note": "slide 4, not 3"},
        {"id": "C3", "supported": False, "citation_ok": False, "note": "false"},
        {"id": "D1", "supported": True, "citation_ok": True, "note": ""},
    ],
    "practice": [
        {"id": "P1", "answer_correct": True, "note": ""},
        {"id": "P2", "answer_correct": False, "note": "case 2 gives n log n"},
    ],
}
VERIFY_AKRA = {
    "claims": [{"id": "C1", "supported": False, "citation_ok": False, "note": ""}],
    "practice": [],
}
TARGETED = {
    "practice": [
        {
            "kind": "question",
            "prompt": "Which case is T(n) = 2T(n/2) + n?",
            "answer": "Case 2",
            "sources": ["S2"],
        }
    ]
}


def respond(req: dict[str, Any]) -> Any:
    user = req["user"]
    if req["model"] == "claude-opus-5-5":
        return ok(VERIFY_AKRA if "Akra-Bazzi" in user else VERIFY_MASTER, model=req["model"])
    if "case 3" in user:
        return ok(TARGETED, model=req["model"])
    return ok({"concepts": [MASTER, AKRA]}, model=req["model"])


@pytest.fixture
def world(db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock) -> dict[str, int]:
    clock.set(local_datetime(date(2026, 10, 19), 12))  # Mon noon
    now = clock.now()
    with transaction(db):
        ids = repo.sync_courses(db, app_config, now)
        repo.replace_modules(
            db,
            ids["CMSC351"],
            [
                ModuleRecord(
                    "CMSC351",
                    1,
                    "Recurrences",
                    1,
                    (ModuleItemRecord("Lecture 7 slides", "File", "https://elms.example/files/7"),),
                )
            ],
            now,
        )
        repo.upsert_materials(
            db,
            [
                MaterialRecord(
                    "CMSC351",
                    1,
                    "Lecture 7 slides",
                    "pptx",
                    "https://elms.example/files/7",
                    (MaterialPage("slide 3", SLIDE_3), MaterialPage("slide 4", SLIDE_4)),
                    "h1",
                )
            ],
            ids,
            now,
        )
        repo.upsert_source_items(
            db,
            [
                SourceItem(
                    Source.CANVAS_ICAL,
                    "mt1",
                    "CMSC351",
                    "Midterm 1",
                    ItemKind.EXAM,
                    now + timedelta(days=6),
                    "a",
                ),
                SourceItem(
                    Source.CANVAS_ICAL,
                    "hw3",
                    "CMSC351",
                    "Homework 3",
                    ItemKind.ASSIGNMENT,
                    now + timedelta(days=2),
                    "b",
                ),
            ],
            ids,
            now,
        )
        repo.apply_merge(db, ids, now)
        repo.refresh_statuses(db, now, tracking_sources=EVERY_SOURCE)
    return {i.title: i.id for i in repo.list_items(db)}


def ai(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, transport: FakeTransport | None
) -> AIClient:
    return AIClient(db, app_config.ai, clock, transport)


def note(app_config: AppConfig, rel: str) -> str:
    return (app_config.paths.vault_dir / rel).read_text(encoding="utf-8")


CONCEPT = layout.concept_note("CMSC351", "Master theorem")
EXAM = layout.exam_note("CMSC351", "Midterm 1")


async def study(db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock) -> FakeTransport:
    transport = FakeTransport(responder=respond)
    assert await ensure_study(db, ai(db, app_config, clock, transport), app_config, clock) == 1
    sync_exams(db, clock.now())
    replan(db, app_config, clock)
    return transport


async def test_verified_notes_exam_prep_and_review_plan(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    transport = await study(db, app_config, clock)
    # Generator on the standard tier, verifier on the strong tier, as separate calls.
    assert [c["model"] for c in transport.calls] == [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-opus-5-5",
    ]
    assert "Lecture 7 slides" not in transport.calls[1]["system"]
    names = [r["name"] for r in db.execute("SELECT name FROM concepts")]
    assert names == ["Master theorem"]  # Akra-Bazzi's idea failed verification
    [item] = practice_items(db, [db.execute("SELECT id FROM concepts").fetchone()[0]])
    assert (
        item.prompt == "Solve T(n) = 2T(n/2) + n."
        and item.source_ref == "Lecture 7 slides, slide 4"
    )

    reviews = db.execute(
        "SELECT * FROM plan_blocks WHERE kind = 'review' ORDER BY start_at"
    ).fetchall()
    assert len(reviews) == 4  # 5, 3, 2, 1 days out (7 days out has passed)
    assert {r["item_id"] for r in reviews} == {world["Midterm 1"]}
    assert reviews[0]["label"] == "Review: Master theorem"

    result = vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    assert result.status == "ok" and result.files > 0
    concept = note(app_config, CONCEPT)
    core = parse(concept).regions["core"].content
    assert "It solves T(n) = aT(n/b) + f(n). (Lecture 7 slides, slide 3)" in core
    assert (
        "2T(n/2) + n is Theta(n log n). (Lecture 7 slides, slide 3) _(source unconfirmed)_" in core
    )
    assert "every recurrence" not in concept  # unsupported claim removed
    assert "Case 2 result?" not in concept  # wrong practice dropped
    assert "```mermaid" in core and "> [!question]- Solve T(n) = 2T(n/2) + n." in core
    assert concept.index("## Explain it yourself") > concept.index("%% argus:end core %%")

    exam = parse(note(app_config, EXAM)).regions
    assert "0 solid, 0 shaky, 1 untested" in exam["scope"].content
    assert exam["plan"].content.count("Review: Master theorem") == 4
    assert "> [!check]- Answer\n> Theta(n log n)" in exam["practice"].content
    cards = note(app_config, layout.flashcards("CMSC351"))
    assert (
        "TARGET DECK: Argus::CMSC351" in cards
        and "START\nBasic\nSolve T(n) = 2T(n/2) + n." in cards
    )
    course = note(app_config, layout.course_note("CMSC351"))
    assert "[[Courses/CMSC351/Exams/Midterm 1|Midterm 1]]" in course
    assert "[[Courses/CMSC351/Assignments/Homework 3|Homework 3]]" in course
    module = note(app_config, layout.module_note("CMSC351", "Recurrences"))
    assert (
        "[Lecture 7 slides](https://elms.example/files/7)" in module and "Master theorem" in module
    )
    assert "Courses/CMSC351/Concepts/Master theorem.md" in note(
        app_config, layout.concept_map("CMSC351")
    )
    assert note(app_config, layout.INBOX_README).startswith("# Confusion inbox")
    assert "No proposals right now." in note(app_config, layout.PROPOSALS)

    # Nothing changed: the next cycle commits nothing.
    assert vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config)).files == 0


async def test_self_grading_updates_readiness_and_review_nudges(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    await study(db, app_config, clock)
    concept_id = db.execute("SELECT id FROM concepts").fetchone()[0]
    [item] = practice_items(db, [concept_id])
    record_attempt(db, item.id, "incorrect", clock.now())
    assert concept_readiness(db, [concept_id])[concept_id].value == "shaky"
    with pytest.raises(ValueError, match="result"):
        record_attempt(db, item.id, "maybe", clock.now())
    with pytest.raises(LookupError):
        record_attempt(db, 999, "correct", clock.now())
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    assert "0 solid, 1 shaky, 0 untested" in note(app_config, EXAM)

    first = db.execute(
        "SELECT start_at FROM plan_blocks WHERE kind = 'review' ORDER BY start_at"
    ).fetchone()[0]
    clock.set(repo.parse_iso(first) + timedelta(minutes=2))
    engine = ReminderEngine(db, app_config, clock, Router([]), caps=tracked_caps(app_config))
    [nudge] = start_nudges(engine.snapshot(clock.now()))
    assert nudge.title == "Time to review" and "Review: Master theorem" in nudge.body
    assert any(r.rule is Rule.START_NUDGE for r in candidates(engine.snapshot(clock.now())))


async def test_without_ai_the_fallback_notes_are_still_written(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    assert await ensure_study(db, ai(db, app_config, clock, None), app_config, clock) == 0
    no_ai = tracked_caps(app_config, ai=False)
    assert vault.run_cycle(db, app_config, clock, caps=no_ai).status == "ok"
    assert "[Lecture 7 slides](https://elms.example/files/7)" in note(
        app_config, layout.module_note("CMSC351", "Recurrences")
    )
    assert "**Due:**" in note(app_config, layout.assignment_note("CMSC351", "Homework 3"))
    # No AI, no concept notes: the exam note has no study regions to promise (D055).
    exam = note(app_config, EXAM)
    assert "Practice questions appear here" not in exam and "Readiness" not in exam
    assert "%% argus:begin practice %%\n%% argus:end practice %%" in exam
    assert not (app_config.paths.vault_dir / CONCEPT).exists()
    assert not (app_config.paths.vault_dir / layout.flashcards("CMSC351")).exists()
    assert not (app_config.paths.vault_dir / layout.INBOX_README).exists()
    assert "Concept map" not in note(app_config, layout.course_note("CMSC351"))


async def test_confusion_inbox_targets_practice_and_never_edits_the_entry(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    transport = await study(db, app_config, clock)
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    entry = app_config.paths.vault_dir / layout.INBOX_DIR / "this week.md"
    original = "# Stuff\n\n- I don't get the master theorem case 3\n- something about lunch\n"
    entry.write_text(original, encoding="utf-8")
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    assert entry.read_text(encoding="utf-8") == original
    assert db.execute("SELECT priority FROM concepts").fetchone()[0] == 1
    [(entry_id, concept_id, text)] = vault.pending_practice(db)
    assert text == "I don't get the master theorem case 3"
    client = ai(db, app_config, clock, transport)
    assert await targeted_practice(db, client, clock, concept_id, text) == 1
    vault.mark_practiced(db, entry_id, clock.now())
    assert vault.pending_practice(db) == []
    vault.run_cycle(
        db, app_config, clock, caps=tracked_caps(app_config)
    )  # the same line is not counted twice
    assert db.execute("SELECT priority FROM concepts").fetchone()[0] == 1
    assert "Which case is T(n) = 2T(n/2) + n?" in note(app_config, CONCEPT)


async def test_graded_work_never_feeds_generation(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    """Review R10: homework files are left out, and inbox lines naming graded work get no practice."""
    from argus.study import study_materials

    with transaction(db):
        repo.upsert_materials(
            db,
            [
                MaterialRecord(
                    "CMSC351",
                    1,
                    "Homework 3 solutions",
                    "pdf",
                    "https://elms.example/files/9",
                    (MaterialPage("page 1", "Answer to problem 1: ..."),),
                    "h9",
                )
            ],
            repo.course_ids(db),
            clock.now(),
        )
    assert [m.title for m in study_materials(db, "CMSC351", 1)] == ["Lecture 7 slides"]
    transport = await study(db, app_config, clock)
    assert "Answer to problem 1" not in transport.calls[0]["user"]
    concept_id = db.execute("SELECT id FROM concepts").fetchone()[0]
    before = len(transport.calls)
    added = await targeted_practice(
        db,
        ai(db, app_config, clock, transport),
        clock,
        concept_id,
        "how do I do Homework 3 problem 2 case 3",
    )
    assert added == 0 and len(transport.calls) == before


def test_match_concept() -> None:
    concepts = [
        (1, "CMSC351", "Master theorem"),
        (2, "MATH240", "Eigenvalues"),
        (3, "CMSC351", "Big-O notation"),
    ]
    assert vault.match_concept("confused by the master theorem", concepts) == 1
    assert vault.match_concept("MATH240: what are eigenvalues for?", concepts) == 2
    assert vault.match_concept("CMSC351 eigenvalues??", concepts) is None  # course code narrows
    assert vault.match_concept("lunch", concepts) is None


async def test_student_edit_becomes_a_proposal_and_approval_applies_once(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    await study(db, app_config, clock)
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    path = app_config.paths.vault_dir / CONCEPT
    path.write_text(replace_region(path.read_text(), "core", "My own summary."), encoding="utf-8")
    with transaction(db):
        db.execute("UPDATE concepts SET body = replace(body, 'It solves', 'The theorem solves')")
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    assert "My own summary." in path.read_text()
    [proposal] = db.execute("SELECT * FROM proposals WHERE status = 'open'").fetchall()
    assert "[[Courses/CMSC351/Concepts/Master theorem]]" in note(app_config, layout.PROPOSALS)
    vault.run_cycle(
        db,
        app_config,
        clock,
        caps=tracked_caps(app_config),
        before=lambda w: w.approve(proposal["id"]) and None,
    )
    assert "The theorem solves" in path.read_text() and "My own summary." not in path.read_text()
    assert (
        db.execute("SELECT status FROM proposals WHERE id = ?", (proposal["id"],)).fetchone()[0]
        == "applied"
    )
    assert "No proposals right now." in note(app_config, layout.PROPOSALS)


async def test_allow_folder_edits_freely_and_logs_in_weekly_note(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    root = app_config.paths.vault_dir
    folder = root / layout.course_note("CMSC351")
    folder.write_text(
        folder.read_text().replace("course: CMSC351\n", "course: CMSC351\nargus-edit: allow\n", 1)
    )
    hw = root / layout.assignment_note("CMSC351", "Homework 3")
    hw.write_text(replace_region(hw.read_text(), "core", "scribbles"), encoding="utf-8")
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    assert "**Due:**" in hw.read_text()
    assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    weekly = note(app_config, layout.weekly(date(2026, 10, 19)))
    assert "[[Courses/CMSC351/Assignments/Homework 3]], rewrote core" in weekly


async def test_anki_ids_from_the_plugin_survive(
    db: sqlite3.Connection, app_config: AppConfig, clock: FakeClock, world: dict[str, int]
) -> None:
    await study(db, app_config, clock)
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    path = app_config.paths.vault_dir / layout.flashcards("CMSC351")
    annotated = path.read_text().replace("\nEND", "\n<!--ID: 1566052191670-->\nEND", 1)
    path.write_text(annotated, encoding="utf-8")
    with transaction(db):
        db.execute("UPDATE practice_items SET answer = 'Theta(n lg n)'")
    vault.run_cycle(db, app_config, clock, caps=tracked_caps(app_config))
    text = path.read_text()
    assert "<!--ID: 1566052191670-->" in text and "Back: Theta(n lg n)" in text
    assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_three_failed_cycles_raise_a_system_alert(
    db: sqlite3.Connection,
    app_config: AppConfig,
    clock: FakeClock,
    world: dict[str, int],
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    broken = app_config.model_copy(
        update={"paths": app_config.paths.model_copy(update={"vault_dir": blocker / "vault"})}
    )
    engine = ReminderEngine(db, broken, clock, Router([]), caps=tracked_caps(broken))
    for n in range(3):
        assert vault.run_cycle(db, broken, clock, caps=tracked_caps(broken)).status == "error"
        alerts = [
            r for r in candidates(engine.snapshot(clock.now())) if r.rule is Rule.SYSTEM_ALERT
        ]
        assert bool(alerts) == (n == 2)
        clock.advance(timedelta(minutes=15))
    assert alerts[0].title == "Vault git sync failing"
    health = vault.health(db)
    assert health.failures == 3 and health.last_ok is None
    assert vault.due(db, clock.now()) and not vault.due(db, clock.now() - timedelta(minutes=14))
