"""Today's queue actions (D058, D064): every route with and without JS, refused when stale or
cross-origin, and the confirm line."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.capabilities import derive
from argus.clock import FakeClock, local_datetime, to_local
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.planning import compute, replan
from argus.store import repo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner

ORIGIN = {"origin": "http://testserver", "referer": "http://testserver/today"}
HX = {**ORIGIN, "hx-request": "true"}
EVIL = {"origin": "https://evil.example"}
TRACKING = derive(DEMO_CONFIG, demo_secrets()).tracking_sources
PS6, HW4, HW3 = "item:6", "item:4", "item:2"  # the demo queue starts PS6, HW4, HW3, Quiz 2


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


def db(path: Path, clock: FakeClock) -> sqlite3.Connection:
    return open_db(path, clock)


def order(path: Path, clock: FakeClock) -> list[str]:
    conn = db(path, clock)
    try:
        return [
            e.entry_id
            for e in compute(conn, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING).queue
        ]
    finally:
        conn.close()


def toast(r: object) -> dict[str, str]:
    return json.loads(r.headers["hx-trigger"])["toast"]  # type: ignore[attr-defined]


def test_today_leads_with_the_next_task_and_lists_the_rest_in_order(client: TestClient) -> None:
    html = client.get("/today").text
    hero = html[html.index('id="hero"') :]
    assert "Next up" in hero[:400] and "Problem Set 6" in hero[:800]
    assert "Needs 4.5 h more than your windows have before it" in html
    queue = html[html.index('id="queue-heading"') :]
    assert queue.index("Homework 4") < queue.index("Homework 3") < queue.index("Quiz 2")
    assert 'value="2"' in queue  # numbers continue from the hero (1)


def test_move_without_and_with_js(client: TestClient, db_path: Path, clock: FakeClock) -> None:
    r = client.post(
        f"/queue/{HW4}/move",
        data={"dir": "up", "before": PS6},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/today#q-item-4"
    assert order(db_path, clock)[:2] == [HW4, PS6]
    r = client.post(f"/queue/{HW4}/move", data={"dir": "down", "before": PS6}, headers=HX)
    assert r.status_code == 200 and r.text.lstrip().startswith('<div id="queue"')
    assert toast(r)["message"] == "Moved Homework 4 to 2 of 8"
    assert 'data-focus="q-item-4:down"' in r.text
    assert order(db_path, clock)[:2] == [PS6, HW4]


def test_move_to_the_top_hands_focus_to_the_other_arrow(client: TestClient) -> None:
    r = client.post(f"/queue/{HW4}/move", data={"dir": "up", "before": PS6}, headers=HX)
    assert 'data-focus="hero:down"' in r.text  # Homework 4 is the hero now; Move up is gone


def test_a_stale_move_is_refused_and_the_list_shown_again(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    before = order(db_path, clock)
    r = client.post(f"/queue/{HW3}/move", data={"dir": "up", "before": PS6}, headers=HX)
    assert r.status_code == 200 and toast(r)["kind"] == "error"
    assert "The list changed since you loaded it" in r.text
    r = client.post(
        f"/queue/{HW3}/move",
        data={"dir": "up", "before": PS6},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert r.headers["location"] == "/today?changed=1#q-item-2"
    assert "The list changed since you loaded it" in client.get("/today?changed=1").text
    assert order(db_path, clock) == before


def test_pin_and_not_tonight(client: TestClient, db_path: Path, clock: FakeClock) -> None:
    r = client.post("/queue/item:3/pin", data={"pin": "1"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/today#q-item-3"
    assert order(db_path, clock)[0] == "item:3"
    r = client.post("/queue/item:3/pin", data={"pin": "0"}, headers=HX)
    assert toast(r)["message"] == "Unpinned Quiz 2"
    r = client.post(f"/queue/{PS6}/skip", data={"skip": "1"}, headers=HX)
    assert toast(r)["message"] == "Problem Set 6 moved out of tonight"
    assert order(db_path, clock)[-1] == PS6
    assert 'id="skipped-heading"' in r.text and "Back to tonight" in r.text
    r = client.post(
        f"/queue/{PS6}/skip", data={"skip": "0"}, headers=ORIGIN, follow_redirects=False
    )
    assert r.status_code == 303 and order(db_path, clock)[0] == PS6


def test_time_left(client: TestClient, db_path: Path, clock: FakeClock) -> None:
    r = client.post("/items/2/left", data={"hours": "1"}, headers=HX)
    assert toast(r)["message"] == "About 1 h left on Homework 3"
    assert "~1 h left" in r.text
    r = client.post("/items/2/left", data={"hours": "0.5"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/today#q-item-2"
    assert client.post("/items/2/left", data={"hours": "abc"}, headers=ORIGIN).status_code == 422
    assert client.post("/items/2/left", data={"hours": "500"}, headers=ORIGIN).status_code == 422
    conn = db(db_path, clock)
    assert [r[0] for r in conn.execute("SELECT hours FROM remaining_marks")] == [1.0, 0.5]


def test_done_from_the_queue_leaves_an_undo(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    r = client.post("/items/6/done", data={"done": "1", "view": "queue"}, headers=HX)
    assert toast(r)["message"] == "Marked Problem Set 6 done"
    assert "Marked <strong>Problem Set 6</strong> done." in r.text and "Undo" in r.text
    assert PS6 not in order(db_path, clock)
    r = client.post("/items/6/done", data={"done": "0", "view": "queue"}, headers=HX)
    assert PS6 in order(db_path, clock) and "Undo" not in r.text


def test_the_timer_from_the_hero(client: TestClient) -> None:
    r = client.post("/items/6/timer", data={"action": "start", "view": "queue"}, headers=HX)
    assert "Working on" in r.text and "Stop timer" in r.text and 'data-focus="hero:timer"' in r.text
    r = client.post("/items/6/timer", data={"action": "stop", "view": "queue"}, headers=HX)
    assert "Next up" in r.text


def test_the_confirm_line_asks_once_and_records_the_answer(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    monday = to_local(clock.now()).date()
    clock.set(local_datetime(monday, 18, 50))
    conn = db(db_path, clock)
    replan(conn, DEMO_CONFIG, clock, tracking_sources=TRACKING)  # stores tonight's split
    clock.set(local_datetime(monday, 23, 30))
    html = client.get("/today").text
    assert "Earlier today: did you work on <strong>Problem Set 6</strong>?" in html
    assert 'value="4.0"' in html and ">None<" in html
    r = client.post(
        f"/windows/{monday}T19:00/confirm",
        data={"item_id": "6", "hours": "2"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/today#queue"
    rows = conn.execute("SELECT source, window_id FROM time_logs WHERE item_id = 6").fetchall()
    assert [tuple(x) for x in rows] == [("checkin", f"{monday}T19:00")]
    assert "did you work on" not in client.get("/today").text
    assert (
        client.post(
            f"/windows/{monday}T07:00/confirm", data={"item_id": "6", "hours": "1"}, headers=ORIGIN
        ).status_code
        == 404
    )
    assert repo.window_plan(conn, f"{monday}T19:00") is not None


@pytest.mark.parametrize(
    ("path", "data"),
    [
        (f"/queue/{HW3}/move", {"dir": "up", "before": PS6}),
        (f"/queue/{HW3}/pin", {"pin": "1"}),
        (f"/queue/{HW3}/skip", {"skip": "1"}),
        ("/items/2/left", {"hours": "1"}),
        ("/windows/2026-10-19T19:00/confirm", {"item_id": "2", "hours": "1"}),
    ],
)
def test_cross_origin_posts_are_refused(
    client: TestClient, path: str, data: dict[str, str]
) -> None:
    assert client.post(path, data=data, headers=EVIL).status_code == 403


def test_bad_entries_and_directions(client: TestClient) -> None:
    assert client.post("/queue/item:999/pin", data={"pin": "1"}, headers=ORIGIN).status_code == 404
    assert client.post("/queue/bogus/pin", data={"pin": "1"}, headers=ORIGIN).status_code == 404
    assert (
        client.post(f"/queue/{HW3}/move", data={"dir": "sideways"}, headers=ORIGIN).status_code
        == 422
    )


def test_estimate_and_log_forms_answer_htmx_with_a_toast(
    client: TestClient, db_path: Path, clock: FakeClock
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


def test_the_time_left_field_accepts_any_amount_and_starts_empty_without_an_estimate(
    client: TestClient,
) -> None:
    html = client.get("/today").text
    field = html[html.index('id="q-item-2-hours"') :][:300]
    assert 'step="any"' in field  # a pre-filled 2.4 must not block its own submit (review)
    survey = html[html.index('id="q-item-11-hours"') :][:300]  # no estimate: nothing pre-filled
    assert 'value=""' in survey


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


def test_the_timer_without_js_replans(client: TestClient, db_path: Path, clock: FakeClock) -> None:
    conn = db(db_path, clock)
    conn.execute("UPDATE plan_state SET planned_at = '2000-01-01T00:00:00Z'")
    client.post(
        "/items/6/timer",
        data={"action": "start", "view": "queue"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert conn.execute("SELECT planned_at FROM plan_state").fetchone()[0] != "2000-01-01T00:00:00Z"


def test_set_time_asks_for_work_without_an_estimate(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    """D069: the course survey has no description, so no estimate; its card and row ask for a
    time, one tap away. Homework 3 has an AI estimate and doesn't."""
    html = client.get("/today").text
    assert 'id="set-time-11-q"' in html and 'id="set-time-2-q"' not in html
    panel = html[html.index('id="set-time-11-q"') :][:3000]
    assert "How long will it take?" in panel and 'name="hours" value="0.5">30 min</button>' in panel
    assert 'name="view" value="queue"' in panel
    r = client.post("/items/11/estimate", data={"hours": "2", "view": "queue"}, headers=HX)
    assert toast(r)["message"] == "Set your estimate for Course survey to 2 h"
    assert 'id="set-time-11-q"' not in r.text and "~2 h left" in r.text
    assert 'data-focus="q-item-11:left"' in r.text
    plain = client.post(
        "/items/11/estimate",
        data={"hours": "3", "view": "queue"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert plain.status_code == 303 and plain.headers["location"] == "/today#q-item-11"
    conn = db(db_path, clock)
    rows = conn.execute("SELECT source, p50_hours FROM estimates WHERE item_id = 11").fetchall()
    assert [tuple(r) for r in rows] == [("user", 2.0), ("user", 3.0)]


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
