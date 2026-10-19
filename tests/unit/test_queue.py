"""The queue: what to work on, in order, and how a window's time is split (D058)."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from argus.clock import local_datetime
from argus.planner.queue import Entry, Pref, Tier, forecast, move, order
from argus.planner.windows import WorkWindow, window_spans

MON = date(2026, 10, 19)
NOW = local_datetime(MON, 9)
EVENINGS = WorkWindow(frozenset({0, 1, 2, 3}), time(19), time(23))
SPANS = window_spans(
    NOW, NOW + timedelta(days=14), [EVENINGS], meetings=[], commitments=[], buffer_minutes=0
)
TONIGHT = SPANS[0].window_id


def at(day: int, hour: int = 23) -> datetime:
    return local_datetime(MON + timedelta(days=day), hour)


def work(i: int, tier: Tier, slack: float, due: datetime | None, remaining: float = 2.0) -> Entry:
    return Entry(f"item:{i}", i, "work", due, tier, slack, remaining)


def study(exam: int, hours: float = 0.75) -> Entry:
    return Entry(f"study:{exam}", exam, "study", at(9, 9), Tier.STUDY, None, hours)


def ids(entries: list[Entry]) -> list[str]:
    return [e.entry_id for e in entries]


def test_tiers_then_slack_then_due() -> None:
    entries = [
        work(1, Tier.OK, 10.0, at(5)),
        work(2, Tier.CREEPING, 1.0, at(3)),
        work(3, Tier.AT_RISK, -2.0, at(2)),
        study(9),
        work(4, Tier.OK, 3.0, at(6)),
        work(5, Tier.OVERDUE, 0.0, at(-1)),
        work(6, Tier.OK, 3.0, None),
    ]
    assert ids(order(entries, {}, NOW, TONIGHT)) == [
        "item:5",
        "item:3",
        "item:2",
        "study:9",
        "item:4",  # slack 3 before slack 10
        "item:6",  # same slack, but no due date sorts after a dated one
        "item:1",
    ]


def test_pins_come_first_in_pin_order_and_skip_beats_pin() -> None:
    entries = [
        work(1, Tier.AT_RISK, -1, at(1)),
        work(2, Tier.OK, 9, at(5)),
        work(3, Tier.OK, 9, at(6)),
    ]
    prefs = {"item:3": Pref("item:3", pin_rank=0), "item:2": Pref("item:2", pin_rank=1)}
    assert ids(order(entries, prefs, NOW, TONIGHT)) == ["item:3", "item:2", "item:1"]
    prefs["item:3"] = Pref("item:3", pin_rank=0, skip_window=TONIGHT)
    assert ids(order(entries, prefs, NOW, TONIGHT)) == ["item:2", "item:1", "item:3"]
    # The skip is for tonight only.
    assert ids(order(entries, prefs, NOW, SPANS[1].window_id))[0] == "item:3"


def test_move_pins_expire() -> None:
    entries = [work(1, Tier.OK, 1, at(1)), work(2, Tier.OK, 9, at(5))]
    moved = {"item:2": Pref("item:2", pin_rank=0, pin_kind="move", pin_until=at(0, 23))}
    assert ids(order(entries, moved, at(0, 20), TONIGHT)) == ["item:2", "item:1"]
    assert ids(order(entries, moved, at(0, 23), TONIGHT)) == ["item:1", "item:2"]


def test_move_pins_the_displayed_prefix_and_refuses_stale_moves() -> None:
    shown = ["item:1", "item:2", "item:3", "item:4"]
    assert move(shown, "item:3", "up", before="item:2") == ["item:1", "item:3", "item:2"]
    assert move(shown, "item:1", "down", before="item:2") == ["item:2", "item:1"]
    assert move(shown, "item:3", "up", before="item:1") is None  # the list changed meanwhile
    assert move(shown, "item:1", "up", before=None) is None  # already first
    assert move(shown, "item:4", "down", before=None) is None  # already last
    assert move(shown, "item:9", "up", before="item:4") is None  # not in the list


def test_forecast_reserves_study_first_then_fills_in_queue_order() -> None:
    entries = [work(1, Tier.AT_RISK, -1, at(0), 3.0), work(2, Tier.OK, 5, at(3), 6.0)]
    plan = forecast(entries, SPANS, NOW, reserved={TONIGHT: {9: 1.0}})
    assert plan[TONIGHT] == [("study:9", 1.0), ("item:1", 3.0)]
    assert plan[SPANS[1].window_id] == [("item:2", 4.0)]
    assert plan[SPANS[2].window_id] == [("item:2", 2.0)]


def test_forecast_respects_due_times_and_skips() -> None:
    due_at_9 = work(1, Tier.OK, 0, at(0, 21), 5.0)
    plan = forecast([due_at_9], SPANS, NOW)
    assert plan[TONIGHT] == [("item:1", 2.0)]  # only 7-9 pm is before it's due
    skipped = forecast([work(2, Tier.OK, 9, at(3), 2.0)], SPANS, NOW, skips={"item:2": TONIGHT})
    assert TONIGHT not in skipped and skipped[SPANS[1].window_id] == [("item:2", 2.0)]


def test_forecast_inside_a_window_uses_only_the_rest() -> None:
    plan = forecast([work(1, Tier.OK, 9, at(5), 9.0)], SPANS, at(0, 22))
    assert plan[TONIGHT] == [("item:1", 1.0)]


entry_st = st.builds(
    lambda i, tier, slack, due, rem: Entry(
        f"item:{i}",
        i,
        "work",
        None if due is None else NOW + timedelta(hours=due),
        tier,
        slack,
        rem,
    ),
    st.integers(1, 50),
    st.sampled_from(list(Tier)),
    st.floats(-20, 40).map(lambda x: round(x, 2)),
    st.one_of(st.none(), st.integers(-24, 300)),
    st.floats(0, 8),
)


@settings(max_examples=150)
@given(
    entries=st.lists(entry_st, max_size=12, unique_by=lambda e: e.entry_id),
    pins=st.lists(st.integers(1, 50), max_size=4, unique=True),
    seed=st.integers(0, 10_000),
)
def test_property_order_is_deterministic_and_total(
    entries: list[Entry], pins: list[int], seed: int
) -> None:
    prefs = {f"item:{p}": Pref(f"item:{p}", pin_rank=r) for r, p in enumerate(pins)}
    first = order(entries, prefs, NOW, TONIGHT)
    shuffled = list(entries)
    random.Random(seed).shuffle(shuffled)
    assert order(shuffled, prefs, NOW, TONIGHT) == first
    assert sorted(ids(first)) == sorted(ids(entries))


@settings(max_examples=150)
@given(
    entries=st.lists(entry_st, max_size=10, unique_by=lambda e: e.entry_id),
    later=st.integers(0, 72),
)
def test_property_forecast_never_overfills_a_window(entries: list[Entry], later: int) -> None:
    now = NOW + timedelta(hours=later)
    plan = forecast(order(entries, {}, now, None), SPANS, now)
    for span in SPANS:
        given_out = sum(h for _, h in plan.get(span.window_id, []))
        assert given_out <= span.hours_between(now, span.end) + 1e-9
    for e in entries:
        placed = sum(h for alloc in plan.values() for eid, h in alloc if eid == e.entry_id)
        assert placed <= e.remaining + 1e-9


def test_an_item_alone_needs_as_few_windows_as_its_hours_allow() -> None:
    plan = forecast([work(1, Tier.OK, 9, at(13), 10.0)], SPANS, NOW)
    named = [w for w, alloc in plan.items() if alloc]
    assert len(named) == 3  # 4 + 4 + 2 hours
    assert sum(h for alloc in plan.values() for _, h in alloc) == pytest.approx(10.0)
