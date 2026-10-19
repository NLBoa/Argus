from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from argus.domain.models import Item, ItemKind, ItemStatus
from argus.reminders.rules import (
    Reminder,
    Rule,
    SessionRef,
    Snapshot,
    SourceAlertState,
    candidates,
    due_now,
    due_soon,
    item_key,
    new_items,
    system_alerts,
)
from argus.reminders.text import SEGMENT, gsm_length

NOW = datetime(2026, 10, 19, 13, tzinfo=UTC)  # Mon 09:00 EDT


def item(i: int, due: timedelta | None, **kw: object) -> Item:
    base: dict[str, object] = {
        "id": i,
        "course_code": "CMSC351",
        "kind": ItemKind.ASSIGNMENT,
        "title": f"Homework {i}",
        "due_at": NOW + due if due is not None else None,
        "status": ItemStatus.UPCOMING,
        "created_at": NOW - timedelta(days=10),
    }
    base.update(kw)
    return Item(**base)  # type: ignore[arg-type]


def snap(*items: Item, sent: set[str] | None = None, **kw: object) -> Snapshot:
    return Snapshot(now=NOW, items=items, sent_keys=frozenset(sent or ()), **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- due 24h / 3h


@pytest.mark.parametrize(
    ("due", "rule"),
    [
        (timedelta(hours=24), Rule.DUE_24H),
        (timedelta(hours=3, minutes=1), Rule.DUE_24H),
        (timedelta(hours=3), Rule.DUE_3H),
        (timedelta(minutes=1), Rule.DUE_3H),
    ],
)
def test_deadline_windows(due: timedelta, rule: Rule) -> None:
    [r] = candidates(snap(item(1, due)))
    assert r.rule == rule
    assert r.keys == (f"1:{rule.value}:{(NOW + due).strftime('%Y-%m-%dT%H:%M:%SZ')}",)
    assert r.item_ids == (1,)
    assert gsm_length(r.sms) <= SEGMENT


@pytest.mark.parametrize(
    "due", [timedelta(hours=24, seconds=1), timedelta(0), timedelta(hours=-1), None]
)
def test_outside_windows_nothing(due: timedelta | None) -> None:
    assert candidates(snap(item(1, due))) == []


def test_three_hour_warning_bypasses_quiet_and_cap() -> None:
    [r] = due_now(snap(item(1, timedelta(hours=2))))
    assert r.bypass_quiet
    assert not r.counts_toward_cap
    assert r.priority == 5
    assert r.title == "Due in 2h"
    assert r.expires_at == NOW + timedelta(hours=2)
    [late] = due_now(snap(item(1, timedelta(minutes=20))))
    assert late.title == "Due within the hour"


def test_24h_titles_and_expiry() -> None:
    [tomorrow] = due_soon(snap(item(1, timedelta(hours=20))))
    assert tomorrow.title == "Due tomorrow"  # Tue 5am local
    [late_today] = due_soon(snap(item(1, timedelta(hours=14, minutes=59))))
    assert late_today.title == "Due today"  # Mon 11:59 pm local: same day though 15h away
    assert tomorrow.expires_at == NOW + timedelta(hours=17)
    assert not tomorrow.bypass_quiet
    [today] = due_soon(snap(item(1, timedelta(hours=8))))
    assert today.title == "Due today"
    assert "Mon 5pm" in today.body
    assert "Reply DONE 1" in today.body


@pytest.mark.parametrize(
    "it",
    [
        item(1, timedelta(hours=2), status=ItemStatus.SUBMITTED),
        item(1, timedelta(hours=2), status=ItemStatus.GRADED),
        item(1, timedelta(hours=2), kind=ItemKind.EXAM),
        item(1, timedelta(hours=2), kind=ItemKind.EVENT),
    ],
)
def test_closed_or_unsubmittable_items_are_quiet(it: Item) -> None:
    assert candidates(snap(it)) == []


def test_in_progress_items_still_remind() -> None:
    assert [
        r.rule for r in candidates(snap(item(1, timedelta(hours=2), status=ItemStatus.IN_PROGRESS)))
    ] == [Rule.DUE_3H]


def test_sent_keys_suppress_and_moved_due_rearms() -> None:
    it = item(1, timedelta(hours=2))
    sent = {item_key(it, Rule.DUE_3H)}
    assert candidates(snap(it, sent=sent)) == []
    moved = item(1, timedelta(hours=2, minutes=30))
    assert [r.rule for r in candidates(snap(moved, sent=sent))] == [Rule.DUE_3H]


def test_long_titles_fit_one_segment() -> None:
    it = item(1, timedelta(hours=10), title="An extraordinarily long assignment title " * 5)
    [r] = candidates(snap(it))
    assert gsm_length(r.sms) <= SEGMENT


# ---------------------------------------------------------------- new items


def test_new_items_batched_with_numbers_and_per_item_keys() -> None:
    fresh = [item(i, timedelta(days=i), created_at=NOW - timedelta(hours=1)) for i in range(1, 6)]
    [r] = new_items(snap(*fresh))
    assert r.title == "5 new items"
    assert r.body.startswith("[1] CMSC351 Homework 1 (Tue 9am); [2] CMSC351 Homework 2")
    assert r.body.endswith("+2 more")
    assert r.item_ids == (1, 2, 3)
    assert len(r.keys) == 5
    assert r.expires_at == NOW + timedelta(days=1)


def test_new_item_filters() -> None:
    created = NOW - timedelta(hours=1)
    ok = item(1, timedelta(days=2), created_at=created)
    far = item(2, timedelta(days=8), created_at=created)
    stale = item(3, timedelta(days=2), created_at=NOW - timedelta(days=2))
    sent = item(4, timedelta(days=2), created_at=created)
    [r] = new_items(snap(ok, far, stale, sent, sent={item_key(sent, Rule.NEW_ITEM)}))
    assert r.title == "New item"
    assert r.item_ids == (1,)


def test_first_import_is_not_new() -> None:
    created = NOW - timedelta(hours=1)
    it = item(1, timedelta(days=2), created_at=created)
    assert new_items(snap(it, bootstrap_until=created + timedelta(minutes=5))) == []
    assert new_items(snap(it, bootstrap_until=created - timedelta(minutes=5))) != []


# ---------------------------------------------------------------- system alerts


def test_system_alert_after_three_failures_once_per_streak() -> None:
    since = NOW - timedelta(hours=1)
    states = {
        "gradescope": SourceAlertState("gradescope", 3, since, "boom"),
        "piazza": SourceAlertState("piazza", 2, since),
    }
    [r] = system_alerts(snap(sources=states))
    assert r.title == "Gradescope sync failing"
    assert r.rule == Rule.SYSTEM_ALERT
    assert system_alerts(snap(sources=states, sent=set(r.keys))) == []
    new_streak = {"gradescope": SourceAlertState("gradescope", 4, NOW, None)}
    assert system_alerts(snap(sources=new_streak, sent=set(r.keys))) != []


def test_candidates_most_urgent_first() -> None:
    rs = candidates(snap(item(1, timedelta(hours=20)), item(2, timedelta(hours=2))))
    assert [r.rule for r in rs] == [Rule.DUE_3H, Rule.DUE_24H]


def test_rule_labels() -> None:
    assert all(rule.label for rule in Rule)


# ---------------------------------------------------------------- property: no duplicates


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.integers(min_value=-60, max_value=60 * 24 * 7), min_size=1, max_size=6),
    st.integers(min_value=5, max_value=60),
)
def test_simulated_week_never_duplicates(due_minutes: list[int], step: int) -> None:
    items = [item(i, timedelta(minutes=m)) for i, m in enumerate(due_minutes, start=1)]
    sent: set[str] = set()
    sent_log: list[Reminder] = []
    t = NOW
    while t < NOW + timedelta(days=8):
        s = Snapshot(now=t, items=items, sent_keys=frozenset(sent))
        for r in candidates(s):
            assert not (set(r.keys) & sent), f"duplicate {r.keys}"
            sent.update(r.keys)
            sent_log.append(r)
        t += timedelta(minutes=step)
    per_item_rule = [
        (r.item_ids, r.rule) for r in sent_log if r.rule in (Rule.DUE_3H, Rule.DUE_24H)
    ]
    assert len(per_item_rule) == len(set(per_item_rule))
    # Every item due later than now got its 3-hour warning.
    for it in items:
        if it.due_at and it.due_at > NOW:
            assert item_key(it, Rule.DUE_3H) in sent


