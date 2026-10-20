"""Estimate detail, workload, and announcements views and their actions."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.store import repo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner
from tests.ai_fakes import FakeTransport, ok, recorded

ORIGIN = {"Origin": "http://testserver"}
HX = {**ORIGIN, "HX-Request": "true"}
HW3, PS6, MIDTERM_FINDING = 2, 6, 1


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport(
        responder=lambda req: ok(recorded("estimate_hw3"), model="claude-sonnet-5-5")
    )


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock, transport: FakeTransport) -> Iterator[TestClient]:
    db = tmp_path / "w.db"
    conn = open_db(db, clock)
    seed_demo(conn, clock.now())
    conn.close()
    app = create_app(
        DEMO_CONFIG,
        DEMO_SECRETS,
        clock,
        db_path=db,
        runner_factory=lambda c, f: OfflineRunner(),
        ai_transport=transport,
    )
    with TestClient(app) as c:
        yield c


def conn_of(client: TestClient) -> sqlite3.Connection:
    c = sqlite3.connect(client.app.state.argus.db_path)  # type: ignore[attr-defined]
    c.row_factory = sqlite3.Row
    return c


def test_item_detail(client: TestClient) -> None:
    html = client.get(f"/items/{HW3}").text
    assert "Write up the three proofs" in html
    assert "Each proof is similar in length" in html
    assert "took 1.40x their estimates (1 finished). Argus applies 1.10x" in html
    assert "AI estimate before calibration" in html
    assert "claude-sonnet-5-5, prompt estimate.v1" in html
    assert client.get("/items/9999").status_code == 404


def test_item_rows_show_time_left_and_link_to_detail(client: TestClient) -> None:
    html = client.get("/upcoming").text
    assert f'href="/items/{HW3}"' in html
    assert re.search(r'<span class="tnum">~\d+(\.\d)? h</span>', html)  # remaining p80 (D065)
    assert "source-link" not in html  # sources live on the item page


def test_override_clear_and_replan(client: TestClient) -> None:
    r = client.post(
        f"/items/{HW3}/estimate",
        data={"hours": "8"},
        headers={**ORIGIN, "Referer": f"http://testserver/items/{HW3}"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/items/{HW3}"
    html = client.get(f"/items/{HW3}").text
    assert "You set this estimate." in html
    assert "<strong>8 h</strong> left to do." in html  # the plan uses the override
    client.post(f"/items/{HW3}/estimate/clear", headers=ORIGIN)
    assert "You set this estimate." not in client.get(f"/items/{HW3}").text
    assert (
        client.post(f"/items/{HW3}/estimate", data={"hours": "0"}, headers=ORIGIN).status_code
        == 422
    )
    assert (
        client.post("/items/9999/estimate", data={"hours": "2"}, headers=ORIGIN).status_code == 404
    )


def test_rerun_uses_ai_without_cache(client: TestClient, transport: FakeTransport) -> None:
    client.post(f"/items/{PS6}/estimate/rerun", headers=ORIGIN)
    client.post(f"/items/{PS6}/estimate/rerun", headers=ORIGIN)
    assert len(transport.calls) == 2
    sources = [e.source for e in repo.estimates_for(conn_of(client), PS6)]
    assert sources == ["default", "ai", "ai"]


def test_log_time(client: TestClient) -> None:
    client.post(f"/items/{HW3}/log", data={"hours": "1.5"}, headers=ORIGIN)
    html = client.get(f"/items/{HW3}").text
    assert 'Time spent <span class="section-count">1.5 h</span>' in html
    assert "Logged here" in html
    assert client.post(f"/items/{HW3}/log", data={"hours": "30"}, headers=ORIGIN).status_code == 422


def test_workload(client: TestClient) -> None:
    html = client.get("/workload").text
    assert "Work due against window time" in html and "Next 7 days:" in html
    assert 'class="wl__plot"' in html and "Show as a table" in html
    # Tonight's window (7-11 pm) is 4 h: Homework 3 (due 11:30 pm) and Problem Set 6 (11:59 pm,
    # after Homework 3) don't both fit, so both need attention.
    assert "Homework 3" in html and "Problem Set 6" in html
    assert "more hours than your work windows have before it is due" in html


def test_announcements_and_decisions(client: TestClient) -> None:
    html = client.get("/announcements").text
    assert "1 needs your decision." in html
    assert "Use announced date" in html
    assert "Lab 3 room change" in html  # not analyzed yet: the no-AI fallback
    r = client.post(f"/findings/{MIDTERM_FINDING}/confirm", headers=HX)
    assert json.loads(r.headers["HX-Trigger"])["toast"]["message"].startswith(
        "Using the announced date for Midterm 1: "
    )
    assert "Use announced date" not in r.text
    again = client.post(f"/findings/{MIDTERM_FINDING}/confirm", headers=HX)
    assert json.loads(again.headers["HX-Trigger"])["toast"]["kind"] == "error"
    assert client.post(f"/findings/{MIDTERM_FINDING}/confirm", headers=ORIGIN).status_code == 409
    assert client.post(f"/findings/{MIDTERM_FINDING}/bogus", headers=HX).status_code == 404
    assert client.post("/findings/999/dismiss", headers=HX).status_code == 404


def test_dismiss_plain_form(client: TestClient) -> None:
    r = client.post(f"/findings/{MIDTERM_FINDING}/dismiss", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/announcements"


def test_health_shows_ai_spend(client: TestClient) -> None:
    html = client.get("/health").text
    assert "$0.00 of $10.00" in html
    assert "(AI not set up)" not in html  # a transport is configured
