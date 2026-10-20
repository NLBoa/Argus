"""WCAG 2.2 AA: zero axe violations on every page, both themes, every viewport it ships at."""

from collections.abc import Callable

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

from tests.e2e.conftest import today_state
from tests.e2e.pages import TODAY_STATES, VIEWPORTS, case_id, cases

pytestmark = [pytest.mark.e2e, pytest.mark.a11y]

AXE_OPTIONS = {
    "runOnly": {
        "type": "tag",
        "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"],
    }
}
CASES = cases()


@pytest.mark.parametrize(
    ("name", "path", "viewport", "profile"), CASES, ids=[case_id(n, v, p) for n, _, v, p in CASES]
)
def test_no_axe_violations(
    page: Page,
    server_url: Callable[[str], str],
    theme: str,
    name: str,
    path: str,
    viewport: str,
    profile: str,
) -> None:
    w, h = VIEWPORTS[viewport]
    page.set_viewport_size({"width": w, "height": h})
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(server_url(profile) + path)
    page.wait_for_load_state("load" if name == "kiosk" else "networkidle")
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, (
        f"{name} ({theme}, {viewport}, {profile}):\n{results.generate_report()}"
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


@pytest.mark.parametrize("viewport", ["mobile", "desktop"])
@pytest.mark.parametrize("state", TODAY_STATES)
def test_today_states_have_no_violations(
    page: Page, fresh_server, theme: str, state: str, viewport: str
) -> None:  # type: ignore[no-untyped-def]
    s = fresh_server()
    today_state(s, state)
    w, h = VIEWPORTS[viewport]
    page.set_viewport_size({"width": w, "height": h})
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(s.url + "/today")
    page.wait_for_load_state("networkidle")
    if viewport == "mobile":  # a row's "..." menu, open
        page.locator("#queue ol .q-card__menu summary").first.click()
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, (
        f"{state} ({theme}, {viewport}):\n{results.generate_report()}"
    )
