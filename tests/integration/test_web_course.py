"""Course and Search views, with and without a Canvas token (the Phase 3 gate)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.clock import FakeClock
from argus.config import Secrets
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner

NO_TOKEN = Secrets(**{**DEMO_SECRETS.model_dump(), "canvas_token": None})


def client_for(tmp_path: Path, clock: FakeClock, secrets: Secrets) -> TestClient:
    db = tmp_path / "c.db"
    conn = open_db(db, clock)
    seed_demo(conn, clock.now())
    conn.close()
    return TestClient(
        create_app(
            DEMO_CONFIG, secrets, clock, db_path=db, runner_factory=lambda c, f: OfflineRunner()
        )
    )


@pytest.fixture(params=["token", "no-token"])
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock, mode: str) -> Iterator[TestClient]:
    with client_for(tmp_path, clock, DEMO_SECRETS if mode == "token" else NO_TOKEN) as c:
        yield c


def test_course_view(client: TestClient, mode: str) -> None:
    r = client.get("/courses/cmsc351")
    assert r.status_code == 200
    html = r.text
    assert "Homework 3" in html and "Midterm 1" in html  # items always
    assert 'Tue Thu, <span class="tnum">9:30 am to 10:45 am</span>, IRB 0324' in html  # meetings
    assert "Midterm 1 is in class on Wednesday, Oct 28" in html  # staff posts always
    assert "https://www.gradescope.com/courses/2000001" in html
    if mode == "token":
        assert "91.5% (A-)" in html
        assert "Week 7: Dynamic programming" in html
        assert "1 item, locked" in html
    else:
        # Hidden cleanly: no sections, no empty boxes, no hints about modules.
        assert "Current grade" not in html
        assert "Modules" not in html
        assert "Week 7" not in html


def test_unknown_course_is_404(client: TestClient) -> None:
    assert client.get("/courses/HIST289").status_code == 404


def test_health_reflects_token(client: TestClient, mode: str) -> None:
    html = client.get("/health").text
    assert "ELMS modules" in html
    if mode == "no-token":
        assert "Uses ARGUS_CANVAS_TOKEN" in html


def test_search(client: TestClient, mode: str) -> None:
    html = client.get("/search?q=master").text
    assert "Announcements and posts" in html
    assert "<mark>master</mark>" in html
    if mode == "token":
        assert "Week 2: Recurrences and the master theorem" in html  # module hit
    # Course filter narrows results.
    filtered = client.get("/search?q=room&course=CMSC351").text
    assert "Lab 3 room change" not in filtered
    assert "Lab 3 room change" in client.get("/search?q=room").text


def test_search_states(client: TestClient) -> None:
    assert "Search everything Argus knows" in client.get("/search").text
    assert "No matches for" in client.get("/search?q=zzzqqq").text
    assert client.get("/search?q=" + "x" * 201).status_code == 422
    assert "1 result for room" in client.get("/search?q=room").text
    assert "2 results for lab" in client.get("/search?q=lab").text


def test_chips_link_to_course_pages(client: TestClient) -> None:
    assert 'href="/courses/CMSC351"' in client.get("/today").text


def test_palette_lists_courses(client: TestClient) -> None:
    labels = {i["label"] for i in client.get("/palette.json").json()["items"]}
    assert {"Open CMSC351", "Open MATH240", "Open ENGL393"} <= labels
