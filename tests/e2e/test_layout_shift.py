"""No layout shift on HTMX swaps (CLAUDE.md UI budgets, D064).

Every `layout-shift` entry is recorded, with `hadRecentInput`. Shifts the student didn't cause
(no recent input) must total under 0.001 around each same-size swap; a move may change only the
#queue box, never what sits around it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import LiveServer

pytestmark = pytest.mark.e2e

RECORD = """
window.__shifts = [];
new PerformanceObserver((list) => {
  for (const e of list.getEntries()) window.__shifts.push({ value: e.value, input: e.hadRecentInput });
}).observe({ type: "layout-shift", buffered: true });
"""


def unprompted(page: Page) -> float:
    shifts: list[dict[str, Any]] = page.evaluate("window.__shifts")
    return sum(s["value"] for s in shifts if not s["input"])


def reset(page: Page) -> None:
    page.evaluate("window.__shifts = []")


def enter(page: Page, name: str) -> None:
    page.get_by_role("button", name=name, exact=True).focus()
    page.keyboard.press("Enter")


def boxes(page: Page, *selectors: str) -> list[Any]:
    return [page.locator(s).bounding_box() for s in selectors]


@pytest.fixture
def today(page: Page, fresh_server: Callable[..., LiveServer]) -> Page:
    s = fresh_server()
    page.add_init_script(RECORD)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(s.url + "/today")
    page.wait_for_load_state("networkidle")
    reset(page)
    return page


def test_same_size_swaps_do_not_shift_the_page(today: Page) -> None:
    enter(today, "Pin to top Quiz 4")
    expect(today.locator("#queue ol .q-card").first).to_contain_text("Quiz 4")
    today.wait_for_timeout(200)
    assert unprompted(today) < 0.001
    reset(today)
    enter(today, "Mark done Quiz 2")
    expect(today.locator("#q-item-3")).to_have_count(0)
    today.wait_for_timeout(200)
    assert unprompted(today) < 0.001
    reset(today)
    today.locator("#hero-timer").focus()
    today.keyboard.press("Enter")  # stop the essay's timer: the hero becomes the next task
    expect(today.locator("#hero")).to_contain_text("Next up")
    today.wait_for_timeout(200)
    assert unprompted(today) < 0.001


def test_a_move_changes_only_the_queue(today: Page) -> None:
    around = ("#hero", ".today__aside", "#queue")
    before = boxes(today, *around)
    enter(today, "Move Homework 3 up")
    expect(today.locator("#queue ol .q-card").first).to_contain_text("Homework 3")
    today.wait_for_timeout(200)
    assert boxes(today, *around) == before  # same cards, same sizes: nothing around it moved
    assert unprompted(today) < 0.001


def test_the_recorder_sees_a_shift_nobody_asked_for(today: Page) -> None:
    today.evaluate(
        "document.querySelector('#main').prepend(Object.assign(document.createElement('div'),"
        " {style: 'height: 240px'}))"
    )
    today.wait_for_timeout(200)
    assert unprompted(today) > 0.01
