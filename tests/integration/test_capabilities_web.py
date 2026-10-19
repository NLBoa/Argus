"""Every page, in every setup: nothing a missing source can't support is rendered (D055).

Token-dependent markup carries `data-requires="<feature>"`, so one check covers every page:
in a setup without the feature, no element may carry it. Pages come from tests/e2e/pages.py,
so new pages are covered automatically.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.capabilities import Feature, derive
from argus.clock import FakeClock
from argus.config import Secrets
from argus.demo import DEMO_CONFIG, DemoProfile, DemoTransport, demo_secrets, seed_demo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner
from tests.conftest import DEFAULT_NOW
from tests.e2e.pages import PAGES

NO_POSTS = demo_secrets(DemoProfile.NO_CANVAS_TOKEN).model_copy(
    update={"piazza_email": None, "piazza_password": None}
)
SETUPS: dict[str, tuple[DemoProfile, Secrets]] = {
    "full": (DemoProfile.FULL, demo_secrets(DemoProfile.FULL)),
    "full-ai": (DemoProfile.FULL, demo_secrets(DemoProfile.FULL)),  # with the demo's AI
    "no-token": (DemoProfile.NO_CANVAS_TOKEN, demo_secrets(DemoProfile.NO_CANVAS_TOKEN)),
    "token-removed": (DemoProfile.FULL, demo_secrets(DemoProfile.NO_CANVAS_TOKEN)),
    "no-posts": (DemoProfile.NO_CANVAS_TOKEN, NO_POSTS),
}
PATHS = sorted({path for _, path, viewports in PAGES if "kiosk" not in viewports} | {"/kiosk"})


@pytest.fixture(scope="module", params=list(SETUPS))
def setup(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, TestClient, Secrets]]:
    name = str(request.param)
    profile, secrets = SETUPS[name]
    clock = FakeClock(DEFAULT_NOW)
    db = Path(tmp_path_factory.mktemp(name)) / "caps.db"
    conn = open_db(db, clock)
    seed_demo(conn, clock.now(), profile)
    conn.close()
    app = create_app(
        DEMO_CONFIG,
        secrets,
        clock,
        db_path=db,
        runner_factory=lambda c, f: OfflineRunner(),
        ai_transport=DemoTransport() if name == "full-ai" else None,
    )
    with TestClient(app) as client:
        yield name, client, secrets


@pytest.mark.parametrize("path", PATHS)
def test_no_feature_markup_without_the_feature(
    setup: tuple[str, TestClient, Secrets], path: str
) -> None:
    name, client, secrets = setup
    caps = derive(DEMO_CONFIG, secrets, ai=name == "full-ai")
    r = client.get(path, follow_redirects=False)
    assert r.status_code in (200, 307, 404), (path, r.status_code)
    html = r.text
    for feature in Feature:
        if not caps.has(feature):
            assert f'data-requires="{feature.value}"' not in html, (name, path, feature)
    if not caps.has(Feature.MODULES):
        assert "#module-" not in html, (name, path)
    if not caps.has(Feature.STAFF_POSTS):
        assert 'href="/announcements"' not in html, (name, path)
        assert "OK 1, NO 1" not in html
    if not caps.has(Feature.CANVAS_ANNOUNCEMENTS):
        # Seeded ELMS announcements (stored before the token was removed) never show.
        for text in (
            "Lab 3 room change",
            "Midterm 1 is in class on Wednesday",
            "discussion_topics",
        ):
            assert text not in html, (name, path, text)
    if not caps.has(Feature.MODULES):
        for text in ("Week 7: Dynamic programming", "91.5%", "86.25%"):
            assert text not in html, (name, path, text)


def test_announcements_page_needs_a_post_source(setup: tuple[str, TestClient, Secrets]) -> None:
    name, client, _ = setup
    r = client.get("/announcements", follow_redirects=False)
    if name == "no-posts":
        assert r.status_code == 307 and r.headers["location"] == "/today"
        boot = re.search(r'id="argus-boot">(.*?)</script>', client.get("/today").text)
        assert boot and '"pages": ["/announcements"]' in boot.group(1)  # palette skips it
    else:
        assert r.status_code == 200


def test_did_you_submit_replaces_overdue_without_tracking(
    setup: tuple[str, TestClient, Secrets],
) -> None:
    """Lab report 2 (iCal-only without the token) was due 3h ago. With a source that reports
    submissions it is overdue; without one Argus asks instead of accusing (D056)."""
    name, client, _ = setup
    html = client.get("/today").text
    if name == "full":
        assert "Past due" in html and "Overdue" in html
        assert "Did you submit?" not in html
    elif name in ("no-token", "no-posts"):
        assert "Did you submit?" in html and "to confirm" in html
        assert ">Overdue<" not in html


def test_marked_sections_render_when_set_up(setup: tuple[str, TestClient, Secrets]) -> None:
    """Positive control: the markers the checks above look for really are on the pages."""
    name, client, _ = setup
    course = client.get("/courses/CMSC351").text
    if name.startswith("full"):
        for feature in ("modules", "grade", "staff_posts"):
            assert f'data-requires="{feature}"' in course
        assert 'data-requires="staff_posts"' in client.get("/today").text  # the nav entry
    item = client.get("/items/2").text
    assert ('data-requires="ai"' in item) is (name == "full-ai")  # Re-run AI estimate
    if name == "full":  # the ELMS strings checked above really are seeded
        assert "Lab 3 room change" in client.get("/courses/MATH240").text
        assert "Week 7: Dynamic programming" in course


def test_unconfirmed_items_offer_mark_done_not_start(
    setup: tuple[str, TestClient, Secrets],
) -> None:
    name, client, _ = setup
    if name not in ("no-token", "no-posts"):
        return
    today = client.get("/today").text
    row = today[today.index("Lab report 2") :]
    row = row[: row.index("</li>")]
    assert "Mark done" in row and 'value="start"' not in row
    # Older than the 7-day ask window: listed on the course page, labelled, not asked again.
    course = client.get("/courses/CMSC351").text
    hw2 = course[course.index("Homework 2") :]
    assert "Not marked done" in hw2[: hw2.index("</li>")]
    assert "Homework 2" not in today


def test_htmx_row_swaps_keep_the_unconfirmed_state(tmp_path: Path) -> None:
    """Review P0: a row re-rendered by an HTMX action must use the same tracking rule as the
    page, so reopening an untracked past-due item never turns it into "Overdue"."""
    clock = FakeClock(DEFAULT_NOW)
    db = tmp_path / "swap.db"
    conn = open_db(db, clock)
    seed_demo(conn, clock.now(), DemoProfile.NO_CANVAS_TOKEN)
    lab = conn.execute("SELECT id FROM items WHERE title = 'Lab report 2'").fetchone()[0]
    conn.close()
    app = create_app(
        DEMO_CONFIG,
        demo_secrets(DemoProfile.NO_CANVAS_TOKEN),
        clock,
        db_path=db,
        runner_factory=lambda c, f: OfflineRunner(),
    )
    hx = {"hx-request": "true", "origin": "http://testserver"}
    with TestClient(app) as client:
        done = client.post(f"/items/{lab}/done", data={"done": "1"}, headers=hx)
        assert done.status_code == 200 and 'data-done="true"' in done.text
        reopened = client.post(f"/items/{lab}/done", data={"done": "0"}, headers=hx)
        assert reopened.status_code == 200
        assert 'data-risk="unconfirmed"' in reopened.text
        assert "Overdue" not in reopened.text
        assert 'value="start"' not in reopened.text  # no Start button for it
