"""WCAG 2.2 AA: zero axe violations on every page, both themes, every viewport it ships at."""

from collections.abc import Callable

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

from tests.e2e.conftest import today_state
from tests.e2e.pages import TODAY_STATES, VIEWPORTS, case_id, cases, set_up_cases

pytestmark = [pytest.mark.e2e, pytest.mark.a11y]

AXE_OPTIONS = {
    "runOnly": {
        "type": "tag",
        "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"],
    }
}
CASES = cases()
SET_UP = set_up_cases()


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


@pytest.mark.parametrize(("name", "viewport", "profile"), SET_UP, ids=[case_id(*c) for c in SET_UP])
def test_set_up_pages_have_no_violations(
    page: Page,
    set_up_url: Callable[[str, str], str],
    theme: str,
    name: str,
    viewport: str,
    profile: str,
) -> None:
    """Pages that need data the demo doesn't seed (pages.SET_UP_PAGES)."""
    w, h = VIEWPORTS[viewport]
    page.set_viewport_size({"width": w, "height": h})
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(set_up_url(name, profile))
    page.wait_for_load_state("networkidle")
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
        page.locator(".item-row__menu summary").first.click()
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, (
        f"{state} ({theme}, {viewport}):\n{results.generate_report()}"
    )


def test_file_adder_states_have_no_violations(  # type: ignore[no-untyped-def]
    page: Page, fresh_server, theme: str
) -> None:
    """The file adder's filled form, its error message, and the sent page (D074)."""
    s = fresh_server()
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(s.url + "/files/new?course=CMSC351")
    page.locator("input[name=file]").set_input_files(
        files=[{"name": "a.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.7\n"}]
    )
    page.get_by_label("What it is").fill("Slides")
    page.get_by_label("Slides", exact=True).check()
    page.locator("input[name=by_date]").fill("2026-10-01")
    page.get_by_role("button", name="Upload").click()
    page.locator("#file-error").wait_for()
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, results.generate_report()
    page.locator("input[name=by_date]").fill("2026-10-22")
    page.get_by_role("button", name="Upload").click()
    page.get_by_role("heading", name="Sent to the planner").wait_for()
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, results.generate_report()
