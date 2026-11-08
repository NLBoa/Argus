"""The goals page in a browser (D079): reachable from More, keyboard path through its toggles."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_goals_is_in_more_and_g_o_goes_there(page: Page, base_url: str) -> None:
    page.goto(base_url + "/today")
    page.keyboard.press("g")
    page.keyboard.press("o")
    expect(page).to_have_url(base_url + "/goals")
    expect(page.get_by_role("heading", level=1, name="Goals")).to_be_visible()


def test_keyboard_reaches_the_trend_table_and_inactive_list(page: Page, base_url: str) -> None:
    page.goto(base_url + "/goals")
    toggle = page.locator(".goal-trend summary")
    toggle.focus()
    expect(toggle).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator(".goal-trend table")).to_be_visible()
    region = page.get_by_role("region", name="Test scores by topic")
    region.focus()
    expect(region).to_be_focused()


def test_meters_and_bars_have_text_alternatives(page: Page, base_url: str) -> None:
    page.goto(base_url + "/goals")
    meter = page.locator(".goal-meter").first
    expect(meter).to_have_attribute("role", "img")
    assert "minutes done" in (meter.get_attribute("aria-label") or "")
    bars = page.locator(".goal-trend__bars").first
    assert "minutes done by week" in (bars.get_attribute("aria-label") or "")
