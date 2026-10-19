from collections.abc import Callable
from datetime import time

import pytest
import respx

from argus.collectors.base import CollectContext, RunStatus
from argus.collectors.umdio import API, UmdioCollector, describe, parse_days, parse_time
from argus.domain.models import Source, Weekday
from tests.integration.conftest import fixture_bytes


def mock_umdio() -> respx.Route:
    route = respx.get(f"{API}/courses/CMSC351/sections/0101").respond(
        200, content=fixture_bytes("umdio", "cmsc351_sections.json")
    )
    respx.get(f"{API}/courses/CMSC351").respond(
        200, content=fixture_bytes("umdio", "cmsc351_course.json")
    )
    respx.get(f"{API}/courses/MATH240/sections/0201").respond(
        200, content=fixture_bytes("umdio", "math240_sections.json")
    )
    respx.get(f"{API}/courses/MATH240").respond(
        200, content=fixture_bytes("umdio", "math240_course.json")
    )
    return route


@respx.mock
async def test_collect_meetings_and_titles(make_ctx: Callable[..., CollectContext]) -> None:
    mock_umdio()
    result = await UmdioCollector().collect(make_ctx())
    assert result.status == RunStatus.OK
    cmsc = result.meetings["CMSC351"]
    assert [(m.kind, m.days, m.start, m.end) for m in cmsc] == [
        ("lecture", (Weekday.TUE, Weekday.THU), time(9, 30), time(10, 45)),
        ("discussion", (Weekday.FRI,), time(12, 0), time(12, 50)),
    ]  # the ONLINE row has no times and is skipped
    assert describe(cmsc[0]) == "TuTh 09:30-10:45 IRB 1101"
    math = result.meetings["MATH240"]
    assert math[0].days == (Weekday.MON, Weekday.WED, Weekday.FRI)
    assert math[1].kind == "lab"
    titles = {c.course_code: c.title for c in result.courses}
    assert titles == {"CMSC351": "Algorithms", "MATH240": "Introduction to Linear Algebra"}
    assert result.courses[0].instructors == ("Synthetic Professor",)


@respx.mock
async def test_semester_param_and_unchanged(
    make_ctx: Callable[..., CollectContext], app_config: object
) -> None:
    route = mock_umdio()
    ctx = make_ctx()
    ctx = type(ctx)(**{**ctx.__dict__, "config": ctx.config.model_copy(update={"term": "202608"})})
    first = await UmdioCollector().collect(ctx)
    assert route.calls[0].request.url.params["semester"] == "202608"
    states = {(Source.UMDIO.value, k): v for k, v in first.state_updates.items()}
    second = await UmdioCollector().collect(type(ctx)(**{**ctx.__dict__, "states": states}))
    assert second.status == RunStatus.UNCHANGED
    assert second.meetings == {}


@respx.mock
async def test_missing_section_is_error(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(f"{API}/courses/CMSC351/sections/0101").respond(200, json=[])
    respx.get(f"{API}/courses/CMSC351").respond(200, json=[])
    with pytest.raises(ValueError, match="no section CMSC351-0101"):
        await UmdioCollector().collect(make_ctx())


@respx.mock
async def test_malformed_payload_is_error(make_ctx: Callable[..., CollectContext]) -> None:
    respx.get(f"{API}/courses/CMSC351/sections/0101").respond(200, json={"error_code": 404})
    respx.get(f"{API}/courses/CMSC351").respond(200, json=[])
    with pytest.raises(ValueError, match="non-list"):
        await UmdioCollector().collect(make_ctx())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9:30am", time(9, 30)),
        ("12:00pm", time(12)),
        ("12:15am", time(0, 15)),
        ("1:05PM", time(13, 5)),
        ("10:00 a.m.", time(10)),
    ],
)
def test_parse_time(raw: str, expected: time) -> None:
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["", "13:00pm", "9.30am", "noon", "0:30am"])
def test_parse_time_rejects(raw: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_time(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MWF", (Weekday.MON, Weekday.WED, Weekday.FRI)),
        ("TuTh", (Weekday.TUE, Weekday.THU)),
        ("SaSu", (Weekday.SAT, Weekday.SUN)),
        ("TBA", ()),
        ("", ()),
    ],
)
def test_parse_days(raw: str, expected: tuple[Weekday, ...]) -> None:
    assert parse_days(raw) == expected


def test_enabled_needs_courses(make_ctx: Callable[..., CollectContext]) -> None:
    assert UmdioCollector().enabled(make_ctx())
