"""No layout shift on HTMX swaps (CLAUDE.md UI budgets, D064).

Every `layout-shift` entry is recorded, with `hadRecentInput`. Shifts the student didn't cause
(no recent input) must total under 0.001 around each same-size swap: a row swaps in place and
nothing around it moves."""

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
    hw3 = today.locator("#item-2")
    hw3.hover()
    enter(today, "Start Homework 3")
    expect(hw3).to_have_attribute("data-running", "true")
    today.wait_for_timeout(200)
    assert unprompted(today) < 0.001
    reset(today)
    hw3.hover()
    enter(today, "Mark done Homework 3")
    expect(today.locator("#item-2")).to_have_attribute("data-done", "true")
    today.wait_for_timeout(200)
    assert unprompted(today) < 0.001


def test_a_row_swap_changes_only_that_row(today: Page) -> None:
    around = ("#item-6", ".page-head", "#item-3")
    before = boxes(today, *around)
    today.locator("#item-2").hover()
    enter(today, "Start Homework 3")
    expect(today.locator("#item-2")).to_have_attribute("data-running", "true")
    today.wait_for_timeout(200)
    assert boxes(today, *around) == before
    assert unprompted(today) < 0.001


def test_the_recorder_sees_a_shift_nobody_asked_for(today: Page) -> None:
    today.evaluate(
        "document.querySelector('#main').prepend(Object.assign(document.createElement('div'),"
        " {style: 'height: 240px'}))"
    )
    today.wait_for_timeout(200)
    assert unprompted(today) > 0.01
