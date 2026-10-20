"""Course and Search views, with and without a Canvas token (D025, D055).

Three setups: the full demo, the demo without the token, and "token removed": data synced
with the token, served without it. Stored data from a source that is no longer set up must
not show (D055).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner

SETUPS = {
    "token": (DemoProfile.FULL, DemoProfile.FULL),
    "no-token": (DemoProfile.NO_CANVAS_TOKEN, DemoProfile.NO_CANVAS_TOKEN),
    "token-removed": (DemoProfile.FULL, DemoProfile.NO_CANVAS_TOKEN),  # (seeded, served)
}


def client_for(tmp_path: Path, clock: FakeClock, setup: str) -> TestClient:
    seeded, served = SETUPS[setup]
    db = tmp_path / "c.db"
    conn = open_db(db, clock)
    seed_demo(conn, clock.now(), seeded)
    conn.close()
    return TestClient(
        create_app(
            DEMO_CONFIG,
            demo_secrets(served),
            clock,
            db_path=db,
            runner_factory=lambda c, f: OfflineRunner(),
        )
    )


@pytest.fixture(params=list(SETUPS))
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock, mode: str) -> Iterator[TestClient]:
    with client_for(tmp_path, clock, mode) as c:
        yield c


def test_course_view(client: TestClient, mode: str) -> None:
    r = client.get("/courses/cmsc351")
    assert r.status_code == 200
    html = r.text
    assert "Homework 3" in html and "Midterm 1" in html  # items always
    assert 'Tue Thu, <span class="tnum">9:30 am to 10:45 am</span>, IRB 0324' in html  # meetings
    assert "HW3 problem 2 clarification" in html  # Piazza posts: Piazza is set up
    assert "https://www.gradescope.com/courses/2000001" in html
    if mode == "token":
        assert "91.5% (A-)" in html
        assert "Week 7: Dynamic programming" in html
        assert "1 item, locked" in html
        assert "Midterm 1 is in class on Wednesday, Oct 28" in html  # Canvas announcement
    else:
        # Hidden cleanly: no sections, no empty boxes, no hints about modules or grades,
        # and nothing stored from the ELMS API shows once the token is gone.
        assert "Current grade" not in html
        assert "Modules" not in html
        assert "Week 7" not in html
        assert "91.5%" not in html
        assert "Midterm 1 is in class on Wednesday, Oct 28" not in html
        for feature in ("modules", "grade", "canvas_announcements"):
            assert f'data-requires="{feature}"' not in html


def test_unknown_course_is_404(client: TestClient) -> None:
    assert client.get("/courses/HIST289").status_code == 404


def test_health_reflects_token(client: TestClient, mode: str) -> None:
    html = client.get("/health").text
    assert "ELMS modules" in html
    if mode != "token":
        assert "Uses ARGUS_CANVAS_TOKEN" in html


def test_search(client: TestClient, mode: str) -> None:
    html = client.get("/search?q=master").text
    # Only module titles and an ELMS announcement mention it: nothing to find without the token.
    assert ("<mark>master</mark>" in html) is (mode == "token")
    week2 = "Week 2: Recurrences and the master theorem"  # a module hit
    assert (week2 in html) is (mode == "token")
    assert "#module-" not in html or mode == "token"
    # Posts: Piazza always; Canvas announcements only with the token.
    assert "HW3 problem 2 clarification" in client.get("/search?q=connected").text
    room = client.get("/search?q=room").text
    assert ("Lab 3 room change" in room) is (mode == "token")
    if mode == "token":
        assert "Lab 3 room change" not in client.get("/search?q=room&course=CMSC351").text


def test_search_names_only_what_it_covers(client: TestClient, mode: str) -> None:
    html = client.get("/search").text
    assert "Search everything Argus knows" in html
    assert "Piazza posts" in html
    assert ("module titles" in html) is (mode == "token")
    assert ("ELMS announcements" in html) is (mode == "token")
    assert "module files" not in html  # course files are not searchable


def test_search_states(client: TestClient, mode: str) -> None:
    assert "No matches for" in client.get("/search?q=zzzqqq").text
    assert client.get("/search?q=" + "x" * 201).status_code == 422
    assert "1 result for connected" in client.get("/search?q=connected").text
    assert re.search(r"[2-9] results for lab", client.get("/search?q=lab").text)


def test_item_headers_link_to_course_pages(client: TestClient) -> None:
    # Rows show the code as text (D065); the item header's chip links to the course.
    assert re.search(
        r'class="course-chip" data-accent="\w+" href="/courses/CMSC351"',
        client.get("/items/2").text,
    )


def test_palette_lists_courses(client: TestClient) -> None:
    labels = {i["label"] for i in client.get("/palette.json").json()["items"]}
    assert {"Open CMSC351", "Open MATH240", "Open ENGL393"} <= labels
