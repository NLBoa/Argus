import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import respx

from argus.collectors.base import CollectContext, RunStatus
from argus.collectors.canvas_api import CanvasApiCollector, parse_assignment
from argus.collectors.http import AuthError
from argus.collectors.text import html_to_text
from argus.domain.models import ItemKind, Source
from tests.integration.conftest import fixture_bytes

BASE = "https://umd.instructure.com/api/v1/courses"
TOKEN = "synthetic-canvas-token-0000"


def mock_canvas() -> respx.Route:
    nxt = f'<{BASE}/1234567/assignments?page=2&per_page=100>; rel="next"'
    route = respx.get(f"{BASE}/1234567/assignments", params={"page": "2"}).respond(
        200, content=fixture_bytes("canvas_api", "cmsc351_page2.json")
    )
    respx.get(f"{BASE}/1234567/assignments").respond(
        200, content=fixture_bytes("canvas_api", "cmsc351_page1.json"), headers={"Link": nxt}
    )
    respx.get(f"{BASE}/1234568/assignments").respond(
        200, content=fixture_bytes("canvas_api", "math240_page1.json")
    )
    return route


@respx.mock
async def test_collect_follows_pagination_and_parses(
    make_ctx: Callable[..., CollectContext],
) -> None:
    page2 = mock_canvas()
    result = await CanvasApiCollector().collect(make_ctx(canvas_token=TOKEN))
    assert result.status == RunStatus.OK
    assert page2.called
    assert page2.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    items = {i.external_id: i for i in result.items}
    assert set(items) == {"7000101", "7000102", "7000099", "7000098", "7100001"}
    assert set(result.complete_scopes) == {"CMSC351", "MATH240"}

    hw3 = items["7000101"]
    assert hw3.source == Source.CANVAS_API
    assert hw3.submitted is False and hw3.graded is False
    assert hw3.late_due_at == datetime(2026, 10, 25, 3, 59, tzinfo=UTC)
    assert hw3.release_at == datetime(2026, 10, 16, 13, tzinfo=UTC)
    assert hw3.description == "Prove the three lemmas from lecture 9.\n- Lemma A\n- Lemma B"
    assert hw3.points == 30.0
    quiz = items["7000102"]
    assert quiz.kind == ItemKind.QUIZ
    assert quiz.late_due_at is None  # lock before due is not a late window
    assert items["7000099"].graded is True and items["7000099"].submitted is True
    assert items["7000098"].submitted is None  # no-submission assignment
    assert items["7000098"].due_at is None
    assert items["7100001"].submitted is True and items["7100001"].graded is False


@respx.mock
async def test_unchanged_courses_are_skipped(make_ctx: Callable[..., CollectContext]) -> None:
    mock_canvas()
    first = await CanvasApiCollector().collect(make_ctx(canvas_token=TOKEN))
    states = {(Source.CANVAS_API.value, k): v for k, v in first.state_updates.items()}
    second = await CanvasApiCollector().collect(make_ctx(states, canvas_token=TOKEN))
    assert second.status == RunStatus.UNCHANGED
    assert second.items == ()
    assert second.complete_scopes == {}


@respx.mock
async def test_revoked_token_is_auth_error(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(f"{BASE}/1234567/assignments").respond(
        401, json={"errors": [{"message": "Invalid access token."}]}
    )
    with pytest.raises(AuthError):
        await CanvasApiCollector().collect(make_ctx(canvas_token=TOKEN))


@respx.mock
async def test_non_list_payload_is_error(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(f"{BASE}/1234567/assignments").respond(200, json={"status": "unauthenticated"})
    with pytest.raises(ValueError, match="expected a list"):
        await CanvasApiCollector().collect(make_ctx(canvas_token=TOKEN))


@respx.mock
async def test_empty_course(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(f"{BASE}/1234567/assignments").respond(200, json=[])
    respx.get(f"{BASE}/1234568/assignments").respond(200, json=[])
    result = await CanvasApiCollector().collect(make_ctx(canvas_token=TOKEN))
    assert result.status == RunStatus.OK
    assert result.items == ()
    assert set(result.complete_scopes) == {"CMSC351", "MATH240"}


def test_disabled_without_token(make_ctx: Callable[..., CollectContext]) -> None:
    assert not CanvasApiCollector().enabled(make_ctx())
    assert CanvasApiCollector().enabled(make_ctx(canvas_token=TOKEN))


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError, match="without zone"):
        parse_assignment({"id": 1, "name": "x", "due_at": "2026-10-23T03:59:00"}, "CMSC351")


def test_missing_submission_block_is_unknown() -> None:
    item = parse_assignment(
        {"id": 1, "name": "x", "submission_types": ["online_upload"]}, "CMSC351"
    )
    assert item.submitted is None and item.graded is None


def test_html_to_text() -> None:
    assert html_to_text(None) is None
    assert html_to_text("<p> </p>") is None
    assert html_to_text("<p>a</p><p>b</p>") == "a\nb"
    assert html_to_text("line one<br>line <em>two</em>") == "line one\nline two"


def test_raw_hash_ignores_key_order() -> None:
    a = {"id": 1, "name": "x"}
    b = json.loads('{"name": "x", "id": 1}')
    assert parse_assignment(a, "C").raw_hash == parse_assignment(b, "C").raw_hash
