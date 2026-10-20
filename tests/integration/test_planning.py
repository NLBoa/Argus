"""The planning service against a real store (D057-D062): seeding, progress, the confirm question,
the legacy conversion, and risk that only gets worse while nothing is logged."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from argus.capabilities import derive
from argus.clock import FakeClock, local_datetime, to_local
from argus.config import AppConfig
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.planning import compute, pending_confirmation, replan
from argus.store import repo
from argus.store.db import iso, open_db, transaction

TRACKING = derive(DEMO_CONFIG, demo_secrets()).tracking_sources


@pytest.fixture
def demo(tmp_path: Path, clock: FakeClock) -> sqlite3.Connection:
    conn = open_db(tmp_path / "plan.db", clock)
    seed_demo(conn, clock.now(), DemoProfile.FULL)
    return conn


def item_id(conn: sqlite3.Connection, title: str) -> int:
    return int(conn.execute("SELECT id FROM items WHERE title = ?", (title,)).fetchone()[0])


def no_windows(cfg: AppConfig) -> AppConfig:
    return cfg.model_copy(update={"schedule": cfg.schedule.model_copy(update={"work_windows": []})})


# ---------------------------------------------------------------- seeding (D057)


def test_config_seeds_windows_once_and_then_settings_own_them(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    replan(db, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    assert len(repo.work_windows(db)) == 2  # Mon-Thu evenings, Sunday afternoon
    assert db.execute("SELECT windows_seeded_at FROM plan_state").fetchone()[0] is not None
    with transaction(db):
        repo.set_work_windows(db, [], clock.now())  # the student removes them all
    replan(db, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    assert repo.work_windows(db) == []  # never re-seeded


def test_config_without_windows_does_not_mark_them_seeded(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    replan(db, no_windows(DEMO_CONFIG), clock, tracking_sources=TRACKING)
    assert repo.work_windows(db) == []
    assert db.execute("SELECT windows_seeded_at FROM plan_state").fetchone()[0] is None
    replan(db, DEMO_CONFIG, clock, tracking_sources=TRACKING)  # added to config.yaml later
    assert len(repo.work_windows(db)) == 2


# ---------------------------------------------------------------- progress (D060)


def test_remaining_is_estimate_minus_logged_time_and_a_running_timer_counts(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    hw4 = item_id(demo, "Homework 4")
    before = compute(demo, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING).items[hw4].remaining
    assert before is not None and before > 1
    with transaction(demo):
        repo.start_timer(demo, hw4, clock.now())
    later = clock.now() + timedelta(hours=1)
    after = compute(demo, DEMO_CONFIG, later, tracking_sources=TRACKING).items[hw4].remaining
    assert after == pytest.approx(before - 1.0)


def test_a_time_left_mark_counts_only_time_logged_after_it(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    hw4 = item_id(demo, "Homework 4")
    with transaction(demo):
        repo.log_time(demo, hw4, 1.0, clock.now() - timedelta(hours=2))  # before the mark
        repo.add_remaining_mark(demo, hw4, 2.0, clock.now(), "web")
        repo.log_time(demo, hw4, 0.5, clock.now() + timedelta(hours=1))  # after it
    later = clock.now() + timedelta(hours=2)
    assert compute(demo, DEMO_CONFIG, later, tracking_sources=TRACKING).items[hw4].remaining == (
        pytest.approx(1.5)
    )


def test_calibration_ignores_legacy_time(demo: sqlite3.Connection, clock: FakeClock) -> None:
    hw2 = item_id(demo, "Homework 2")  # finished, 3.5 h logged in the demo
    with transaction(demo):
        demo.execute(
            "INSERT INTO time_logs (item_id, started_at, ended_at, source, created_at)"
            " VALUES (?, ?, ?, 'legacy', ?)",
            (
                hw2,
                iso(clock.now() - timedelta(days=9)),
                iso(clock.now() - timedelta(days=8)),
                iso(clock.now()),
            ),
        )
    assert repo.logged_hours(demo, clock.now())[hw2] > repo.actual_hours(demo, clock.now())[hw2]
    [row] = [r for r in repo.finished_history(demo, clock.now()) if r.item_id == hw2]
    assert row.actual == pytest.approx(3.5)


def test_legacy_sessions_convert_once_and_only_without_real_logs(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    hw4, quiz = item_id(demo, "Homework 4"), item_id(demo, "Quiz 2")
    now = clock.now()
    with transaction(demo):
        demo.execute("UPDATE plan_state SET legacy_converted_at = NULL")
        for item, start, hours in (
            (hw4, now - timedelta(hours=5), 2),
            (quiz, now - timedelta(hours=3), 1),
        ):
            demo.execute(
                "INSERT INTO plan_blocks_legacy (item_id, kind, start_at, end_at, created_at)"
                " VALUES (?, 'work', ?, ?, ?)",
                (item, iso(start), iso(start + timedelta(hours=hours)), iso(now)),
            )
        repo.log_time(demo, quiz, 0.5, now)  # Quiz 2 has real time: its logs are the truth
    with transaction(demo):
        assert repo.convert_legacy_blocks(demo, now) == 1
        assert repo.convert_legacy_blocks(demo, now) is None  # once
    sources = [r[0] for r in demo.execute("SELECT source FROM time_logs WHERE item_id = ?", (hw4,))]
    assert sources == ["legacy"]


# ---------------------------------------------------------------- the confirm question (D060)


def window_tonight(demo: sqlite3.Connection, clock: FakeClock) -> tuple[str, int]:
    """Plan tonight's window (Mon 7-11 pm) the way the worker does, 10 minutes before it."""
    clock.set(local_datetime(to_local(clock.now()).date(), 18, 50))
    replan(demo, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    plan = compute(demo, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING)
    assert plan.window is not None
    stored = repo.window_plan(demo, plan.window.window_id)
    assert stored is not None and stored.allocation
    first = next(e for e, _ in stored.allocation if e.startswith("item:"))
    return plan.window.window_id, int(first.split(":", 1)[1])


def test_after_a_window_with_no_logs_argus_asks_once(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    window_id, first = window_tonight(demo, clock)
    clock.set(clock.now() + timedelta(hours=5))  # 11:50 pm: the window is over
    plan = compute(demo, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING)
    asked = pending_confirmation(demo, plan)
    assert asked is not None and asked.window_id == window_id and first in asked.item_ids
    stored = repo.window_plan(demo, window_id)
    assert stored is not None
    with transaction(demo):
        assert repo.checkin(
            demo,
            first,
            window_id=window_id,
            window_start=stored.starts_at,
            hours=0,
            now=clock.now(),
        )
        assert not repo.checkin(  # answered once
            demo,
            first,
            window_id=window_id,
            window_start=stored.starts_at,
            hours=2,
            now=clock.now(),
        )
    again = pending_confirmation(demo, plan)
    assert again is None or first not in again.item_ids


def test_an_sms_log_answers_the_question_and_no_answer_means_no_credit(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    window_id, first = window_tonight(demo, clock)
    clock.set(clock.now() + timedelta(hours=5))
    before = compute(demo, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING)
    assert before.items[first].remaining is not None  # nothing was assumed for the window
    with transaction(demo):
        repo.log_time(demo, first, 1.5, clock.now(), source="sms")  # LOG 1 1.5h the next day
    plan = compute(demo, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING)
    asked = pending_confirmation(demo, plan)
    assert asked is None or first not in asked.item_ids
    assert window_id


def test_the_question_goes_when_the_next_window_ends(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    window_tonight(demo, clock)
    clock.set(
        local_datetime(to_local(clock.now()).date() + timedelta(days=1), 23, 5)
    )  # Tue 11:05 pm
    plan = compute(demo, DEMO_CONFIG, clock.now(), tracking_sources=TRACKING)
    asked = pending_confirmation(demo, plan)
    assert asked is None or asked.window_id != "2026-10-19T19:00"


def test_the_split_is_frozen_when_the_window_starts(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    window_id, _ = window_tonight(demo, clock)
    before = repo.window_plan(demo, window_id)
    clock.set(clock.now() + timedelta(hours=2))  # 8:50 pm, inside the window
    replan(demo, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    assert repo.window_plan(demo, window_id) == before


# ---------------------------------------------------------------- risk over time (D059)


def test_with_no_new_logs_risk_only_gets_worse(demo: sqlite3.Connection, clock: FakeClock) -> None:
    """With nothing logged, an item's window time before its due date only shrinks and its demand
    never grows, so its level only gets worse, with one exception (D059): earlier work that can
    no longer be done in time (its deadline passed, or the window time before it ran out) stops
    counting, D drops, and the item may get some room back."""
    with transaction(demo):
        repo.stop_timer(demo, clock.now())  # a running timer is progress: stop it
    replan(demo, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    start = clock.now()
    last: dict[int, tuple[int, float, float]] = {}  # item -> (severity, capacity, demand)
    for hours in range(0, 72, 3):
        now = start + timedelta(hours=hours)
        plan = compute(demo, DEMO_CONFIG, now, tracking_sources=TRACKING)
        for i, ip in plan.items.items():
            due = ip.item.due_at
            if due is None or due <= now or ip.load is None:
                continue
            severity, capacity, demand = ip.level.severity, ip.load.capacity, ip.load.demand
            if i in last:
                was_severity, was_capacity, was_demand = last[i]
                assert capacity <= was_capacity + 1e-9, (ip.item.title, now)
                assert demand <= was_demand + 1e-9, (ip.item.title, now)
                if demand == pytest.approx(was_demand):  # no competing deadline passed
                    assert severity >= was_severity, (ip.item.title, now, ip.level)
            last[i] = (severity, capacity, demand)
        # No overfilled window, ever.
        for sp in plan.spans:
            used = sum(h for _, h in plan.allocation(sp.window_id))
            assert used <= sp.hours_between(now, sp.end) + 1e-9


def test_without_windows_risk_falls_back_to_the_time_only_rule(
    tmp_path: Path, clock: FakeClock
) -> None:
    conn = open_db(tmp_path / "none.db", clock)
    seed_demo(conn, clock.now(), DemoProfile.FULL)
    with transaction(conn):
        repo.set_work_windows(conn, [], clock.now())
    plan = compute(conn, no_windows(DEMO_CONFIG), clock.now(), tracking_sources=TRACKING)
    assert not plan.windows_on and plan.forecast == {}
    assert all(ip.load is None for ip in plan.items.values())


def test_migration_0009_rebuilt_time_logs_and_retired_start_nudges(db: sqlite3.Connection) -> None:
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"time_logs_item", "time_logs_one_running", "time_logs_checkin"} <= names
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "work_windows",
        "queue_prefs",
        "remaining_marks",
        "window_plans",
        "plan_blocks_legacy",
    } <= tables
    assert "plan_blocks" not in tables
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO time_logs (item_id, started_at, source, created_at) VALUES (1, 'x', 'assumed', 'x')"
        )


def test_migration_0009_keeps_logs_and_blocks_and_expires_old_start_nudges(
    tmp_path: Path, clock: FakeClock
) -> None:
    import shutil

    from argus.store.db import MIGRATIONS_DIR, connect, migrate

    old = tmp_path / "old"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("000[1-8]_*.sql")):
        shutil.copy(f, old / f.name)
    conn = connect(tmp_path / "upgrade.db")
    migrate(conn, clock, old)
    now = iso(clock.now())
    with transaction(conn):
        conn.execute(
            "INSERT INTO courses (code, section, created_at, updated_at) VALUES ('CMSC351', '0101', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO items (course_id, kind, title, due_at, created_at, updated_at)"
            " VALUES (1, 'assignment', 'HW', ?, ?, ?)",
            (now, now, now),
        )
        conn.execute(
            "INSERT INTO time_logs (item_id, started_at, ended_at, source, created_at) VALUES (1, ?, ?, 'timer', ?)",
            (now, now, now),
        )
        conn.execute(
            "INSERT INTO plan_blocks (item_id, kind, start_at, end_at, created_at) VALUES (1, 'work', ?, ?, ?)",
            (now, iso(clock.now() + timedelta(hours=1)), now),
        )
        conn.execute(
            "INSERT INTO notifications (key, rule, status, title, body, created_at)"
            " VALUES ('1:start_nudge:x', 'start_nudge', 'pending', 't', 'b', ?)",
            (now,),
        )
    migrate(conn, clock)
    assert conn.execute("SELECT source FROM time_logs").fetchall()[0][0] == "timer"
    assert conn.execute("SELECT COUNT(*) FROM plan_blocks_legacy").fetchone()[0] == 1
    assert repo.retryable_notifications(conn, clock.now()) == []  # never retried


def test_the_window_message_skips_work_done_since_the_split_was_stored(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    from argus.notify.router import Router
    from argus.reminders.engine import ReminderEngine

    window_id, _ = window_tonight(demo, clock)
    stored = repo.window_plan(demo, window_id)
    assert stored is not None
    hw4, quiz = item_id(demo, "Homework 4"), item_id(demo, "Quiz 2")
    with transaction(demo):
        repo.save_window_plan(
            demo,
            window_id,
            starts_at=stored.starts_at,
            ends_at=stored.ends_at,
            allocation=[(f"item:{quiz}", 1.0), ("item:999999", 1.0), (f"item:{hw4}", 2.0)],
            now=clock.now(),
        )
        repo.set_user_done(demo, quiz, True, clock.now())  # done since the split was made
    with transaction(demo):
        repo.refresh_statuses(demo, clock.now(), tracking_sources=TRACKING)
    engine = ReminderEngine(
        demo, DEMO_CONFIG, clock, Router([]), caps=derive(DEMO_CONFIG, demo_secrets())
    )
    snap = engine.snapshot(clock.now())
    assert snap.window is not None
    assert [e.item_id for e in snap.window.entries] == [hw4]  # done and unknown items dropped
    assert snap.in_window == frozenset()  # the window hasn't started yet
    clock.set(stored.starts_at + timedelta(minutes=5))
    assert engine.snapshot(clock.now()).in_window == frozenset({hw4})


def test_an_overdue_item_in_its_late_window_gets_window_time_and_competes(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    """Due this morning, late submissions until Thursday: still workable, so it is planned into
    tonight and counts against work due after it (D059)."""
    hw4 = item_id(demo, "Homework 4")
    late = item_id(demo, "Quiz 2")
    now = clock.now()
    with transaction(demo):
        demo.execute(
            "UPDATE items SET due_at = ?, late_due_at = ? WHERE id = ?",
            (iso(now - timedelta(hours=2)), iso(now + timedelta(days=3)), late),
        )
        repo.refresh_statuses(demo, now, tracking_sources=TRACKING)
    plan = compute(demo, DEMO_CONFIG, now, tracking_sources=TRACKING)
    assert plan.items[late].item.status.is_open
    assert plan.queue[0].entry_id == f"item:{late}"  # overdue but workable: first
    planned = {e for alloc in plan.forecast.values() for e, _ in alloc}
    assert f"item:{late}" in planned
    others = compute(demo, DEMO_CONFIG, now, tracking_sources=TRACKING).items[hw4].load
    assert others is not None and others.demand > (plan.items[hw4].remaining or 0)


def test_the_explanation_is_not_reworded_every_tick(
    demo: sqlite3.Connection, clock: FakeClock
) -> None:
    """Facts like "15.3 hours of work windows" change by the minute; rewording on each would
    make dozens of AI calls a day. The plan line changes only when what it is about changes."""
    assert replan(demo, DEMO_CONFIG, clock, tracking_sources=TRACKING, force=True)
    changes = 0
    for _ in range(3):
        clock.advance(timedelta(minutes=15))
        changes += replan(demo, DEMO_CONFIG, clock, tracking_sources=TRACKING)
    assert changes == 0


def test_a_planning_failure_never_blocks_deadline_alerts(
    demo: sqlite3.Connection, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus.notify.router import Router
    from argus.reminders import engine as engine_module
    from argus.reminders.engine import ReminderEngine
    from argus.reminders.rules import Rule, candidates

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("planner bug")

    monkeypatch.setattr(engine_module, "compute", broken)
    clock.set(local_datetime(to_local(clock.now()).date(), 22))  # Homework 3 is due at 11:30 pm
    engine = ReminderEngine(
        demo, DEMO_CONFIG, clock, Router([]), caps=derive(DEMO_CONFIG, demo_secrets())
    )
    snap = engine.snapshot(clock.now())
    assert snap.window is None and snap.at_risk == frozenset()
    assert any(r.rule is Rule.DUE_3H for r in candidates(snap))


def test_a_failing_morning_brief_waits_instead_of_breaking_the_tick(
    demo: sqlite3.Connection, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus.notify.router import Router
    from argus.reminders import engine as engine_module
    from argus.reminders.engine import ReminderEngine

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("brief bug")

    monkeypatch.setattr(engine_module, "today_text", broken)
    clock.set(local_datetime(to_local(clock.now()).date(), 9))  # after the 8 am brief time
    engine = ReminderEngine(
        demo, DEMO_CONFIG, clock, Router([]), caps=derive(DEMO_CONFIG, demo_secrets())
    )
    assert engine.snapshot(clock.now()).brief is None
