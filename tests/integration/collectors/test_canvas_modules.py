from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import respx

from argus.collectors.base import CollectContext, RunStatus
from argus.collectors.canvas_modules import CanvasModulesCollector, parse_grade
from argus.collectors.http import AuthError
from argus.domain.models import Source
from tests.integration.conftest import fixture_bytes

API = "https://umd.instructure.com/api/v1/courses"
TOKEN = "synthetic-canvas-token-0000"


def mock_modules() -> None:
    respx.get(f"{API}/1234567/modules").respond(
        200, content=fixture_bytes("canvas_modules", "cmsc351_modules.json")
    )
    items = f"{API}/1234567/modules/302/items"
    respx.get(items, params={"page": "2"}).respond(
        200, content=fixture_bytes("canvas_modules", "cmsc351_module302_items_p2.json")
    )
    respx.get(items).respond(
        200,
        content=fixture_bytes("canvas_modules", "cmsc351_module302_items_p1.json"),
        headers={"Link": f'<{items}?page=2>; rel="next"'},
    )
    respx.get(f"{API}/1234567").respond(
        200, content=fixture_bytes("canvas_modules", "cmsc351_course.json")
    )
    respx.get(f"{API}/1234567/enrollments").respond(
        200, content=fixture_bytes("canvas_modules", "cmsc351_enrollments.json")
    )
    respx.get(f"{API}/1234568/modules").respond(200, json=[])
    respx.get(f"{API}/1234568").respond(200, json={"id": 1234568, "syllabus_body": None})
    respx.get(f"{API}/1234568/enrollments").respond(200, json=[])


@respx.mock
async def test_modules_syllabus_and_grade(make_ctx: Callable[..., CollectContext]) -> None:
    mock_modules()
    collector = CanvasModulesCollector()
    ctx = make_ctx(canvas_token=TOKEN)
    assert collector.enabled(ctx)
    result = await collector.collect(ctx)
    assert result.status == RunStatus.OK
    week1, week2 = result.modules["CMSC351"]
    assert week1.name == "Week 1: Asymptotics"
    assert week1.state == "completed"
    assert [(i.type, i.indent) for i in week1.items] == [
        ("SubHeader", 0),
        ("File", 1),
        ("ExternalUrl", 1),
    ]
    assert week1.items[2].url == "https://example.test/growth"  # external links go straight out
    assert week1.items[1].url.endswith("/modules/items/2")  # files stay in Canvas
    assert [i.title for i in week2.items] == [
        "Lecture 4: the master theorem",
        "Recurrence practice (ungraded)",
        "Homework 2",
    ]
    assert week2.unlock_at == datetime(2026, 9, 7, 4, tzinfo=UTC)
    assert result.modules["MATH240"] == ()
    [grade] = result.grades
    assert (grade.course_code, grade.score, grade.letter) == ("CMSC351", 91.5, "A-")
    [syllabus] = result.posts
    assert syllabus.kind == "syllabus"
    assert syllabus.via == Source.CANVAS_MODULES
    assert syllabus.body.startswith("Grading\n- Homework 30%")


@respx.mock
async def test_unchanged_courses_are_skipped(make_ctx: Callable[..., CollectContext]) -> None:
    mock_modules()
    first = await CanvasModulesCollector().collect(make_ctx(canvas_token=TOKEN))
    states = {(Source.CANVAS_MODULES.value, k): v for k, v in first.state_updates.items()}
    second = await CanvasModulesCollector().collect(make_ctx(states, canvas_token=TOKEN))
    assert second.status == RunStatus.UNCHANGED
    assert second.modules == {}


@respx.mock
async def test_revoked_token(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(f"{API}/1234567/modules").respond(401)
    with pytest.raises(AuthError):
        await CanvasModulesCollector().collect(make_ctx(canvas_token=TOKEN))


@respx.mock
async def test_malformed_modules(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(f"{API}/1234567/modules").respond(200, json={"errors": []})
    with pytest.raises(ValueError, match="list of modules"):
        await CanvasModulesCollector().collect(make_ctx(canvas_token=TOKEN))


def test_disabled_without_token(make_ctx: Callable[..., CollectContext]) -> None:
    assert not CanvasModulesCollector().enabled(make_ctx())


@pytest.mark.parametrize("payload", [[], {}, [{"grades": {}}]])
def test_grade_edge_cases(payload: object) -> None:
    grade = parse_grade(payload, "C")
    assert grade is None or (grade.score is None and grade.letter is None)
