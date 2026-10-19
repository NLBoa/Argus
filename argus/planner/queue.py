"""The queue (pure): what to work on next, and how a work window's time is split (D058).

Order, most important first:
1. Skipped for this window ("Not tonight") goes last, even when pinned.
2. Pinned entries, in the student's order. A "move" pin expires at the end of the window it was
   made in, so the automatic order comes back; an explicit pin lasts until the item is done.
3. Tier: overdue (still workable), at risk, creeping, exam study due now, everything else.
4. Slack (window hours to spare, rounded to minutes so float noise never reorders), then the
   due date (undated last), then work before study, then the entry id. The key is total, so the
   order never depends on input order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from argus.planner.windows import WindowSpan


class Tier(IntEnum):
    OVERDUE = 0  # past due but its late window is still open
    AT_RISK = 1
    CREEPING = 2
    STUDY = 3  # exam study time reserved in this window or the next
    OK = 4


@dataclass(frozen=True, slots=True)
class Entry:
    entry_id: str  # "item:<id>" or "study:<exam id>"
    item_id: int
    kind: str  # work | study
    due: datetime | None
    tier: Tier
    slack: float | None  # hours to spare; None for study cards
    remaining: float  # hours still needed (study: hours reserved in the window)


@dataclass(frozen=True, slots=True)
class Pref:
    entry_id: str
    pin_rank: int | None = None
    pin_kind: str = "pin"  # pin | move
    pin_until: datetime | None = None  # a move pin's expiry (the end of its window)
    skip_window: str | None = None  # "Not tonight": the window id it skips


def pinned(pref: Pref | None, now: datetime) -> bool:
    if pref is None or pref.pin_rank is None:
        return False
    return pref.pin_kind == "pin" or pref.pin_until is None or now < pref.pin_until


def _key(e: Entry, pref: Pref | None, now: datetime, window_id: str | None) -> tuple[object, ...]:
    skipped = window_id is not None and pref is not None and pref.skip_window == window_id
    pin = pinned(pref, now)
    slack = round(e.slack * 60) if e.slack is not None else float("inf")
    due = e.due.timestamp() if e.due is not None else 0.0  # undated sort last via the flag
    return (
        skipped,
        not pin,
        pref.pin_rank if pin and pref is not None else 0,
        int(e.tier),
        slack,
        e.due is None,
        due,
        e.kind != "work",
        e.entry_id,
    )


def order(
    entries: Sequence[Entry], prefs: Mapping[str, Pref], now: datetime, window_id: str | None
) -> list[Entry]:
    """The queue, in order. `window_id` is the current or next window ("tonight")."""
    return sorted(entries, key=lambda e: _key(e, prefs.get(e.entry_id), now, window_id))


def move(
    displayed: Sequence[str], entry_id: str, direction: str, before: str | None
) -> list[str] | None:
    """Move an entry one place up or down in the list as the student saw it. Returns the new
    pinned prefix (to store as move pins), or None if the move is no longer valid: the list
    changed since it was shown (`before` is not the neighbor it swaps with), or it is already
    at that end."""
    if entry_id not in displayed:
        return None
    i = displayed.index(entry_id)
    j = i - 1 if direction == "up" else i + 1
    if not 0 <= j < len(displayed) or displayed[j] != before:
        return None
    new = list(displayed)
    new[i], new[j] = new[j], new[i]
    return new[: max(i, j) + 1]


def forecast(
    entries: Sequence[Entry],
    spans: Sequence[WindowSpan],
    now: datetime,
    *,
    reserved: Mapping[str, Mapping[int, float]] | None = None,
    skips: Mapping[str, str] | None = None,
) -> dict[str, list[tuple[str, float]]]:
    """How each window's remaining time would be spent: reserved exam study first, then work
    entries in queue order, each in the earliest windows before its due time. Returns window
    id -> [(entry id, hours)]. Never gives a window more than it has left, nor an entry more
    than it needs."""
    reserved = reserved or {}
    skips = skips or {}
    plan: dict[str, list[tuple[str, float]]] = {}
    used: dict[str, float] = {}
    for s in spans:
        for exam_id, hours in sorted(reserved.get(s.window_id, {}).items()):
            room = s.hours_between(now, s.end) - used.get(s.window_id, 0.0)
            take = min(hours, room)
            if take > 1e-9:
                plan.setdefault(s.window_id, []).append((f"study:{exam_id}", take))
                used[s.window_id] = used.get(s.window_id, 0.0) + take
    for e in entries:
        if e.kind != "work":
            continue
        need = e.remaining
        for s in spans:
            if need <= 1e-9:
                break
            if skips.get(e.entry_id) == s.window_id:
                continue
            room = s.hours_between(now, s.end) - used.get(s.window_id, 0.0)
            before_due = s.hours_between(now, e.due) if e.due is not None else room
            take = min(need, room, before_due)
            if take > 1e-9:
                plan.setdefault(s.window_id, []).append((e.entry_id, take))
                used[s.window_id] = used.get(s.window_id, 0.0) + take
                need -= take
    return plan
