"""Settings: when you study (D057). Every action works with and without JS (D016 origin check)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.store import repo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner

ORIGIN = {"origin": "http://testserver", "referer": "http://testserver/settings"}
HX = {**ORIGIN, "hx-request": "true"}


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    db = tmp_path / "s.db"
    conn = open_db(db, clock)
    seed_demo(conn, clock.now(), DemoProfile.FULL)  # seeds Mon-Thu 7-11 pm and Sun 1-6 pm
    conn.close()
    app = create_app(
        DEMO_CONFIG, demo_secrets(), clock, db_path=db, runner_factory=lambda c, f: OfflineRunner()
    )
    with TestClient(app) as c:
        yield c


def windows(client: TestClient) -> list[str]:
    conn = open_db(client.app.state.argus.db_path, client.app.state.argus.clock)  # type: ignore[attr-defined]
    try:
        return sorted(
            f"{sorted(w.days)} {w.start:%H:%M}-{w.end:%H:%M}" for w in repo.work_windows(conn)
        )
    finally:
        conn.close()


EVERY_DAY = {"count": "1", "every_0": "on", "start_0": "19:00", "end_0": "23:00", "action": "save"}


def test_page_shows_the_saved_windows_in_words(client: TestClient) -> None:
    html = client.get("/settings").text
    assert "When you study" in html
    assert "Mon-Thu, 7 pm to 11 pm" in html and "Sun, 1 pm to 6 pm" in html
    assert "19 h</strong> in the next 7 days" in html  # 16 + 5 - 2 h of the Tue/Thu shift
    assert 'name="start_0" value="19:00"' in html and "Time 2" in html


def test_save_every_day_without_js(client: TestClient) -> None:
    r = client.post("/settings/windows", data=EVERY_DAY, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert windows(client) == ["[0, 1, 2, 3, 4, 5, 6] 19:00-23:00"]
    html = client.get("/settings").text
    assert "Every day, 7 pm to 11 pm" in html
    assert "Your work windows: Every day, 7 pm to 11 pm" in client.get("/workload").text


def test_save_with_htmx_returns_the_form_and_a_toast(client: TestClient) -> None:
    r = client.post("/settings/windows", data=EVERY_DAY, headers=HX)
    assert r.status_code == 200 and 'id="windows-form"' in r.text
    assert json.loads(r.headers["HX-Trigger"])["toast"]["message"] == "Saved your work windows"


def test_add_and_remove_change_the_form_until_saved(client: TestClient) -> None:
    before = windows(client)
    added = client.post(
        "/settings/windows",
        data={
            "count": "2",
            "days_0": ["0", "1", "2", "3"],
            "start_0": "19:00",
            "end_0": "23:00",
            "days_1": "6",
            "start_1": "13:00",
            "end_1": "18:00",
            "action": "add",
        },
        headers=ORIGIN,
    )
    assert added.status_code == 200 and "Time 3" in added.text and "Not saved yet" in added.text
    removed = client.post(
        "/settings/windows",
        data={
            "count": "2",
            "every_0": "on",
            "start_0": "19:00",
            "end_0": "23:00",
            "action": "remove-1",
        },
        headers=ORIGIN,
    )
    assert "Time 2" not in removed.text
    assert windows(client) == before  # nothing saved yet


def test_mistakes_are_shown_on_their_row_and_nothing_is_saved(client: TestClient) -> None:
    before = windows(client)
    bad = {"count": "1", "start_0": "19:00", "end_0": "23:00", "action": "save"}  # no days
    plain = client.post("/settings/windows", data=bad, headers=ORIGIN)
    assert plain.status_code == 422 and "Pick at least one day." in plain.text
    swapped = client.post("/settings/windows", data=bad, headers=HX)
    assert (
        swapped.status_code == 200 and "Pick at least one day." in swapped.text
    )  # HTMX swaps 200s
    assert 'aria-describedby="window-error-0"' in swapped.text
    assert windows(client) == before


def test_removing_every_window_turns_them_off(client: TestClient) -> None:
    r = client.post("/settings/windows", data={"count": "0", "action": "save"}, headers=HX)
    assert json.loads(r.headers["HX-Trigger"])["toast"]["message"] == "Removed your work windows"
    assert windows(client) == []  # and config.yaml does not seed them back
    assert "Tell Argus when you study." in client.get("/today").text
    assert "needs work windows: set them in Settings" in client.get("/health").text


def test_cross_origin_posts_are_refused(client: TestClient) -> None:
    r = client.post("/settings/windows", data=EVERY_DAY, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_settings_is_in_the_nav_and_the_palette_page_list(client: TestClient) -> None:
    html = client.get("/today").text
    assert 'href="/settings"' in html
    assert "Tell Argus when you study." not in html  # the demo has windows


def test_without_js_save_always_lands_on_settings(client: TestClient) -> None:
    after_remove = {**ORIGIN, "referer": "http://testserver/settings/windows"}
    r = client.post(
        "/settings/windows", data=EVERY_DAY, headers=after_remove, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    reload = client.get("/settings/windows", follow_redirects=False)
    assert reload.status_code == 303 and reload.headers["location"] == "/settings"
