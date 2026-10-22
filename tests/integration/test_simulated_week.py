"""A simulated week, end to end (harness: `simweek.py`).

Fake Canvas, Gradescope, and Piazza feed the real worker (collectors -> store -> mocked AI
-> estimates -> planner -> reminders -> ntfy) every 15 minutes of a frozen clock, from
Monday 08:00 to Sunday 23:45 local. Assertions check the deadline and reminder promises.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from argus.clock import FakeClock, to_local
from argus.config import AppConfig
from argus.estimates import effective_estimates
from argus.store import repo
from argus.store.db import transaction
from tests.integration.simweek import END, QUIET, run_week, with_windows
from tests.integration.test_deadline_golden import GOLDEN, deadline_alerts


async def test_simulated_week(tmp_path: Any, app_config: AppConfig) -> None:
    """No work windows: the time-only rules, and no window messages."""
    conn, sent = await run_week(tmp_path, app_config)
    _check(conn, sent)
    assert not [n for n in repo.recent_notifications(conn, limit=500) if n.rule == "window_plan"]


async def test_simulated_week_with_windows_never_logging(
    tmp_path: Any, app_config: AppConfig
) -> None:
    """Work windows on, and a student who never logs time or answers: no nagging loop (D060)."""
    conn, sent = await run_week(tmp_path, with_windows(app_config))
    _check(conn, sent)
    _check_windows(conn)
    # Deadline alerts are exactly what they were before work windows existed.
    assert deadline_alerts(conn) == json.loads(GOLDEN.read_text())


async def test_simulated_week_with_windows_logging_as_planned(
    tmp_path: Any, app_config: AppConfig
) -> None:
    """A student who does what each window planned and says so: nothing is planned twice."""

    def answer_last_window(conn: sqlite3.Connection, clock: FakeClock) -> None:
        plan = repo.latest_ended_window_plan(conn, clock.now())
        if plan is None or clock.now() - plan.ends_at >= timedelta(minutes=15):
            return
        with transaction(conn):
            for entry, hours in plan.allocation:
                if entry.startswith("item:"):
                    item_id = int(entry.split(":", 1)[1])
                    repo.checkin(
                        conn,
                        item_id,
                        window_id=plan.window_id,
                        window_start=plan.starts_at,
                        hours=hours,
                        now=clock.now(),
                    )

    conn, sent = await run_week(tmp_path, with_windows(app_config), student=answer_last_window)
    _check(conn, sent)
    _check_windows(conn)
    # Each item gets at most its estimate (p80 = 3 h, calibrated once finished work shows the
    # student's actual time) of window time across all windows, so it is named in no more
    # windows than that needs.
    planned: Counter[int] = Counter()
    windows_named: Counter[int] = Counter()
    for r in conn.execute("SELECT allocation FROM window_plans"):
        for entry, hours in json.loads(r["allocation"]):
            if entry.startswith("item:"):
                planned[int(entry.split(":", 1)[1])] += hours
                windows_named[int(entry.split(":", 1)[1])] += 1
    eff = effective_estimates(conn, END)
    assert planned and all(h <= eff[i].p80 + 1e-6 for i, h in planned.items()), planned
    capacity = 4.0  # the shortest evening window
    for item_id, n in windows_named.items():
        # +2: a window split by a due time, and work that arrives mid-week and goes ahead of it
        # (Quiz B is estimated only once its Canvas description appears on Wednesday, D069).
        assert n <= -(-planned[item_id] // capacity) + 2, (item_id, n)


def _check_windows(conn: sqlite3.Connection) -> None:
    notifications = repo.recent_notifications(conn, limit=500)
    window_msgs = [n for n in notifications if n.rule == "window_plan"]
    assert window_msgs, "the week has work windows with work in them"
    keys = [n.key for n in window_msgs]
    assert len(keys) == len(set(keys))  # one message per window, at most
    assert len(keys) <= 9  # Mon-Thu evenings and Sunday afternoon this week
    at_risk: Counter[int] = Counter()
    plans = [
        (repo.parse_iso(r["starts_at"]), repo.parse_iso(r["ends_at"]), json.loads(r["allocation"]))
        for r in conn.execute("SELECT * FROM window_plans")
    ]
    for n in notifications:
        if n.rule != "at_risk" or n.status != "sent" or n.sent_at is None:
            continue
        for item_id in n.item_ids:
            at_risk[item_id] += 1
            for start, end, alloc in plans:
                inside = start <= n.sent_at < end
                assert not (inside and f"item:{item_id}" in [e for e, _ in alloc]), (n, start)
    assert all(v <= 1 for v in at_risk.values()), at_risk  # once per item and due date


def _check(conn: sqlite3.Connection, sent: list[tuple[datetime, dict[str, Any]]]) -> None:
    notifications = repo.recent_notifications(conn, limit=500)
    rules_by_item: Counter[tuple[str, int]] = Counter()
    for n in notifications:
        for item_id in n.item_ids:
            rules_by_item[(n.rule, item_id)] += 1
    items = {i.title: i for i in repo.list_items(conn)}

    # One item per assignment: the announced Quiz B merged with Canvas's, PS C merged with Gradescope's.
    assert sorted(items) == ["Homework A", "Homework D", "Lab E", "Problem Set C", "Quiz B"]
    assert not items["Quiz B"].announced  # official date took over
    assert items["Problem Set C"].date_conflict  # 23:30 vs 23:59 flagged

    # Every open deadline this week got exactly one 3-hour warning; submitted work got none.
    for title in ("Lab E", "Quiz B", "Problem Set C"):
        assert rules_by_item[("due_3h", items[title].id)] == 1, title
    assert rules_by_item[("due_3h", items["Homework A"].id)] == 0
    # 24-hour reminders: once each where the window allowed.
    for title in ("Lab E", "Problem Set C", "Homework A"):
        assert rules_by_item[("due_24h", items[title].id)] == 1, title
    # Homework D appeared Thursday: announced as new, once.
    assert rules_by_item[("new_item", items["Homework D"].id)] == 1

    # Nothing in quiet hours except 3-hour warnings; the daily cap holds.
    capped_per_day: Counter[date] = Counter()
    for when, body in sent:
        local = to_local(when)
        if QUIET.contains(local.time()):
            assert body["title"].startswith(("Due in", "Due within")), (local, body["title"])
        if body["title"] not in ("At risk",) and not body["title"].startswith(
            ("Due in", "Due within")
        ):
            capped_per_day[local.date()] += 1
    assert max(capped_per_day.values()) <= 6

    # Titles match the local calendar day (a 24h reminder deferred past quiet hours says "today").
    for when, body in sent:
        if body["title"] in ("Due today", "Due tomorrow"):
            due_day = re.search(r", (Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", body["message"])
            assert due_day is not None
            same_day = due_day.group(1) == f"{to_local(when):%a}"
            assert body["title"] == ("Due today" if same_day else "Due tomorrow"), (when, body)

    # Idempotency: every send has a unique key and nothing was delivered twice.
    keys = [k for (k,) in conn.execute("SELECT key FROM notification_keys")]
    assert len(keys) == len(set(keys))
    assert len(sent) == sum(1 for n in notifications if n.status == "sent")
