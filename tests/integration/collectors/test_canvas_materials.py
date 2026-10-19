from collections.abc import Callable

import respx

from argus.collectors.base import CollectContext
from argus.collectors.canvas_modules import CanvasModulesCollector
from argus.domain.models import Source
from tests.material_fakes import pdf, pptx

API = "https://umd.instructure.com/api/v1/courses"
TOKEN = "synthetic-canvas-token-0000"


def mock(module_items: list[dict]) -> None:  # type: ignore[type-arg]
    respx.get(f"{API}/1234567/modules").respond(
        200, json=[{"id": 301, "name": "Week 1", "position": 1, "items": module_items}]
    )
    respx.get(f"{API}/1234567").respond(200, json={"syllabus_body": None})
    respx.get(f"{API}/1234567/enrollments").respond(200, json=[])
    respx.get(f"{API}/1234568/modules").respond(200, json=[])
    respx.get(f"{API}/1234568").respond(200, json={"syllabus_body": None})
    respx.get(f"{API}/1234568/enrollments").respond(200, json=[])


ITEMS = [
    {
        "id": 1,
        "title": "Lecture 1 slides",
        "type": "File",
        "html_url": "https://umd.instructure.com/courses/1234567/modules/items/1",
        "url": f"{API}/1234567/files/9001",
    },
    {
        "id": 2,
        "title": "Reading notes",
        "type": "File",
        "html_url": "https://umd.instructure.com/x/2",
        "url": f"{API}/1234567/files/9002",
    },
    {
        "id": 3,
        "title": "Syllabus page",
        "type": "Page",
        "html_url": "https://umd.instructure.com/x/3",
        "url": f"{API}/1234567/pages/intro",
    },
    {
        "id": 4,
        "title": "Photo",
        "type": "File",
        "html_url": "https://umd.instructure.com/x/4",
        "url": f"{API}/1234567/files/9004",
    },
    {
        "id": 5,
        "title": "Evil",
        "type": "File",
        "html_url": "https://x",
        "url": "https://evil.example/api/v1/files/1",
    },
]


@respx.mock
async def test_materials_are_extracted_and_files_not_kept(
    make_ctx: Callable[..., CollectContext],
) -> None:
    mock(ITEMS)
    respx.get(f"{API}/1234567/files/9001").respond(
        200,
        json={
            "filename": "l1.pptx",
            "content-type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "size": 1000,
            "url": "https://umd.instructure.com/files/9001/download?verifier=v",
            "updated_at": "2026-09-01T00:00:00Z",
        },
    )
    respx.get("https://umd.instructure.com/files/9001/download").respond(
        200, content=pptx([("Big-O", "Upper bounds")])
    )
    s3 = respx.get("https://uploads.example/9002.pdf").respond(
        200, content=pdf(["Omega means lower bound"])
    )
    respx.get(f"{API}/1234567/files/9002").respond(
        200,
        json={
            "filename": "r.pdf",
            "content-type": "application/pdf",
            "size": 900,
            "url": "https://uploads.example/9002.pdf",
            "updated_at": "2026-09-02T00:00:00Z",
        },
    )
    respx.get(f"{API}/1234567/pages/intro").respond(
        200,
        json={
            "title": "Intro",
            "body": "<p>Welcome to algorithms.</p>",
            "updated_at": "2026-09-03T00:00:00Z",
        },
    )
    respx.get(f"{API}/1234567/files/9004").respond(
        200,
        json={
            "filename": "a.png",
            "content-type": "image/png",
            "size": 10,
            "url": "https://umd.instructure.com/files/9004/download",
            "updated_at": "x",
        },
    )
    evil = respx.get("https://evil.example/api/v1/files/1").respond(200, json={})

    result = await CanvasModulesCollector().collect(make_ctx(canvas_token=TOKEN))
    mats = {m.title: m for m in result.materials}
    assert set(mats) == {"Lecture 1 slides", "Reading notes", "Syllabus page"}
    assert mats["Lecture 1 slides"].pages[0].ref == "slide 1"
    assert mats["Reading notes"].pages[0].text == "Omega means lower bound"
    assert mats["Lecture 1 slides"].url.endswith(
        "/modules/items/1"
    )  # students open Canvas, not a copy
    assert not evil.called  # never contact other hosts with the token
    assert (
        "Authorization" not in s3.calls[0].request.headers
    )  # off-Canvas download without the token

    # Second run: unchanged files are skipped.
    states = {(Source.CANVAS_MODULES.value, k): v for k, v in result.state_updates.items()}
    calls_before = len(respx.calls)
    again = await CanvasModulesCollector().collect(make_ctx(states, canvas_token=TOKEN))
    assert again.materials == ()
    assert not any("download" in str(c.request.url) for c in list(respx.calls)[calls_before:])
