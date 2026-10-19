"""WCAG 2.2 AA: zero axe violations on every page, both themes, every viewport it ships at."""

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

from tests.e2e.pages import PAGES, VIEWPORTS

pytestmark = [pytest.mark.e2e, pytest.mark.a11y]

AXE_OPTIONS = {
    "runOnly": {
        "type": "tag",
        "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"],
    }
}
CASES = [(name, path, vp) for name, path, vps in PAGES for vp in vps]


@pytest.mark.parametrize(("name", "path", "viewport"), CASES, ids=[f"{n}-{v}" for n, _, v in CASES])
def test_no_axe_violations(
    page: Page, base_url: str, theme: str, name: str, path: str, viewport: str
) -> None:
    w, h = VIEWPORTS[viewport]
    page.set_viewport_size({"width": w, "height": h})
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(base_url + path)
    page.wait_for_load_state("load" if name == "kiosk" else "networkidle")
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, (
        f"{name} ({theme}, {viewport}):\n{results.generate_report()}"
    )


def test_palette_open_has_no_violations(page: Page, base_url: str, theme: str) -> None:
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(base_url + "/today")
    page.keyboard.press("Control+k")
    page.get_by_role("combobox").wait_for()
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, results.generate_report()


@pytest.mark.parametrize("path", ["/today", "/upcoming", "/health"])
def test_empty_states_have_no_violations(page: Page, fresh_server, theme: str, path: str) -> None:  # type: ignore[no-untyped-def]
    s = fresh_server(seed=False)
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(s.url + path)
    page.wait_for_load_state("networkidle")
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, results.generate_report()
