"""WCAG 2.2 AA: zero axe violations on every page in both themes."""

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

from tests.e2e.pages import PAGES

pytestmark = [pytest.mark.e2e, pytest.mark.a11y]

AXE_OPTIONS = {
    "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]}
}


@pytest.mark.parametrize(("name", "path"), PAGES, ids=[p[0] for p in PAGES])
def test_no_axe_violations(page: Page, base_url: str, theme: str, name: str, path: str) -> None:
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(base_url + path)
    page.wait_for_load_state("networkidle")
    results = Axe().run(page, options=AXE_OPTIONS)
    assert results.violations_count == 0, f"{name} ({theme}):\n{results.generate_report()}"
