"""Today's rows and exam practice, driven like a person would, each flow with JS and without it.
Today lists the week's deadlines in order and ranks nothing (D072)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import LiveServer, today_state

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


def test_mark_done_from_today(
    qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/today")
    row = qpage.locator("#item-6")  # Problem Set 6, due tonight
    row.hover()
    button = row.get_by_role("button", name="Mark done Problem Set 6", exact=True)
    if js:
        button.focus()
        qpage.keyboard.press("Enter")
        expect(qpage.get_by_text("Marked Problem Set 6 done")).to_be_visible()  # the toast
        expect(qpage.locator("#item-6")).to_have_attribute("data-done", "true")
    else:
        button.click()
        expect(qpage).to_have_url(re.compile(r"/today$"))
        expect(qpage.locator("#item-6")).to_have_count(0)  # done work leaves the week
    done = sqlite3.connect(s.db).execute("SELECT user_done_at FROM items WHERE id = 6")
    assert done.fetchone()[0] is not None


def test_the_morning_tag(qpage: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    today_state(s, "morning")
    qpage.goto(s.url + "/today")
    region = qpage.get_by_role("region", name=re.compile("^Today"))
    row = region.locator("li.item-row", has_text="Pre-lab quiz")
    expect(row).to_contain_text("Tomorrow 7:30 am")
    expect(row).to_contain_text("Next morning")


def test_practice_self_grading(
    qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/exams/5")
    card = qpage.locator("article.practice-card").first
    card.locator("summary").click()
    card.get_by_role("button", name="Got it").click()
    if js:
        expect(qpage.locator("article.practice-card").first).to_contain_text("Marked as got it.")
    else:
        expect(qpage).to_have_url(re.compile(r"/exams/5$"))
    count = sqlite3.connect(s.db).execute(
        "SELECT COUNT(*) FROM practice_attempts WHERE result = 'correct'"
    )
    assert count.fetchone()[0] >= 1
