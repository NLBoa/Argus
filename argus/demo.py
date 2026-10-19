"""Synthetic demo data: preview the dashboard without credentials (`argus serve --demo`).

Everything is fake: course content, ids, URLs, and credentials. All times are relative
to `now`, so the demo looks alive whenever it runs. The e2e and visual suites use the same
seed with a frozen clock.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta

from pydantic import SecretStr

from argus.clock import local_datetime, to_local
from argus.config import AppConfig, Commitment, CourseConfig, ScheduleConfig, Secrets
from argus.domain.models import (
    AuthorRole,
    GradeRecord,
    ItemKind,
    MeetingRecord,
    ModuleItemRecord,
    ModuleRecord,
    PostRecord,
    Source,
    SourceItem,
    Weekday,
)
from argus.store import repo
from argus.store.db import iso, transaction

DEMO_CONFIG = AppConfig(
    courses=[
        CourseConfig(
            code="CMSC351",
            section="0101",
            canvas_id=1000001,
            gradescope_id="2000001",
            accent="indigo",
        ),
        CourseConfig(
            code="MATH240",
            section="0201",
            canvas_id=1000002,
            gradescope_id="2000002",
            accent="teal",
        ),
        CourseConfig(code="ENGL393", section="0301", canvas_id=1000003, accent="plum"),
    ],
    schedule=ScheduleConfig(
        commitments=[
            Commitment(title="Dining hall shift", days=["Tue", "Thu"], start=time(17), end=time(20))
        ]
    ),
)
# Fake values: they make sources show as configured; the demo never syncs.
DEMO_SECRETS = Secrets(
    canvas_ical_url=SecretStr("https://umd.instructure.com/feeds/calendars/user_DEMO.ics"),
    canvas_token=SecretStr("demo-canvas-token"),
    gradescope_email="demo@example.test",
    gradescope_password=SecretStr("demo-password"),
)

CANVAS = "https://umd.instructure.com/courses"
GRADESCOPE = "https://www.gradescope.com/courses"


def _at(now: datetime, days: int, hour: int, minute: int = 0) -> datetime:
    return local_datetime(to_local(now).date() + timedelta(days=days), hour, minute)


def seed_demo(conn: sqlite3.Connection, now: datetime) -> None:
    cfg = DEMO_CONFIG
    with transaction(conn):
        ids = repo.sync_courses(conn, cfg, now - timedelta(days=30))

        def canvas(
            ext: str,
            course: str,
            cid: int,
            title: str,
            kind: ItemKind,
            due: datetime | None,
            **kw: object,
        ) -> SourceItem:
            return SourceItem(
                source=Source.CANVAS_ICAL,
                external_id=ext,
                course_code=course,
                title=title,
                kind=kind,
                due_at=due,
                raw_hash=f"demo-{ext}",
                url=f"{CANVAS}/{cid}/assignments/{ext}",
                **kw,  # type: ignore[arg-type]
            )

        def gs(
            ext: str, course: str, gid: str, title: str, due: datetime, **kw: object
        ) -> SourceItem:
            return SourceItem(
                source=Source.GRADESCOPE,
                external_id=ext,
                course_code=course,
                title=title,
                kind=ItemKind.ASSIGNMENT,
                due_at=due,
                raw_hash=f"demo-{ext}",
                url=f"{GRADESCOPE}/{gid}/assignments/{ext}",
                **kw,  # type: ignore[arg-type]
            )

        a, q, e = ItemKind.ASSIGNMENT, ItemKind.QUIZ, ItemKind.EXAM
        records = [
            canvas("51003", "CMSC351", 1000001, "Homework 3", a, _at(now, 0, 23, 59)),
            gs("61003", "CMSC351", "2000001", "HW3", _at(now, 0, 23, 30), submitted=False),
            canvas("51004", "CMSC351", 1000001, "Quiz 2", q, _at(now, 1, 12)),
            canvas("51005", "CMSC351", 1000001, "Homework 4", a, _at(now, 5, 23, 59)),
            canvas("51009", "CMSC351", 1000001, "Midterm 1", e, _at(now, 9, 9, 30)),
            canvas(
                "51002",
                "CMSC351",
                1000001,
                "Homework 2",
                a,
                _at(now, -10, 23, 59),
                submitted=True,
                graded=True,
            ),
            canvas("52006", "MATH240", 1000002, "Problem Set 6", a, _at(now, 0, 23, 59)),
            gs(
                "62006", "MATH240", "2000002", "Problem Set 6", _at(now, 0, 23, 59), submitted=False
            ),
            canvas("52012", "MATH240", 1000002, "Lab report 2", a, now - timedelta(hours=3)),
            canvas("52007", "MATH240", 1000002, "Problem Set 7", a, _at(now, 6, 23, 59)),
            canvas(
                "53001",
                "ENGL393",
                1000003,
                "Essay draft: technical description",
                a,
                _at(now, 2, 17),
            ),
            canvas(
                "53005",
                "ENGL393",
                1000003,
                "Reading response 5",
                a,
                _at(now, -1, 23, 59),
                submitted=True,
            ),
            canvas("53099", "ENGL393", 1000003, "Course survey", a, None),
        ]
        repo.upsert_source_items(conn, records, ids, now - timedelta(days=7))
        repo.apply_merge(conn, ids, now - timedelta(days=7))
        conn.execute("UPDATE items SET created_at = ?", (iso(now - timedelta(days=7)),))

        meetings = {
            "CMSC351": [
                MeetingRecord(
                    "CMSC351",
                    "lecture",
                    (Weekday.TUE, Weekday.THU),
                    time(9, 30),
                    time(10, 45),
                    "IRB",
                    "0324",
                ),
                MeetingRecord(
                    "CMSC351", "discussion", (Weekday.FRI,), time(10), time(10, 50), "CSI", "1121"
                ),
            ],
            "MATH240": [
                MeetingRecord(
                    "MATH240",
                    "lecture",
                    (Weekday.MON, Weekday.WED, Weekday.FRI),
                    time(11),
                    time(11, 50),
                    "KEY",
                    "0106",
                ),
                MeetingRecord(
                    "MATH240", "lab", (Weekday.TUE,), time(8), time(8, 50), "MTH", "B0423"
                ),
            ],
            "ENGL393": [
                MeetingRecord(
                    "ENGL393",
                    "lecture",
                    (Weekday.MON, Weekday.WED),
                    time(14),
                    time(15, 15),
                    "TWS",
                    "0221",
                ),
            ],
        }
        for code, ms in meetings.items():
            repo.replace_meetings(conn, ids[code], "umdio", ms, now - timedelta(days=2))

        def run(source: str, minutes_ago: int, status: str, error: str | None = None) -> None:
            t = now - timedelta(minutes=minutes_ago)
            repo.record_sync_run(
                conn,
                source=source,
                started_at=t,
                finished_at=t + timedelta(seconds=3),
                status=status,
                error=error,
            )

        run("umdio", 60 * 49, "ok")
        for m in (240, 190, 130):
            run("canvas_ical", m, "ok")
        run("canvas_ical", 10, "unchanged")
        run("gradescope", 186, "ok")
        for m in (126, 96, 66, 36):
            run(
                "gradescope",
                m,
                "error",
                "FetchError: Gradescope page format changed; gradescopeapi may need an update",
            )

        essay = next(i for i in repo.list_items(conn) if i.title.startswith("Essay draft"))
        repo.start_timer(conn, essay.id, now - timedelta(minutes=42))
        hw2 = next(i for i in repo.list_items(conn) if i.title == "Homework 2")
        repo.log_time(conn, hw2.id, 3.5, now - timedelta(days=10))

        for n, (rule, title, body, hours_ago) in enumerate(
            [
                (
                    "due_24h",
                    "Due tomorrow",
                    "[1] ENGL393 Reading response 5, Sun 11:59pm. Reply DONE 1 when submitted.",
                    30,
                ),
                (
                    "new_item",
                    "2 new items",
                    "[1] CMSC351 Quiz 2 (Tue 12pm); [2] MATH240 Problem Set 7 (Sun 11:59pm)",
                    20,
                ),
                (
                    "system_alert",
                    "Gradescope sync failing",
                    "3 failed syncs in a row. Deadlines from it may be stale; check Health.",
                    1,
                ),
            ]
        ):
            nid = repo.claim_notification(
                conn,
                keys=[f"demo:{n}"],
                rule=rule,
                title=title,
                body=body,
                item_ids=[],
                priority=3,
                counts_toward_cap=True,
                now=now - timedelta(hours=hours_ago),
            )
            assert nid is not None
            repo.finish_notification(
                conn, nid, ok=True, channel="ntfy", error=None, now=now - timedelta(hours=hours_ago)
            )
        _seed_tier2(conn, ids, now)
        _seed_findings(conn, ids, now)
        repo.apply_merge(conn, ids, now)
        repo.refresh_statuses(conn, now)
        _seed_estimates(conn, now)
        repo.rebuild_search_index(conn)
    from argus.clock import FakeClock
    from argus.planning import replan

    replan(conn, DEMO_CONFIG, FakeClock(now), force=True)


def _seed_tier2(conn: sqlite3.Connection, ids: dict[str, int], now: datetime) -> None:
    """Modules, a grade, and staff posts (what a Canvas token and Piazza add)."""
    files = f"{CANVAS}/1000001/modules/items"

    def item(n: int, title: str, kind: str = "File", indent: int = 1) -> ModuleItemRecord:
        return ModuleItemRecord(
            title, kind, f"{files}/{n}" if kind != "SubHeader" else None, indent
        )

    modules = [
        ModuleRecord(
            "CMSC351",
            301,
            "Week 1: Asymptotics",
            1,
            (
                item(1, "Lecture 1 slides: big-O, big-Omega, big-Theta"),
                item(2, "Growth-rate worksheet", "Page"),
            ),
            state="completed",
        ),
        ModuleRecord(
            "CMSC351",
            302,
            "Week 2: Recurrences and the master theorem",
            2,
            (
                item(10, "Readings", "SubHeader", 0),
                item(11, "Lecture 4 slides: the master theorem"),
                item(12, "Recurrence practice (ungraded)", "Quiz"),
                item(13, "Homework 2", "Assignment"),
            ),
            state="completed",
        ),
        ModuleRecord(
            "CMSC351",
            303,
            "Week 7: Dynamic programming",
            7,
            (
                item(20, "Lecture 13 slides: memoization"),
                item(21, "Lecture 14 slides: longest common subsequence"),
                item(22, "Homework 4", "Assignment"),
            ),
            state="started",
        ),
        ModuleRecord(
            "CMSC351",
            304,
            "Week 9: Midterm 1 review",
            9,
            (item(30, "Practice midterm (from a past term)"),),
            state="locked",
        ),
    ]
    repo.replace_modules(conn, ids["CMSC351"], modules, now - timedelta(hours=6))
    repo.set_grade(conn, GradeRecord("CMSC351", 91.5, "A-"), now - timedelta(hours=6))
    repo.set_grade(conn, GradeRecord("MATH240", 86.25, "B"), now - timedelta(hours=6))
    posts = [
        PostRecord(
            "canvas",
            "announcement:1",
            Source.CANVAS_API,
            "CMSC351",
            "Midterm 1 is in class on Wednesday, Oct 28",
            "Midterm 1 covers lectures 1 through 12: asymptotics, recurrences, the master theorem, and sorting. "
            "One double-sided cheat sheet is allowed.",
            now - timedelta(days=2),
            "demo-a1",
            AuthorRole.INSTRUCTOR,
            kind="announcement",
            url=f"{CANVAS}/1000001/discussion_topics/1",
        ),
        PostRecord(
            "piazza",
            "demo:41",
            Source.PIAZZA,
            "CMSC351",
            "HW3 problem 2 clarification",
            "You may assume the graph is connected. Homework 3 is still due tonight at 11:59 pm on Gradescope.",
            now - timedelta(hours=20),
            "demo-p41",
            AuthorRole.TA,
            kind="instructor_answer",
            url="https://piazza.com/class/demo/post/41",
        ),
        PostRecord(
            "canvas",
            "announcement:2",
            Source.CANVAS_API,
            "MATH240",
            "Lab 3 room change",
            "This week's lab meets in MTH B0425 instead of the usual room.",
            now - timedelta(days=1),
            "demo-a2",
            AuthorRole.INSTRUCTOR,
            kind="announcement",
            url=f"{CANVAS}/1000002/discussion_topics/2",
        ),
    ]
    repo.upsert_posts(conn, posts, ids, now)


def _seed_estimates(conn: sqlite3.Connection, now: datetime) -> None:
    from argus.estimates import input_hash

    items = {i.title: i for i in repo.list_items(conn)}
    rows = {
        "Homework 3": (
            "ai",
            3.5,
            5.0,
            [
                ("Review lecture 9 notes", 0.5),
                ("Write up the three proofs", 2.5),
                ("Check and typeset", 0.5),
            ],
            ["Each proof is similar in length to the homework 2 proofs"],
            "Three proofs at about 50 minutes each, plus review and typesetting.",
        ),
        "Problem Set 6": (
            "default",
            3.0,
            4.5,
            [],
            [],
            "Typical for an assignment; no history yet. (AI estimate unavailable: unavailable.)",
        ),
        "Quiz 2": (
            "ai",
            1.0,
            1.5,
            [("Review the week's slides", 0.5), ("Take the quiz", 0.5)],
            ["Timed quiz of about 30 minutes"],
            "A short online quiz plus a quick review.",
        ),
        "Homework 4": (
            "ai",
            4.0,
            6.0,
            [
                ("Read the dynamic programming notes", 1.0),
                ("Solve and write up four problems", 3.0),
            ],
            ["Four problems of medium difficulty"],
            "Four problems; dynamic programming tends to take longer the first time.",
        ),
        "Problem Set 7": (
            "ai",
            2.5,
            3.5,
            [("Work the problems", 2.0), ("Check answers", 0.5)],
            ["About ten short computational problems"],
            "Short computational problems.",
        ),
        "Essay draft: technical description": (
            "ai",
            2.5,
            4.0,
            [("Outline", 0.5), ("Draft", 1.5), ("Revise", 0.5)],
            ["About 1,000 words"],
            "A short draft with one revision pass.",
        ),
        "Homework 2": (
            "ai",
            2.5,
            3.5,
            [("Proofs", 2.5)],
            ["Similar to homework 1"],
            "Earlier estimate.",
        ),
    }
    for title, (source, p50, p80, breakdown, assumptions, reasoning) in rows.items():
        item = items.get(title)
        if item is None:
            continue
        repo.insert_estimate(
            conn,
            item_id=item.id,
            source=source,
            p50=p50,
            p80=p80,
            input_hash=input_hash(item),
            now=now - timedelta(days=3),
            confidence="medium" if source == "ai" else "low",
            breakdown=breakdown,
            assumptions=assumptions,
            reasoning=reasoning,
            model="claude-sonnet-5-5" if source == "ai" else None,
            prompt_version="estimate.v1" if source == "ai" else None,
        )
    essay = items.get("Essay draft: technical description")
    if essay is not None:
        repo.insert_estimate(
            conn,
            item_id=essay.id,
            source="user",
            p50=3.0,
            p80=3.0,
            input_hash=input_hash(essay),
            now=now - timedelta(days=1),
            reasoning="Set by you.",
        )


def _seed_findings(conn: sqlite3.Connection, ids: dict[str, int], now: datetime) -> None:
    posts = [
        PostRecord(
            "piazza",
            "demo:50",
            Source.PIAZZA,
            "CMSC351",
            "Midterm 1 moved",
            "Because of the career fair, Midterm 1 moves to Thursday, Oct 29, same time and room.",
            now - timedelta(hours=5),
            "demo-p50",
            AuthorRole.INSTRUCTOR,
            kind="note",
            url="https://piazza.com/class/demo/post/50",
            pinned=True,
        ),
        PostRecord(
            "piazza",
            "demo:51",
            Source.PIAZZA,
            "MATH240",
            "Quiz 4 next Friday",
            "Quiz 4 will be next Friday in lab, covering eigenvalues.",
            now - timedelta(days=1),
            "demo-p51",
            AuthorRole.INSTRUCTOR,
            kind="note",
            url="https://piazza.com/class/demo/post/51",
        ),
    ]
    repo.upsert_posts(conn, posts, ids, now)
    post_ids = {
        r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM posts")
    }
    for pid in post_ids.values():
        repo.mark_scanned(conn, pid)
    # Leave one staff post unanalyzed to show the no-AI fallback.
    conn.execute("UPDATE posts SET scanned_hash = NULL WHERE external_id = 'announcement:2'")
    midterm = next(
        i for i in repo.list_items(conn) if i.title == "Midterm 1" and i.course_code == "CMSC351"
    )
    repo.insert_finding(
        conn,
        post_id=post_ids["demo:50"],
        course_id=ids["CMSC351"],
        kind="date_change",
        title="Midterm 1",
        date=_at(now, 10, 23, 59),
        date_only=True,
        scope=None,
        quote="Midterm 1 moves to Thursday, Oct 29",
        confidence=0.97,
        item_id=midterm.id,
        status="conflict",
        reason="Disagrees with the official date",
        now=now - timedelta(hours=5),
    )
    fid = repo.insert_finding(
        conn,
        post_id=post_ids["demo:51"],
        course_id=ids["MATH240"],
        kind="new_assessment",
        title="Quiz 4",
        date=_at(now, 11, 23, 59),
        date_only=True,
        scope="Eigenvalues",
        quote="Quiz 4 will be next Friday in lab",
        confidence=0.95,
        item_id=None,
        status="applied",
        reason="Announced by staff",
        now=now - timedelta(days=1),
    )
    repo.add_finding_source(
        conn,
        finding_id=fid,
        course_id=ids["MATH240"],
        title="Quiz 4",
        kind=ItemKind.QUIZ,
        due_at=_at(now, 11, 23, 59),
        date_only=True,
        url="https://piazza.com/class/demo/post/51",
        item_id=None,
        now=now - timedelta(days=1),
    )
