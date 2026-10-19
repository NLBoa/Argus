"""The simulated week harness, shared by the week tests and the deadline golden.

Fake Canvas, Gradescope, and Piazza feed the real worker (collectors -> store -> mocked AI
-> estimates -> planner -> reminders -> ntfy) every 15 minutes of a frozen clock, from
Monday 08:00 to Sunday 23:45 local.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar

import httpx
import respx
from pydantic import SecretStr

from argus.clock import FakeClock, local_datetime
from argus.collectors.canvas_ical import CanvasIcalCollector
from argus.collectors.gradescope import GradescopeCollector, GSAssignment
from argus.collectors.http import Fetcher
from argus.collectors.piazza import PiazzaCollector
from argus.collectors.runner import SyncRunner
from argus.config import AppConfig, Secrets, TimeWindow, WorkWindowConfig
from argus.domain.models import MeetingRecord, Weekday
from argus.store import repo
from argus.store.db import open_db, transaction
from argus.worker import Worker
from tests.ai_fakes import FakeTransport, ok

FEED = "https://umd.instructure.com/feeds/calendars/user_FAKEFEED.ics"
NTFY = "https://ntfy.sh/"
START = local_datetime(date(2026, 10, 19), 8)  # Mon 08:00 EDT
END = local_datetime(date(2026, 10, 25), 23, 45)  # Sun 23:45 EDT
QUIET = TimeWindow(start=time(23), end=time(8))


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return local_datetime(date(2026, 10, 19) + timedelta(days=day), hh, mm)


# Canvas events: (uid, title, due, visible_from)
CANVAS = [
    ("9001", "Homework A", at(2, 23, 59), START),
    ("9002", "Quiz B", at(4, 10), at(2, 9)),  # listed Wednesday, announced on Piazza Tuesday
    ("9003", "Problem Set C", at(3, 23, 59), START),
    ("9004", "Homework D", at(7, 23, 59), at(3, 12)),  # appears Thursday: a "new item"
    ("9005", "Lab E", at(1, 8, 30), START),  # due just after quiet hours end
]
COURSE = {
    "9001": "CMSC351",
    "9002": "CMSC351",
    "9003": "MATH240",
    "9004": "CMSC351",
    "9005": "MATH240",
}
CANVAS_IDS = {"CMSC351": 1234567, "MATH240": 1234568}


def feed(now: datetime) -> bytes:
    events = []
    for uid, title, due, visible in CANVAS:
        if now < visible:
            continue
        code = COURSE[uid]
        events.append(
            "BEGIN:VEVENT\n"
            f"UID:event-assignment-{uid}\n"
            f"DTSTART:{due.strftime('%Y%m%dT%H%M%SZ')}\n"
            f"SUMMARY:{title} [{code}-0101-Fall2026]\n"
            f"URL;VALUE=URI:https://umd.instructure.com/courses/{CANVAS_IDS[code]}/assignments/{uid}\n"
            "END:VEVENT\n"
        )
    return ("BEGIN:VCALENDAR\nVERSION:2.0\n" + "".join(events) + "END:VCALENDAR\n").encode()


class FakeGS:
    clock: ClassVar[FakeClock]

    def login(self, email: str, password: str) -> None:
        return None

    def assignments(self, course_id: str) -> list[GSAssignment]:
        if course_id != "765433":
            return []
        # Gradescope says 23:30 for Problem Set C: merged with Canvas, earliest time wins.
        return [GSAssignment("77", "PS C", None, at(3, 23, 30), None, "No Submission", None, 10.0)]


class FakePiazza:
    clock: ClassVar[FakeClock]
    POST: ClassVar[dict[str, Any]] = {
        "id": "p1",
        "nr": 31,
        "type": "note",
        "tags": ["instructor-note"],
        "created": "2026-10-20T14:00:00Z",
        "change_log": [{"uid": "prof"}],
        "history": [
            {
                "subject": "Quiz B on Friday",
                "content": "<p>Quiz B will be this Friday at 10am in class.</p>",
                "created": "2026-10-20T14:00:00Z",
            }
        ],
        "children": [],
    }

    def login(self, email: str, password: str) -> None:
        return None

    def feed(self, nid: str, limit: int) -> list[dict[str, Any]]:
        if self.clock.now() < at(1, 10):
            return []
        return [
            {
                "id": "p1",
                "nr": 31,
                "subject": "Quiz B on Friday",
                "tags": ["instructor-note"],
                "pin": 0,
                "updated": "2026-10-20T14:00:00Z",
            }
        ]

    def post(self, nid: str, cid: str) -> dict[str, Any]:
        return self.POST

    def users(self, nid: str, uids: list[str]) -> list[dict[str, Any]]:
        return [{"id": "prof", "role": "professor"}]


def model(req: dict[str, Any]) -> Any:
    system = req["system"]
    if "course announcements" in system:
        if "Quiz B will be this Friday" in req["user"]:
            return ok(
                {
                    "findings": [
                        {
                            "kind": "new_assessment",
                            "course": "CMSC351",
                            "title": "Quiz B",
                            "date": "2026-10-23T10:00:00-04:00",
                            "scope": None,
                            "quote": "Quiz B will be this Friday at 10am",
                            "confidence": 0.95,
                        }
                    ]
                }
            )
        return ok({"findings": []})
    if "estimate how long" in system:
        return ok(
            {
                "p50_hours": 2.0,
                "p80_hours": 3.0,
                "confidence": "medium",
                "breakdown": [{"step": "Work", "hours": 2.0}],
                "assumptions": ["Typical length"],
                "reasoning": "Typical.",
            },
            model="claude-sonnet-5-5",
        )
    return ok({"line": "Sessions are planned before each deadline."})


# Evenings Mon-Thu 7-11 pm and Sunday 1-6 pm, like the demo.
WINDOWS = [
    WorkWindowConfig(days=["Mon", "Tue", "Wed", "Thu"], start=time(19), end=time(23)),
    WorkWindowConfig(days=["Sun"], start=time(13), end=time(18)),
]
Student = Callable[[sqlite3.Connection, FakeClock], None]


def with_windows(app_config: AppConfig) -> AppConfig:
    return app_config.model_copy(
        update={"schedule": app_config.schedule.model_copy(update={"work_windows": WINDOWS})}
    )


async def run_week(
    tmp_path: Any, app_config: AppConfig, *, student: Student | None = None
) -> tuple[sqlite3.Connection, list[tuple[datetime, dict[str, Any]]]]:
    """Run the worker every 15 minutes from START to END. Returns the db and every ntfy send.
    `student` runs before each tick (e.g. answering "did you work on it?" after a window)."""
    clock = FakeClock(START)
    FakeGS.clock = clock
    FakePiazza.clock = clock
    conn = open_db(tmp_path / "week.db", clock)
    secrets = Secrets(
        canvas_ical_url=SecretStr(FEED),
        gradescope_email="s@example.test",
        gradescope_password=SecretStr("pw"),
        piazza_email="s@example.test",
        piazza_password=SecretStr("pw"),
        ntfy_topic=SecretStr("argus-week"),
    )
    lecture = MeetingRecord(
        "CMSC351", "lecture", (Weekday.TUE, Weekday.THU), time(9, 30), time(10, 45)
    )
    with transaction(conn):
        ids = repo.sync_courses(conn, app_config, clock.now())
        repo.replace_meetings(conn, ids["CMSC351"], "umdio", [lecture], clock.now())
        # Pretend umd.io synced, so the worker does not try the network.
        repo.record_sync_run(
            conn, source="umdio", started_at=clock.now(), finished_at=clock.now(), status="ok"
        )

    sent: list[tuple[datetime, dict[str, Any]]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        sent.append((clock.now(), json.loads(request.content)))
        return httpx.Response(200, json={"id": "x"})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(FEED).mock(
            side_effect=lambda request: httpx.Response(200, content=feed(clock.now()))
        )
        mock.post(NTFY).mock(side_effect=capture)
        async with httpx.AsyncClient() as client:

            def runner(fetcher: Fetcher) -> SyncRunner:
                return SyncRunner(
                    conn,
                    app_config,
                    secrets,
                    clock,
                    fetcher,
                    collectors=[
                        CanvasIcalCollector(),
                        GradescopeCollector(FakeGS),
                        PiazzaCollector(FakePiazza),
                    ],
                )

            worker = Worker(
                conn,
                app_config,
                secrets,
                clock,
                client,
                runner_factory=runner,
                transport=FakeTransport(responder=model),
            )
            done_marked = False
            while clock.now() <= END:
                if not done_marked and clock.now() >= at(2, 15):
                    hw_a = next(i for i in repo.list_items(conn) if i.title == "Homework A")
                    with transaction(conn):
                        repo.set_user_done(
                            conn, hw_a.id, True, clock.now()
                        )  # submitted Wednesday afternoon
                    done_marked = True
                if student is not None:
                    student(conn, clock)
                await worker.tick()
                clock.advance(timedelta(minutes=15))

    return conn, sent
