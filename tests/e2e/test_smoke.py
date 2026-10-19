import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_home_loads_with_skip_link(page: Page, base_url: str) -> None:
    page.goto(base_url + "/")
    expect(page.get_by_role("heading", level=1)).to_have_text("Argus")
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
