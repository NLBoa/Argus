"""A simulated week, end to end (harness: `simweek.py`).

Fake Canvas, Gradescope, and Piazza feed the real worker (collectors -> store -> mocked AI
-> estimates -> planner -> reminders -> ntfy) every 15 minutes of a frozen clock, from
Monday 08:00 to Sunday 23:45 local. Assertions check the deadline and reminder promises.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import date, datetime
from typing import Any

from argus.clock import to_local
from argus.config import AppConfig
from argus.store import repo
from tests.integration.simweek import QUIET, run_week


async def test_simulated_week(tmp_path: Any, app_config: AppConfig) -> None:
    conn, sent = await run_week(tmp_path, app_config)
    _check(conn, sent)


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

    # Work is planned once: never more than the estimate (p80 = 3 h) per item, so nudges stop.
    planned = Counter[int]()
    for b in conn.execute("SELECT item_id, start_at, end_at FROM plan_blocks WHERE kind = 'work'"):
        s_, e_ = (
            datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (b["start_at"], b["end_at"])
        )
        planned[b["item_id"]] += (e_ - s_).total_seconds() / 3600
    assert planned and max(planned.values()) <= 3.0 + 1e-9, planned
    nudges = Counter(n.item_ids[0] for n in notifications if n.rule == "start_nudge")
    assert max(nudges.values()) <= 3, nudges

    # Start nudges went out, and no work session ever overlapped class.
    assert any(n.rule == "start_nudge" for n in notifications)
    for b in conn.execute("SELECT start_at, end_at FROM plan_blocks"):
        start = to_local(datetime.fromisoformat(b["start_at"].replace("Z", "+00:00")))
        end = to_local(datetime.fromisoformat(b["end_at"].replace("Z", "+00:00")))
        if start.weekday() in (1, 3):
            class_start = start.replace(hour=9, minute=15)  # lecture 9:30 minus the walking buffer
            class_end = start.replace(hour=11, minute=0)
            assert end <= class_start or start >= class_end, (start, end)

    # Idempotency: every send has a unique key and nothing was delivered twice.
    keys = [k for (k,) in conn.execute("SELECT key FROM notification_keys")]
    assert len(keys) == len(set(keys))
    assert len(sent) == sum(1 for n in notifications if n.status == "sent")
