"""Estimates and logged time from the web (D069): the item page and a row's "Set time", with and
without JS. The ranked queue is gone from the web (D072)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.capabilities import derive
from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.planning import replan
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner

ORIGIN = {"origin": "http://testserver", "referer": "http://testserver/today"}
HX = {**ORIGIN, "hx-request": "true"}
TRACKING = derive(DEMO_CONFIG, demo_secrets()).tracking_sources


@pytest.fixture
def db_path(tmp_path: Path, clock: FakeClock) -> Path:
    path = tmp_path / "q.db"
    conn = open_db(path, clock)
    seed_demo(conn, clock.now(), DemoProfile.FULL)
    conn.execute("UPDATE time_logs SET ended_at = started_at WHERE ended_at IS NULL")  # no timer
    replan(conn, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    conn.close()
    return path


@pytest.fixture
def client(db_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(
        DEMO_CONFIG,
        demo_secrets(),
        clock,
        db_path=db_path,
        runner_factory=lambda c, f: OfflineRunner(),
    )
    with TestClient(app) as c:
        yield c


def toast(r: object) -> dict[str, str]:
    return json.loads(r.headers["hx-trigger"])["toast"]  # type: ignore[attr-defined]


def test_estimate_and_log_forms_answer_htmx_with_a_toast(
    client: TestClient,
) -> None:
    item_hx = {**HX, "referer": "http://testserver/items/2"}
    r = client.post("/items/2/estimate", data={"hours": "3"}, headers=item_hx)
    assert r.status_code == 200 and toast(r)["message"] == "Set your estimate for Homework 3 to 3 h"
    assert '<main id="main"' in r.text
    r = client.post("/items/2/log", data={"hours": "1"}, headers=item_hx)
    assert r.status_code == 200 and toast(r)["message"] == "Logged 1 h on Homework 3"
    r = client.post("/items/2/estimate/clear", headers=item_hx)
    assert toast(r)["message"] == "Removed your estimate for Homework 3"
    bad = client.post("/items/2/log", data={"hours": "30"}, headers=item_hx)
    assert bad.status_code == 200 and toast(bad)["kind"] == "error"
    plain = client.post(
        "/items/2/log",
        data={"hours": "1"},
        headers={**ORIGIN, "referer": "http://testserver/items/2"},
        follow_redirects=False,
    )
    assert plain.status_code == 303 and plain.headers["location"] == "/items/2"


def test_a_config_lead_time_outside_the_choices_is_shown_and_kept(
    tmp_path: Path, clock: FakeClock
) -> None:
    cfg = DEMO_CONFIG.model_copy(
        update={
            "notifications": DEMO_CONFIG.notifications.model_copy(
                update={"window_notice_minutes": 25}
            )
        }
    )
    path = tmp_path / "n.db"
    conn = open_db(path, clock)
    seed_demo(conn, clock.now(), DemoProfile.FULL)
    conn.close()
    app = create_app(
        cfg, demo_secrets(), clock, db_path=path, runner_factory=lambda c, f: OfflineRunner()
    )
    with TestClient(app) as c:
        html = c.get("/settings").text
        assert '<option value="25" selected>25 minutes before</option>' in html
        r = c.post(
            "/settings/notice", data={"minutes": "25"}, headers=ORIGIN, follow_redirects=False
        )
        assert r.status_code == 303


def test_set_time_from_a_row(client: TestClient) -> None:
    html = client.get("/upcoming").text
    assert 'id="set-time-11"' in html and 'id="set-time-2"' not in html
    bad = client.post("/items/11/estimate", data={"hours": "0", "view": "row"}, headers=HX)
    assert toast(bad)["kind"] == "error" and 'id="set-time-11"' in bad.text
    r = client.post("/items/11/estimate", data={"hours": "1", "view": "row"}, headers=HX)
    assert toast(r)["message"] == "Set your estimate for Course survey to 1 h"
    assert r.text.lstrip().startswith('<li class="item-row" id="item-11"')
    assert "set-time" not in r.text and "~1 h" in r.text
    plain = client.post(
        "/items/11/estimate",
        data={"hours": "2", "view": "row"},
        headers={**ORIGIN, "referer": "http://testserver/upcoming"},
        follow_redirects=False,
    )
    assert plain.status_code == 303 and plain.headers["location"] == "/upcoming"


def test_the_item_page_offers_presets_without_an_estimate(client: TestClient) -> None:
    html = client.get("/items/11").text
    assert "How long do you think it will take?" in html
    assert 'name="view" value="item"' in html
    assert "data-estimate-state" not in html  # no AI set up here: no word about estimating


@pytest.mark.parametrize(
    "path",
    ["/queue/item:2/move", "/queue/item:2/pin", "/queue/item:2/skip", "/items/2/left"],
)
def test_the_queue_routes_are_gone(client: TestClient, path: str) -> None:
    """D072: Today lists deadlines; nothing on the web ranks them any more."""
    assert client.post(path, data={}, headers=ORIGIN).status_code == 404
