"""The sync runner end to end: fixtures -> collectors -> store -> merge -> status."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
import respx
from pydantic import SecretStr

from argus.clock import LOCAL_TZ, FakeClock
from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.canvas_api import CanvasApiCollector
from argus.collectors.canvas_ical import CanvasIcalCollector
from argus.collectors.gradescope import GradescopeCollector, GSAssignment
from argus.collectors.http import AuthError, Fetcher, make_client
from argus.collectors.piazza import PiazzaCollector
from argus.collectors.piazza_email import GMAIL, PiazzaEmailCollector
from argus.collectors.runner import SyncRunner, run_collector
from argus.collectors.umdio import API as UMDIO
from argus.collectors.umdio import UmdioCollector
from argus.config import AppConfig, Secrets
from argus.domain.models import ItemKind, ItemStatus, Source
from argus.logging import redactor
from argus.store import repo
from tests.integration.collectors.test_piazza_email import MESSAGES
from tests.integration.conftest import fixture_bytes

FEED_URL = "https://umd.instructure.com/feeds/calendars/user_FAKEFEED.ics"
CANVAS = "https://umd.instructure.com/api/v1/courses"


def edt(*a: int) -> datetime:
    return datetime(*a, tzinfo=LOCAL_TZ)


class FakeGradescope:
    data: ClassVar[dict[str, list[GSAssignment]]] = {
        "765432": [
            GSAssignment(
                "5550003",
                "HW3",
                edt(2026, 10, 16, 9),
                edt(2026, 10, 22, 23, 30),
                edt(2026, 10, 24, 23, 59),
                "No Submission",
                None,
                30.0,
            ),
            GSAssignment(
                "5550002", "Homework 2", None, edt(2026, 10, 8, 23, 59), None, None, 27.5, 30.0
            ),
            GSAssignment(
                "5550004",
                "Programming Project 1",
                None,
                edt(2026, 10, 30, 23, 59),
                None,
                "Submitted",
                None,
                50.0,
            ),
        ],
        "765433": [],
    }
    error: ClassVar[Exception | None] = None

    def login(self, email: str, password: str) -> None:
        if FakeGradescope.error:
            raise FakeGradescope.error

    def assignments(self, course_id: str) -> list[GSAssignment]:
        return FakeGradescope.data[course_id]


class RejectingPiazza:
    def login(self, email: str, password: str) -> None:
        raise AuthError("Piazza rejected the credentials")

    def feed(self, nid: str, limit: int) -> list[dict[str, Any]]:
        raise AssertionError("unreachable")

    def post(self, nid: str, cid: str) -> dict[str, Any]:
        raise AssertionError("unreachable")

    def users(self, nid: str, uids: list[str]) -> list[dict[str, Any]]:
        raise AssertionError("unreachable")


def mock_sources(feed: bytes) -> None:
    respx.get(FEED_URL).respond(200, content=feed)
    respx.get(f"{CANVAS}/1234567/assignments", params={"page": "2"}).respond(
        200, content=fixture_bytes("canvas_api", "cmsc351_page2.json")
    )
    respx.get(f"{CANVAS}/1234567/assignments").respond(
        200,
        content=fixture_bytes("canvas_api", "cmsc351_page1.json"),
        headers={"Link": f'<{CANVAS}/1234567/assignments?page=2>; rel="next"'},
    )
    respx.get(f"{CANVAS}/1234568/assignments").respond(
        200, content=fixture_bytes("canvas_api", "math240_page1.json")
    )
    respx.get("https://umd.instructure.com/api/v1/announcements").respond(
        200, content=fixture_bytes("canvas_api", "announcements.json")
    )
    for code, section in (("CMSC351", "0101"), ("MATH240", "0201")):
        low = code.lower()
        respx.get(f"{UMDIO}/courses/{code}/sections/{section}").respond(
            200, content=fixture_bytes("umdio", f"{low}_sections.json")
        )
        respx.get(f"{UMDIO}/courses/{code}").respond(
            200, content=fixture_bytes("umdio", f"{low}_course.json")
        )
    respx.post("https://oauth2.googleapis.com/token").respond(200, json={"access_token": "atok"})
    respx.get(GMAIL).respond(200, json={"messages": [{"id": m} for m in MESSAGES]})
    for mid, msg in MESSAGES.items():
        respx.get(f"{GMAIL}/{mid}").respond(200, json=msg)


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def secrets(tmp_path: Path) -> Secrets:
    token = tmp_path / "gmail.json"
    token.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    return Secrets(
        canvas_ical_url=SecretStr(FEED_URL),
        canvas_token=SecretStr("synthetic-canvas-token"),
        gradescope_email="student@example.test",
        gradescope_password=SecretStr("synthetic-gs-password"),
        piazza_email="student@example.test",
        piazza_password=SecretStr("pw"),
        gmail_token_path=token,
    )


@pytest.fixture
def runner(
    db: sqlite3.Connection, app_config: AppConfig, secrets: Secrets, clock: FakeClock
) -> SyncRunner:
    FakeGradescope.error = None
    return SyncRunner(
        db,
        app_config,
        secrets,
        clock,
        Fetcher(make_client(), sleep=_no_sleep, rand=lambda: 0.0),
        collectors=[
            CanvasIcalCollector(),
            CanvasApiCollector(),
            GradescopeCollector(FakeGradescope),
            PiazzaCollector(RejectingPiazza),
            PiazzaEmailCollector(),
            UmdioCollector(),
        ],
    )


def statuses(report_results: list[CollectResult]) -> dict[Source, RunStatus]:
    return {r.source: r.status for r in report_results}


@respx.mock
async def test_first_cycle_end_to_end(runner: SyncRunner, db: sqlite3.Connection) -> None:
    mock_sources(fixture_bytes("canvas_ical", "feed.ics"))
    report = await runner.run_cycle()
    assert statuses(report.results) == {
        Source.CANVAS_ICAL: RunStatus.OK,
        Source.CANVAS_API: RunStatus.OK,
        Source.GRADESCOPE: RunStatus.OK,
        Source.PIAZZA: RunStatus.AUTH_ERROR,
        Source.PIAZZA_EMAIL: RunStatus.OK,  # fallback ran in the same cycle
        Source.UMDIO: RunStatus.OK,
    }
    items = repo.list_items(db)
    assert len(items) == len(report.new_items) == 9
    by_title = {(i.course_code, i.title): i for i in items}

    hw3 = by_title[("CMSC351", "Homework 3")]
    assert {link.source for link in hw3.links} == {
        Source.CANVAS_API,
        Source.CANVAS_ICAL,
        Source.GRADESCOPE,
    }
    assert hw3.due_at == datetime(2026, 10, 23, 3, 30, tzinfo=UTC)  # Gradescope's earlier time
    assert hw3.date_conflict  # Canvas says 23:59, Gradescope 23:30
    assert hw3.description and "Lemma A" in hw3.description

    assert by_title[("CMSC351", "Quiz 2")].kind == ItemKind.QUIZ
    hw2 = by_title[("CMSC351", "Homework 2")]
    assert hw2.status == ItemStatus.GRADED
    assert len(hw2.links) == 2
    assert by_title[("MATH240", "Midterm 1")].kind == ItemKind.EXAM
    assert by_title[("CMSC351", "Programming Project 1")].status == ItemStatus.SUBMITTED

    assert {m.course_code for m in repo.list_meetings(db)} == {"CMSC351", "MATH240"}
    assert len(report.new_posts) == 4  # 2 Canvas announcements + 2 Piazza emails
    health = repo.source_health(db, ["piazza"])["piazza"]
    assert health.last_status == "auth_error"
    assert health.failures == 1


@respx.mock
async def test_second_cycle_is_stable_and_polite(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    mock_sources(fixture_bytes("canvas_ical", "feed.ics"))
    await runner.run_cycle()
    ids_before = sorted(i.id for i in repo.list_items(db))
    clock.advance(timedelta(minutes=5))
    nothing_due = await runner.run_cycle()
    assert nothing_due.results == []  # every source polled in the current slot already
    clock.advance(timedelta(minutes=30))
    report = await runner.run_cycle()
    assert report.new_items == []
    assert sorted(i.id for i in repo.list_items(db)) == ids_before
    assert statuses(report.results)[Source.CANVAS_ICAL] == RunStatus.UNCHANGED
    assert Source.UMDIO not in statuses(report.results)  # weekly


@respx.mock
async def test_failing_collector_does_not_block_others(
    runner: SyncRunner, db: sqlite3.Connection, secrets: Secrets
) -> None:
    mock_sources(fixture_bytes("canvas_ical", "feed.ics"))
    redactor.register(secrets.secret_values())  # as configure_logging does at startup
    FakeGradescope.error = RuntimeError("scraper exploded on synthetic-gs-password")
    report = await runner.run_cycle()
    result = report.result(Source.GRADESCOPE)
    assert result is not None
    assert result.status == RunStatus.ERROR
    assert result.error == "RuntimeError: scraper exploded on [REDACTED]"
    stored = repo.source_health(db, ["gradescope"])["gradescope"]
    assert stored.last_error == "RuntimeError: scraper exploded on [REDACTED]"
    assert statuses(report.results)[Source.CANVAS_ICAL] == RunStatus.OK
    assert len(repo.list_items(db)) == 8  # everything but the Gradescope-only item
    assert repo.consecutive_failures(db, "gradescope") == 1


@respx.mock
async def test_one_wrong_section_keeps_the_other_class_times(
    runner: SyncRunner, db: sqlite3.Connection
) -> None:
    mock_sources(fixture_bytes("canvas_ical", "feed.ics"))
    respx.get(f"{UMDIO}/courses/MATH240/sections/0201").respond(404)  # replaces the good route
    report = await runner.run_cycle()
    result = report.result(Source.UMDIO)
    assert result is not None and result.status == RunStatus.ERROR
    assert {m.course_code for m in repo.list_meetings(db)} == {"CMSC351"}
    stored = repo.source_health(db, ["umdio"])["umdio"]
    assert stored.last_error is not None and "MATH240-0201" in stored.last_error


@respx.mock
async def test_assignment_removed_from_canvas_disappears(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    feed = fixture_bytes("canvas_ical", "feed.ics")
    mock_sources(feed)
    await runner.run_cycle()
    assert any(i.title == "Homework 4" for i in repo.list_items(db))
    start = feed.index(b"BEGIN:VEVENT\nUID:event-assignment-7000103")
    end = feed.index(b"END:VEVENT", start) + len(b"END:VEVENT") + 1
    respx.get(FEED_URL).respond(200, content=feed[:start] + feed[end:])
    clock.advance(timedelta(minutes=31))
    await runner.run_cycle(only={Source.CANVAS_ICAL})
    assert not any(i.title == "Homework 4" for i in repo.list_items(db))


@respx.mock
async def test_empty_listing_keeps_items_until_it_persists_a_day(
    runner: SyncRunner, db: sqlite3.Connection, clock: FakeClock
) -> None:
    """Review R6: an empty feed (broken page, login page) must not delete every deadline."""
    feed = fixture_bytes("canvas_ical", "feed.ics")
    mock_sources(feed)
    await runner.run_cycle()
    before = {i.title for i in repo.list_items(db)}
    assert "Homework 4" in before
    respx.get(FEED_URL).respond(200, content=b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
    for _ in range(3):
        clock.advance(timedelta(minutes=31))
        report = await runner.run_cycle(only={Source.CANVAS_ICAL})
        assert {i.title for i in repo.list_items(db)} == before
    [run] = repo.recent_sync_runs(db, "canvas_ical", limit=1)
    assert run.status == "error" and run.error and "vanished at once" in run.error
    assert repo.source_health(db, ["canvas_ical"])["canvas_ical"].failures == 3  # the alert fires
    clock.advance(timedelta(hours=25))  # still empty a day later: believe it
    report = await runner.run_cycle(only={Source.CANVAS_ICAL})
    assert report is not None
    assert "Homework 4" not in {i.title for i in repo.list_items(db)}
    assert repo.recent_sync_runs(db, "canvas_ical", limit=1)[0].status in ("ok", "unchanged")


@respx.mock
async def test_only_and_force(runner: SyncRunner, db: sqlite3.Connection) -> None:
    mock_sources(fixture_bytes("canvas_ical", "feed.ics"))
    report = await runner.run_cycle(only={Source.UMDIO})
    assert statuses(report.results) == {Source.UMDIO: RunStatus.OK}
    forced = await runner.run_cycle(force=True)
    assert Source.UMDIO in statuses(forced.results)
    email_only = await runner.run_cycle(only={Source.PIAZZA_EMAIL})
    assert statuses(email_only.results) == {Source.PIAZZA_EMAIL: RunStatus.UNCHANGED}


@respx.mock
async def test_email_fallback_not_used_when_piazza_works(
    db: sqlite3.Connection, app_config: AppConfig, secrets: Secrets, clock: FakeClock
) -> None:
    mock_sources(fixture_bytes("canvas_ical", "feed.ics"))

    class QuietPiazza(RejectingPiazza):
        def login(self, email: str, password: str) -> None:
            return None

        def feed(self, nid: str, limit: int) -> list[dict[str, Any]]:
            return []

    runner = SyncRunner(
        db,
        app_config,
        secrets,
        clock,
        Fetcher(make_client(), sleep=_no_sleep),
        collectors=[PiazzaCollector(QuietPiazza), PiazzaEmailCollector()],
    )
    report = await runner.run_cycle()
    assert statuses(report.results) == {Source.PIAZZA: RunStatus.UNCHANGED}


async def test_run_collector_timeout(make_ctx: Callable[..., CollectContext]) -> None:
    class Slow:
        source = Source.UMDIO

        def enabled(self, ctx: CollectContext) -> bool:
            return True

        async def collect(self, ctx: CollectContext) -> CollectResult:
            import asyncio

            await asyncio.sleep(10)
            raise AssertionError

    result = await run_collector(Slow(), make_ctx(), timeout=0.01)
    assert result.status == RunStatus.ERROR
    assert result.error == "timed out after 0s"
