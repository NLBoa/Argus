from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from argus.collectors.base import CollectContext, RunStatus
from argus.collectors.canvas_ical import CanvasIcalCollector, parse_feed
from argus.collectors.http import AuthError
from argus.config import AppConfig
from argus.domain.models import ItemKind, Source
from argus.store.repo import SourceState, sha256
from tests.integration.conftest import fixture_bytes

FEED_URL = "https://umd.instructure.com/feeds/calendars/user_FAKEFEED.ics"
FEED = fixture_bytes("canvas_ical", "feed.ics")


def test_parse_feed_maps_courses_ids_kinds_and_times(app_config: AppConfig) -> None:
    items = {i.external_id: i for i in parse_feed(FEED, app_config)}
    assert set(items) == {
        "7000101",
        "7000102",
        "7000103",
        "calendar-event-880001",
        "calendar-event-880002",
    }  # HIST289 is not configured, so it is skipped
    hw = items["7000101"]
    assert hw.course_code == "CMSC351"
    assert hw.title == "Homework 3"
    assert hw.kind == ItemKind.ASSIGNMENT
    assert hw.due_at == datetime(2026, 10, 23, 3, 59, tzinfo=UTC)  # UTC as published
    assert hw.description and hw.description.startswith("Prove the three lemmas")
    assert hw.url == "https://umd.instructure.com/courses/1234567/assignments/7000101"
    assert items["7000102"].kind == ItemKind.QUIZ
    midterm = items["calendar-event-880001"]
    assert midterm.course_code == "MATH240"
    assert midterm.kind == ItemKind.EXAM
    allday = items["calendar-event-880002"]
    assert allday.all_day
    assert allday.course_code == "MATH240"  # from the [MATH240-…] bracket: no URL
    assert allday.due_at == datetime(2026, 10, 31, 3, 59, tzinfo=UTC)  # 23:59 EDT Oct 30
    assert allday.kind == ItemKind.EVENT


def test_parse_feed_hashes_each_event_stably(app_config: AppConfig) -> None:
    a = parse_feed(FEED, app_config)
    b = parse_feed(FEED, app_config)
    assert [i.raw_hash for i in a] == [i.raw_hash for i in b]
    assert len({i.raw_hash for i in a}) == len(a)


def test_empty_feed(app_config: AppConfig) -> None:
    assert parse_feed(fixture_bytes("canvas_ical", "empty.ics"), app_config) == []


@pytest.mark.parametrize(
    "body", [b"", b"<html>login</html>", fixture_bytes("canvas_ical", "malformed.ics")]
)
def test_malformed_feed_raises_value_error(app_config: AppConfig, body: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_feed(body, app_config)


@respx.mock
async def test_collect_ok_then_unchanged(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(FEED_URL).respond(200, content=FEED, headers={"ETag": '"v1"'})
    collector = CanvasIcalCollector()
    ctx = make_ctx(canvas_ical_url=FEED_URL)
    assert collector.enabled(ctx)
    first = await collector.collect(ctx)
    assert first.status == RunStatus.OK
    assert len(first.items) == 5
    assert set(first.complete_scopes) == {"CMSC351", "MATH240"}
    assert first.state_updates["feed"].etag == '"v1"'

    # Same bytes again (server ignored the ETag): detected via hash.
    second = await collector.collect(
        make_ctx(
            {(Source.CANVAS_ICAL.value, "feed"): first.state_updates["feed"]},
            canvas_ical_url=FEED_URL,
        )
    )
    assert second.status == RunStatus.UNCHANGED
    assert second.items == ()


@respx.mock
async def test_collect_sends_conditional_headers_and_handles_304(
    make_ctx: Callable[..., CollectContext],
) -> None:
    route = respx.get(FEED_URL).respond(304)
    prev = SourceState(payload_hash=sha256(FEED), etag='"v1"')
    result = await CanvasIcalCollector().collect(
        make_ctx({(Source.CANVAS_ICAL.value, "feed"): prev}, canvas_ical_url=FEED_URL)
    )
    assert result.status == RunStatus.UNCHANGED
    assert route.calls[0].request.headers["If-None-Match"] == '"v1"'


@respx.mock
async def test_collect_auth_failure(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError) as e:
        await CanvasIcalCollector().collect(make_ctx(canvas_ical_url=FEED_URL))
    assert "FAKEFEED" not in str(e.value)


def test_disabled_without_feed_url(make_ctx: Callable[..., CollectContext]) -> None:
    assert not CanvasIcalCollector().enabled(make_ctx())
