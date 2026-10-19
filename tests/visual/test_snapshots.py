import pytest
from playwright.sync_api import Page

from tests.e2e.pages import PAGES
from tests.visual.snapshot import assert_snapshot

pytestmark = pytest.mark.visual

VIEWPORTS = {"mobile": (390, 844), "desktop": (1280, 800)}


@pytest.mark.parametrize("viewport", list(VIEWPORTS))
@pytest.mark.parametrize(("name", "path"), PAGES, ids=[p[0] for p in PAGES])
def test_page_snapshot(
    page: Page, base_url: str, theme: str, viewport: str, name: str, path: str
) -> None:
    w, h = VIEWPORTS[viewport]
    page.set_viewport_size({"width": w, "height": h})
    page.emulate_media(color_scheme=theme, reduced_motion="reduce")  # type: ignore[arg-type]
    page.goto(base_url + path)
    page.wait_for_load_state("networkidle")
    assert_snapshot(page, f"{name}-{viewport}-{theme}")