# ---------------------------------------------------------------- at risk, start nudges, AI budget


def test_at_risk_is_immediate_and_uncapped() -> None:
    it = item(1, timedelta(days=2))
    [r] = candidates(snap(it, at_risk=frozenset({1})))
    assert r.rule == Rule.AT_RISK
    assert not r.counts_toward_cap and not r.bypass_quiet
    assert "EST 1" in r.body and gsm_length(r.sms) <= SEGMENT
    assert candidates(snap(it, at_risk=frozenset({1}), sent=set(r.keys))) == []
    assert candidates(snap(item(1, timedelta(hours=-1)), at_risk=frozenset({1}))) == []


def test_start_nudge_fires_in_window_once() -> None:
    it = item(1, timedelta(days=2))
    s = SessionRef(1, NOW - timedelta(minutes=3), NOW + timedelta(minutes=87))
    [r] = candidates(snap(it, sessions=[s]))
    assert r.rule == Rule.START_NUDGE and r.title == "Time to start"
    assert "until 10:27am" in r.body
    assert r.expires_at == s.start + timedelta(minutes=10)
    assert candidates(snap(it, sessions=[s], sent=set(r.keys))) == []
    late = SessionRef(1, NOW - timedelta(minutes=11), NOW + timedelta(minutes=60))
    assert candidates(snap(it, sessions=[late])) == []
    done = item(1, timedelta(days=2), status=ItemStatus.SUBMITTED)
    assert candidates(snap(done, sessions=[s])) == []


def test_ai_budget_alert_once_per_month() -> None:
    [r] = candidates(snap(ai_budget_month="2026-10"))
    assert r.rule == Rule.SYSTEM_ALERT and r.keys == ("system:ai_budget:2026-10",)
    assert candidates(snap(ai_budget_month="2026-10", sent=set(r.keys))) == []
