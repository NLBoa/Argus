import pytest
from playwright.sync_api import Page

from tests.e2e.pages import PAGES, VIEWPORTS
from tests.visual.snapshot import assert_snapshot

pytestmark = pytest.mark.visual
CASES = [(name, path, vp) for name, path, vps in PAGES for vp in vps]


@pytest.mark.parametrize(("name", "path", "viewport"), CASES, ids=[f"{n}-{v}" for n, _, v in CASES])
def test_page_snapshot(
    page: Page, base_url: str, theme: str, name: str, path: str, viewport: str
) -> None:
    w, h = VIEWPORTS[viewport]
    page.set_viewport_size({"width": w, "height": h})
    page.emulate_media(color_scheme=theme, reduced_motion="reduce")  # type: ignore[arg-type]
    page.goto(base_url + path)
    page.wait_for_load_state("load" if name == "kiosk" else "networkidle")
    page.evaluate("document.fonts.ready")
    # The running timer ticks in real time; pin it so snapshots are stable.
    page.add_style_tag(content=".timer-bar__elapsed { visibility: hidden; }")
    assert_snapshot(page, f"{name}-{viewport}-{theme}")
