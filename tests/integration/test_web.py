"""Every view in every state (data, empty, error, stale), the actions, and the security guards."""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.store import repo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import STATIC_DIR, get_conn

ORIGIN = {"Origin": "http://testserver"}
HX = {**ORIGIN, "HX-Request": "true"}


class CountingRunner:
    calls = 0

    async def run_cycle(self, *, only: Any = None, force: bool = False) -> None:
        CountingRunner.calls += 1


def build(tmp_path: Path, clock: FakeClock, seed: bool = True) -> TestClient:
    db = tmp_path / "web.db"
    conn = open_db(db, clock)
    if seed:
        seed_demo(conn, clock.now())
    conn.close()
    app = create_app(
        DEMO_CONFIG, DEMO_SECRETS, clock, db_path=db, runner_factory=lambda c, f: CountingRunner()
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    with build(tmp_path, clock) as c:
        yield c


@pytest.fixture
def empty(tmp_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    with build(tmp_path, clock, seed=False) as c:
        yield c


def item_id(client: TestClient, title: str) -> int:
    db = client.app.state.argus.db_path  # type: ignore[attr-defined]
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT id FROM items WHERE title = ?", (title,)).fetchone()
    return int(row[0])


# ---------------------------------------------------------------- views with data


def test_root_redirects_to_today(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/today"


def test_today_with_data(client: TestClient) -> None:
    r = client.get("/today")
    assert r.status_code == 200
    html = r.text
    assert "Monday, October 19" in html
    assert "2 due today, 1 tomorrow, 1 past due" in html
    # Four failures in a row: a banner, not a quiet note (D067)
    assert "Gradescope has failed 4 syncs in a row" in html
    assert "1 note edit to review" in html  # the status line
    assert "Essay draft: technical description" in html  # running timer
    assert 'aria-current="page"' in html
    assert "MATH240 lecture" in html  # day strip
    assert 'id="hero"' in html and "Working on" in html


def test_upcoming_with_filter(client: TestClient) -> None:
    html = client.get("/upcoming?course=CMSC351").text
    assert "Homework 4" in html
    assert "Problem Set 7" not in html
    assert client.get("/upcoming?course=NOPE").status_code == 200  # unknown filter = all
    assert client.get("/upcoming?days=0").status_code == 422


def test_health_with_data(client: TestClient) -> None:
    html = client.get("/health").text
    assert "Failing (4 in a row)" in html
    assert "Gradescope page format changed" in html
    assert "set ARGUS_GMAIL_TOKEN_PATH" in html  # unconfigured hint
    assert "Gradescope sync failing" in html  # recent reminder
    assert 'href="/health">Check health' not in html  # no self-link on Health


def test_styleguide_shows_every_component(client: TestClient) -> None:
    html = client.get("/styleguide").text
    for marker in [
        "course-chip",
        "risk-badge",
        "course-code",
        "status-line",
        "status-note",
        "wl__plot",
        "item-row",
        "day-rail",
        "q-card",
        'class="hero"',
        "confirm-line",
        "queue-done",
        'class="strip"',
        "workload",
        "finding-card",
        "diff__split",
        "practice-card",
        "readiness__bar",
        "empty-state",
        "skeleton",
        "banner--error",
        "toast",
        'data-mark="wordmark"',
        'data-mark="square"',
    ]:
        assert marker in html, marker


# ---------------------------------------------------------------- empty and error states


def test_empty_states(empty: TestClient) -> None:
    today = empty.get("/today").text
    assert "Nothing in the queue" in today
    assert "Nothing scheduled today." in today
    assert "Nothing left due today" in today
    assert "Nothing due in the next 14 days" in empty.get("/upcoming").text
    health = empty.get("/health").text
    assert "Waiting for first sync" in health
    assert "No reminders yet" in health


@pytest.mark.parametrize("path", ["/today", "/upcoming", "/health"])
def test_error_state_with_retry(tmp_path: Path, clock: FakeClock, path: str) -> None:
    with build(tmp_path, clock) as c:

        def broken() -> Iterator[sqlite3.Connection]:
            conn = sqlite3.connect(":memory:")  # no tables: every query fails
            yield conn

        c.app.dependency_overrides[get_conn] = broken  # type: ignore[attr-defined]
        r = c.get(path)
        assert r.status_code == 503
        assert "Couldn&#39;t load this page" in r.text or "Couldn't load this page" in r.text
        assert f'href="{path}"' in r.text and "Retry" in r.text


# ---------------------------------------------------------------- actions


def test_mark_done_htmx_and_plain_form(client: TestClient) -> None:
    pid = item_id(client, "Problem Set 6")
    r = client.post(f"/items/{pid}/done", data={"done": "1"}, headers=HX)
    assert r.status_code == 200
    assert 'data-done="true"' in r.text
    assert json.loads(r.headers["HX-Trigger"]) == {
        "toast": {"message": "Marked Problem Set 6 done", "kind": "ok"}
    }
    r = client.post(
        f"/items/{pid}/done",
        data={"done": "0"},
        headers={**ORIGIN, "Referer": "http://testserver/upcoming?course=MATH240"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/upcoming?course=MATH240"
    assert client.post("/items/99999/done", data={"done": "1"}, headers=HX).status_code == 404


def test_mark_done_stops_its_timer(client: TestClient) -> None:
    essay = item_id(client, "Essay draft: technical description")
    client.post(f"/items/{essay}/done", data={"done": "1"}, headers=HX)
    assert "Running timer" not in client.get("/today").text


def test_timer_start_stop(client: TestClient, clock: FakeClock) -> None:
    hw = item_id(client, "Homework 3")
    r = client.post(f"/items/{hw}/timer", data={"action": "start"}, headers=HX)
    assert 'data-running="true"' in r.text
    assert "Timer started" in r.headers["HX-Trigger"]
    clock.advance(timedelta(minutes=25))
    r = client.post(f"/items/{hw}/timer", data={"action": "stop"}, headers=HX)
    assert "Logged 25 min" in r.headers["HX-Trigger"]
    r = client.post("/timer/stop", headers=HX)
    assert r.status_code == 200
    assert (
        client.post("/items/99999/timer", data={"action": "start"}, headers=HX).status_code == 404
    )
    r = client.post("/timer/stop", headers={**ORIGIN}, follow_redirects=False)
    assert r.status_code == 303


def test_mute_rule(client: TestClient) -> None:
    r = client.post("/rules/new_item/mute", data={"muted": "1"}, headers=HX)
    assert "New items muted" in r.headers["HX-Trigger"]
    assert 'aria-label="New items muted"' in r.text
    assert client.post("/rules/bogus/mute", data={"muted": "1"}, headers=HX).status_code == 404
    r = client.post(
        "/rules/new_item/mute", data={"muted": "0"}, headers=ORIGIN, follow_redirects=False
    )
    assert r.headers["location"] == "/health"


def test_sync_now_runs_in_background(client: TestClient) -> None:
    CountingRunner.calls = 0
    r = client.post("/sync", headers=HX)
    assert r.status_code == 202
    assert "Sync started" in r.headers["HX-Trigger"]
    deadline = time.monotonic() + 2
    while CountingRunner.calls == 0 and time.monotonic() < deadline:
        client.get("/healthz")
    assert CountingRunner.calls == 1
    assert client.post("/sync", headers=ORIGIN, follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- security


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.example"}, {"Referer": "https://evil.example/page"}],
    ids=["no-origin", "foreign-origin", "foreign-referer"],
)
def test_cross_origin_posts_are_refused(client: TestClient, headers: dict[str, str]) -> None:
    pid = item_id(client, "Problem Set 6")
    r = client.post(f"/items/{pid}/done", data={"done": "1"}, headers=headers)
    assert r.status_code == 403
    conn = sqlite3.connect(client.app.state.argus.db_path)  # type: ignore[attr-defined]
    assert conn.execute("SELECT user_done_at FROM items WHERE id = ?", (pid,)).fetchone()[0] is None


def test_security_headers(client: TestClient) -> None:
    r = client.get("/today")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"


def test_no_external_resources(client: TestClient) -> None:
    for path in ["/today", "/upcoming", "/health", "/styleguide", "/kiosk"]:
        html = client.get(path).text
        srcs = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
        external = [
            s
            for s in srcs
            if not s.startswith(
                ("https://umd.instructure.com", "https://www.gradescope.com", "https://piazza.com")
            )
        ]
        assert external == [], f"{path} loads {external}"


# ---------------------------------------------------------------- kiosk, palette, pwa, theme


def test_kiosk_page_and_stream(client: TestClient) -> None:
    html = client.get("/kiosk").text
    assert 'data-theme="dark"' in html
    assert "<button" not in html and "<a " not in html
    assert "Next deadlines" in html and "Next" in html and "MATH240 lecture" in html
    with client.stream("GET", "/kiosk/stream?once=1") as r:
        body = "".join(r.iter_text())
    assert r.headers["content-type"].startswith("text/event-stream")
    event = next(line for line in body.splitlines() if line.startswith("data: "))
    payload = json.loads(event[6:])
    assert "Next deadlines" in payload["html"]
    assert payload["now"].startswith("2026-10-19T13:00:00")


def test_palette_items(client: TestClient) -> None:
    items = client.get("/palette.json").json()["items"]
    titles = {i["label"] for i in items}
    assert {"Homework 3", "Problem Set 7"} <= titles
    assert all(i["href"].startswith("/items/") for i in items if i["id"].startswith("item-"))


def test_manifest_service_worker_offline(client: TestClient) -> None:
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["start_url"] == "/today"
    for icon in m.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js")
    assert "argus-" in sw.text and "/offline" in sw.text
    assert client.get("/offline").status_code == 200


def test_theme_cookie_renders_server_side(client: TestClient) -> None:
    client.cookies.set("argus_theme", "dark")
    assert '<html lang="en" data-theme="dark">' in client.get("/today").text
    client.cookies.set("argus_theme", "bogus")
    assert '<html lang="en">' in client.get("/today").text


def test_static_assets_are_versioned_and_immutable(client: TestClient) -> None:
    html = client.get("/today").text
    css = re.search(r'href="(/static/dist/app\.css\?v=[0-9a-f]{10})"', html)
    assert css
    r = client.get(css.group(1))
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/static/missing.js").status_code == 404


# ---------------------------------------------------------------- budgets


def test_js_and_css_under_150kb_gzipped() -> None:
    files = ["dist/app.css", "app.js", "vendor/alpine.min.js", "vendor/htmx.min.js"]
    total = sum(len(gzip.compress((STATIC_DIR / f).read_bytes())) for f in files)
    assert total < 150 * 1024, f"{total / 1024:.1f} KB gzipped"


def test_fonts_fit_their_budget(client: TestClient) -> None:
    """D066: the display face is at most 45 KB, and the fonts a page preloads at most 90 KB."""
    assert (STATIC_DIR / "fonts/display-bricolage.woff2").stat().st_size <= 45 * 1024
    for path in ["/today", "/kiosk"]:
        preloads = re.findall(
            r'<link rel="preload" href="/static/([^"]+)" as="font"', client.get(path).text
        )
        assert "fonts/display-bricolage.woff2" in preloads
        total = sum((STATIC_DIR / f).stat().st_size for f in preloads)
        assert total <= 90 * 1024, f"{path}: {total / 1024:.1f} KB of preloaded fonts"


def test_main_views_render_fast(client: TestClient) -> None:
    for path in [
        "/today",
        "/upcoming",
        "/health",
        "/workload",
        "/exams",
        "/courses/CMSC351",
        "/settings",
    ]:
        client.get(path)  # warm
        samples = []
        for _ in range(9):
            t = time.perf_counter()
            client.get(path)
            samples.append(time.perf_counter() - t)
        median = sorted(samples)[len(samples) // 2]
        # The budget is 150 ms on the Pi; a dev machine should be far under it.
        assert median < 0.15, f"{path}: {median * 1000:.0f} ms"


def test_repo_running_timer_after_seed(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_db(tmp_path / "x.db", clock)
    seed_demo(conn, clock.now())
    timer = repo.running_timer(conn)
    assert timer is not None
    assert timer.hours(clock.now()) == pytest.approx(0.7)
