"""Setting when you study, with JS and without (D057)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import LiveServer

pytestmark = pytest.mark.e2e


def set_every_evening(page: Page) -> None:
    first = page.get_by_role("group", name="Time 1")
    first.get_by_label("Every day").check()
    first.get_by_label("From").fill("19:00")
    first.get_by_label("To").fill("23:00")
    page.get_by_role("group", name="Time 2").get_by_role("button", name="Remove time 2").click()


def test_every_day_with_js(page: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    page.goto(s.url + "/settings")
    first = page.get_by_role("group", name="Time 1")
    expect(first.get_by_role("group", name="Days for time 1")).to_be_visible()
    first.get_by_label("Every day").check()
    expect(
        first.get_by_role("group", name="Days for time 1")
    ).to_be_hidden()  # no need to pick days
    set_every_evening(page)
    expect(page.get_by_text("Not saved yet")).to_be_visible()
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("Saved your work windows")).to_be_visible()
    expect(page.get_by_text("Every day, 7 pm to 11 pm")).to_be_visible()
    page.goto(s.url + "/workload")
    expect(page.get_by_text("Your work windows: Every day, 7 pm to 11 pm.")).to_be_visible()


def test_every_day_without_js(browser: Browser, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(s.url + "/settings")
    set_every_evening(page)
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page).to_have_url(s.url + "/settings")
    expect(page.get_by_text("Every day, 7 pm to 11 pm")).to_be_visible()
    context.close()


def test_a_mistake_is_explained_next_to_it(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page.goto(s.url + "/settings")
    page.get_by_role("button", name="Add another time").click()
    third = page.get_by_role("group", name="Time 3")
    third.get_by_label("From").fill("08:00")
    third.get_by_label("To").fill("09:00")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(
        page.get_by_role("group", name="Time 3").get_by_text("Pick at least one day.")
    ).to_be_visible()
