"""Editing an exam you added (D076), driven like a person would, with JS and without it."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import LiveServer, set_up_pages

pytestmark = pytest.mark.e2e


@pytest.fixture(params=["js", "nojs"])
def js(request: pytest.FixtureRequest) -> bool:
    return request.param == "js"


@pytest.fixture
def qpage(browser: Browser, js: bool) -> Iterator[Page]:
    ctx = browser.new_context(java_script_enabled=js, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    yield page
    ctx.close()


def test_edit_an_exam_you_added(
    qpage: Page, fresh_server: Callable[..., LiveServer], js: bool
) -> None:
    s = fresh_server()
    exam = set_up_pages(s)["exam-added"]
    qpage.goto(s.url + exam)
    qpage.get_by_role("link", name="Edit exam").click()
    expect(qpage.get_by_role("heading", name="Edit exam", level=1)).to_be_visible()
    expect(qpage.get_by_label("What")).to_have_value("Linear systems quiz")
    qpage.get_by_label("What").fill("Linear systems retake")
    qpage.get_by_label("Starts at").fill("14:30")
    qpage.get_by_label("Study time, in hours").fill("5")
    qpage.get_by_role("button", name="Save changes").click()
    expect(qpage.get_by_role("heading", name="Linear systems retake", level=1)).to_be_visible()
    expect(qpage).to_have_url(re.compile(re.escape(exam) + "$"))
    expect(qpage.get_by_text("2:30 pm")).to_be_visible()
    if js:
        expect(qpage.locator(".toast", has_text="Saved Linear systems retake")).to_be_visible()
