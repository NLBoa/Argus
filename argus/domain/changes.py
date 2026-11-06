"""The item change log, summarized per item for "what changed since T?" (D073).

`apply_merge` appends a `ChangeEvent` whenever it creates an item, moves its due date, removes
it, or brings it back. `summarize_changes` compares each item's state just before `since` with
its state now. Pure; no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


class ChangeKind(StrEnum):
    NEW = "new"  # the item was created (new_due = its due date)
    DUE = "due"  # its due date moved (old_due -> new_due)
    REMOVED = "removed"  # every source record is gone (old_due = new_due = its due date)
    RESTORED = "restored"  # a removed item came back (new_due = its due date)


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    item_id: int
    change: ChangeKind
    old_due: datetime | None
    new_due: datetime | None
    at: datetime


NetStatus = Literal["new", "changed", "removed"]


@dataclass(frozen=True, slots=True)
class NetChange:
    status: NetStatus
    old_due: datetime | None  # set only for "changed": the due date just before `since`


def summarize_changes(
    events: Iterable[ChangeEvent], current_due: Mapping[int, datetime | None]
) -> dict[int, NetChange]:
    """One net change per item from the events after `since` (the caller filters by time).

    Present before and after with the same due date, or absent before and after, nets to
    nothing and is left out. An item missing from `current_due` is treated as undated."""
    by_item: dict[int, list[ChangeEvent]] = {}
    for e in events:
        by_item.setdefault(e.item_id, []).append(e)
    out: dict[int, NetChange] = {}
    for item_id, evs in by_item.items():
        evs.sort(key=lambda e: e.at)
        first, last = evs[0], evs[-1]
        present_before = first.change not in (ChangeKind.NEW, ChangeKind.RESTORED)
        present_after = last.change != ChangeKind.REMOVED
        if present_before and present_after:
            due_before = first.old_due
            if due_before != current_due.get(item_id):
                out[item_id] = NetChange("changed", due_before)
        elif present_before:
            out[item_id] = NetChange("removed", None)
        elif present_after:
            out[item_id] = NetChange("new", None)
    return out
