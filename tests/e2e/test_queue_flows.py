"""Today's queue, driven like a person would (D058, D064), each flow with JS and without it.

The demo starts with a timer running on the essay, so the hero is that timer and the list below
it starts with Problem Set 6 (1), then Homework 3 (2), Quiz 2 (3), Homework 4 (4)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from argus.capabilities import derive
from argus.clock import local_datetime, to_local
from argus.demo import DEMO_CONFIG, demo_secrets
from argus.planning import replan
from argus.store.db import open_db
from tests.e2e.conftest import LiveServer

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


def cards(page: Page) -> list[str]:
    titles = page.locator("#queue ol .q-card__title a").all_inner_texts()
    return [t.split(",")[0].strip() for t in titles]


def press(page: Page, name: str, js: bool) -> None:
    """Keyboard with JS (focus, Enter); a plain click without (the form submits and redirects)."""
    button = page.get_by_role("button", name=name, exact=True)
    if js:
        button.focus()
        page.keyboard.press("Enter")
    else:
        button.click()


def test_reorder_with_the_keyboard(
    qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/today")
    assert cards(qpage)[:2] == ["Problem Set 6", "Homework 3"]
    press(qpage, "Move Homework 3 up", js)
    expect(qpage.locator("#queue ol .q-card").first).to_contain_text("Homework 3")
    assert cards(qpage)[:2] == ["Homework 3", "Problem Set 6"]
    expect(qpage.locator("#q-item-2 .q-tag")).to_have_text("For tonight")
    if js:
        expect(qpage.get_by_text("Moved Homework 3 to 1 of 8")).to_be_visible()
        # At the top, Move up is disabled: focus moves to Move down, so Tab and Enter go on.
        expect(qpage.get_by_role("button", name="Move Homework 3 down", exact=True)).to_be_focused()
        qpage.keyboard.press("Enter")
        expect(qpage.locator("#queue ol .q-card").nth(1)).to_contain_text("Homework 3")
        expect(qpage.get_by_role("button", name="Move Homework 3 down", exact=True)).to_be_focused()
    else:
        assert qpage.url.endswith("/today#q-item-2")


def test_pin(qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/today")
    press(qpage, "Pin to top Quiz 4", js)
    expect(qpage.locator("#queue ol .q-card").first).to_contain_text("Quiz 4")
    expect(qpage.locator("#q-item-12")).to_contain_text("Pinned")
    qpage.reload()
    assert cards(qpage)[0] == "Quiz 4"
    press(qpage, "Unpin Quiz 4", js)
    expect(qpage.locator("#queue ol .q-card").first).to_contain_text("Problem Set 6")


def test_not_tonight(qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/today")
    press(qpage, "Not tonight Problem Set 6", js)
    skipped = qpage.get_by_role("region", name=re.compile("^Not tonight"))
    expect(skipped).to_contain_text("Problem Set 6")
    qpage.reload()
    assert "Problem Set 6" not in cards(qpage)
    press(qpage, "Back to tonight Problem Set 6", js)
    expect(qpage.locator("#queue ol .q-card").first).to_contain_text("Problem Set 6")


def test_time_left(qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/today")
    qpage.get_by_label("Hours left on Homework 3").first.fill("1")
    qpage.get_by_label("Hours left on Homework 3").first.press("Enter")
    expect(qpage.locator("#q-item-2")).to_contain_text("~1 h left")
    if js:
        expect(qpage.get_by_text("About 1 h left on Homework 3")).to_be_visible()


def test_mark_done(qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/today")
    press(qpage, "Mark done Quiz 2", js)
    expect(qpage.locator("#q-item-3")).to_have_count(0)
    if js:
        undo = qpage.get_by_role("button", name="Undo marking Quiz 2 done")
        expect(undo).to_be_focused()
        qpage.keyboard.press("Enter")
        expect(qpage.locator("#q-item-3")).to_have_count(1)


def test_the_confirm_line(qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    monday = to_local(s.clock.now()).date()
    s.clock.set(local_datetime(monday, 18, 50))
    conn = open_db(s.db, s.clock)
    tracking = derive(DEMO_CONFIG, demo_secrets()).tracking_sources
    replan(conn, DEMO_CONFIG, s.clock, tracking_sources=tracking)  # the worker stores the split
    conn.close()
    s.clock.set(local_datetime(monday + timedelta(days=1), 8))
    qpage.goto(s.url + "/today")
    line = qpage.locator("#confirm")
    expect(line).to_contain_text("Last night: did you work on Problem Set 6?")
    line.get_by_role("button", name="None").click()
    expect(qpage.locator("#confirm")).to_have_count(0)
    rows = (
        sqlite3.connect(s.db)
        .execute("SELECT source FROM time_logs WHERE item_id = 6 AND source = 'checkin'")
        .fetchall()
    )
    assert rows == [("checkin",)]


def test_the_timer_from_the_hero(
    qpage: Page, js: bool, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    qpage.goto(s.url + "/today")
    qpage.locator("#hero").get_by_role("button", name="Stop timer").click()
    expect(qpage.locator("#hero")).to_contain_text("Next up")
    expect(qpage.locator("#hero")).to_contain_text("Problem Set 6")
    qpage.locator("#hero").get_by_role("button", name=re.compile("^Start")).click()
    expect(qpage.locator("#hero")).to_contain_text("Working on")
    expect(qpage.locator("#hero")).to_contain_text("Problem Set 6")


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


def test_on_a_phone_focus_stays_in_the_rows_menu_after_a_move(
    browser: Browser, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    page = ctx.new_page()
    page.goto(s.url + "/today")
    card = page.locator("#q-item-4")  # Homework 4 is 4th
    card.locator("summary").focus()
    page.keyboard.press("Enter")
    up = card.get_by_role("button", name="Move Homework 4 up", exact=True)
    up.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#queue ol .q-card").nth(2)).to_contain_text("Homework 4")
    expect(page.locator("#q-item-4 .q-card__menu")).to_have_attribute("open", "")
    expect(
        page.locator("#q-item-4").get_by_role("button", name="Move Homework 4 up", exact=True)
    ).to_be_focused()
    ctx.close()
