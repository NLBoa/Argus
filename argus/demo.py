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
from argus.domain.models import ItemKind, MeetingRecord, Source, SourceItem, Weekday
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
        repo.refresh_statuses(conn, now)
