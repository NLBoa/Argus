from collections.abc import Callable

import pytest
from playwright.sync_api import Page

from tests.e2e.pages import VIEWPORTS, case_id, cases
from tests.visual.snapshot import assert_snapshot

pytestmark = pytest.mark.visual
CASES = cases()


@pytest.mark.parametrize(
    ("name", "path", "viewport", "profile"), CASES, ids=[case_id(n, v, p) for n, _, v, p in CASES]
)
def test_page_snapshot(
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
    page.emulate_media(color_scheme=theme, reduced_motion="reduce")  # type: ignore[arg-type]
    page.goto(server_url(profile) + path)
    page.wait_for_load_state("load" if name == "kiosk" else "networkidle")
    page.evaluate("document.fonts.ready")
    # The running timer ticks in real time; pin it so snapshots are stable.
    page.add_style_tag(content=".timer-bar__elapsed { visibility: hidden; }")
    assert_snapshot(page, f"{case_id(name, viewport, profile)}-{theme}")
