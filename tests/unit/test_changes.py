"""Summarizing the item change log into one entry per item since a point in time (D073)."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.domain.changes import ChangeEvent, ChangeKind, NetChange, summarize_changes

T = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
D1 = datetime(2026, 11, 10, 4, 59, tzinfo=UTC)
D2 = datetime(2026, 11, 12, 4, 59, tzinfo=UTC)


def ev(
    item_id: int,
    change: ChangeKind,
    *,
    old: datetime | None = None,
    new: datetime | None = None,
    minutes: int = 0,
) -> ChangeEvent:
    return ChangeEvent(item_id, change, old, new, T + timedelta(minutes=minutes))


def test_new_item() -> None:
    out = summarize_changes([ev(1, ChangeKind.NEW, new=D1)], {1: D1})
    assert out == {1: NetChange("new", None)}


def test_new_then_moved_is_still_new() -> None:
    events = [ev(1, ChangeKind.NEW, new=D1), ev(1, ChangeKind.DUE, old=D1, new=D2, minutes=5)]
    assert summarize_changes(events, {1: D2}) == {1: NetChange("new", None)}


def test_due_change_reports_the_date_before_the_window() -> None:
    events = [
        ev(1, ChangeKind.DUE, old=D1, new=D2),
        ev(1, ChangeKind.DUE, old=D2, new=D2 + timedelta(days=1), minutes=5),
    ]
    out = summarize_changes(events, {1: D2 + timedelta(days=1)})
    assert out == {1: NetChange("changed", D1)}


def test_due_changed_and_changed_back_is_omitted() -> None:
    events = [
        ev(1, ChangeKind.DUE, old=D1, new=D2),
        ev(1, ChangeKind.DUE, old=D2, new=D1, minutes=5),
    ]
    assert summarize_changes(events, {1: D1}) == {}


def test_date_added_to_an_undated_item_is_a_change() -> None:
    out = summarize_changes([ev(1, ChangeKind.DUE, old=None, new=D1)], {1: D1})
    assert out == {1: NetChange("changed", None)}


def test_removed() -> None:
    out = summarize_changes([ev(1, ChangeKind.REMOVED, old=D1, new=D1)], {1: D1})
    assert out == {1: NetChange("removed", None)}


def test_added_and_removed_inside_the_window_is_omitted() -> None:
    events = [ev(1, ChangeKind.NEW, new=D1), ev(1, ChangeKind.REMOVED, old=D1, new=D1, minutes=5)]
    assert summarize_changes(events, {1: D1}) == {}


def test_restored_after_being_gone_at_since_is_new() -> None:
    assert summarize_changes([ev(1, ChangeKind.RESTORED, new=D1)], {1: D1}) == {
        1: NetChange("new", None)
    }


def test_removed_then_restored_with_a_new_date_is_a_change() -> None:
    events = [
        ev(1, ChangeKind.REMOVED, old=D1, new=D1),
        ev(1, ChangeKind.RESTORED, new=D2, minutes=5),
    ]
    assert summarize_changes(events, {1: D2}) == {1: NetChange("changed", D1)}


def test_removed_then_restored_unchanged_is_omitted() -> None:
    events = [
        ev(1, ChangeKind.REMOVED, old=D1, new=D1),
        ev(1, ChangeKind.RESTORED, new=D1, minutes=5),
    ]
    assert summarize_changes(events, {1: D1}) == {}


def test_events_are_ordered_by_time_not_input_order() -> None:
    events = [ev(1, ChangeKind.REMOVED, old=D2, new=D2, minutes=5), ev(1, ChangeKind.NEW, new=D2)]
    assert summarize_changes(events, {1: D2}) == {}


def test_items_are_independent() -> None:
    events = [ev(1, ChangeKind.NEW, new=D1), ev(2, ChangeKind.REMOVED, old=D2, new=D2)]
    assert summarize_changes(events, {1: D1, 2: D2}) == {
        1: NetChange("new", None),
        2: NetChange("removed", None),
    }


def test_an_item_missing_from_current_dues_counts_as_undated() -> None:
    out = summarize_changes([ev(1, ChangeKind.DUE, old=D1, new=None)], {})
    assert out == {1: NetChange("changed", D1)}


def test_change_kind_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="nope"):
        ChangeKind("nope")


_kinds = st.sampled_from(list(ChangeKind))
_dates = st.one_of(st.none(), st.sampled_from([D1, D2]))


@given(
    st.lists(st.tuples(st.integers(1, 4), _kinds, _dates, _dates, st.integers(0, 60)), max_size=30)
)
def test_one_entry_per_item_and_only_items_with_events(
    raw: list[tuple[int, ChangeKind, datetime | None, datetime | None, int]],
) -> None:
    events = [ChangeEvent(i, k, o, n, T + timedelta(minutes=m)) for i, k, o, n, m in raw]
    out = summarize_changes(events, dict.fromkeys(range(1, 5), D1))
    assert set(out) <= {e.item_id for e in events}
    for change in out.values():
        assert change.status in ("new", "changed", "removed")
        if change.status != "changed":
            assert change.old_due is None
