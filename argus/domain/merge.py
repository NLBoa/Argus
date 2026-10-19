"""Merging source records into items, and resolving an item's fields from its records.

`merge_sources` is pure and deterministic. Its rules:

1. **Links are sticky.** A record already linked to an item stays with it, even if its date
   later diverges. Two existing items are never fused automatically.
2. **Identity.** Canvas iCal and Canvas API records with the same assignment id are the
   same Canvas object. They always join (an unlinked one joins a linked one).
3. **Fuzzy match** (the spec rule). A Canvas-family unit and a Gradescope unit join when the
   course matches, the normalized titles match, and the due times are within `MATCH_WINDOW`.
   A unit holds at most one Gradescope record. Candidates are paired greedily by smallest gap,
   with total-order tie-breaks, so the result does not depend on input order.
4. Manual and finding records never fuzzy-match here. Findings are matched to items in
   `domain/findings.py` (Phase 4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from argus.clock import to_local
from argus.domain.models import ResolvedItem, Source, SourceItem, SourceKey
from argus.domain.titles import normalize_title

MATCH_WINDOW = timedelta(minutes=60)
CONFLICT_TOLERANCE = timedelta(minutes=5)
_FUZZY_FAMILIES = frozenset({"canvas", "gradescope"})
# Field precedence when records disagree (first wins). Due dates use their own rule.
PRECEDENCE: tuple[Source, ...] = (
    Source.CANVAS_API,
    Source.CANVAS_ICAL,
    Source.GRADESCOPE,
    Source.MANUAL,
    Source.FINDING,
)


@dataclass(frozen=True, slots=True)
class MergeGroup:
    item_id: int | None  # None: a new item to create
    keys: tuple[SourceKey, ...]  # sorted by str(key)


@dataclass
class _Unit:
    item_id: int | None
    records: list[SourceItem] = field(default_factory=list)

    @property
    def families(self) -> set[str]:
        return {r.source.family for r in self.records}

    @property
    def sort_key(self) -> tuple[str, ...]:
        return tuple(sorted(str(r.key) for r in self.records))


def _identity(r: SourceItem) -> tuple[str, str]:
    if r.source.family == "canvas":
        return ("canvas", r.external_id)
    return (r.source.value, r.external_id)


def _gap(a: _Unit, b: _Unit) -> timedelta | None:
    """Smallest due-time gap between eligible members, or None if they cannot match."""
    best: timedelta | None = None
    for x in a.records:
        for y in b.records:
            if x.source.family == y.source.family or x.due_at is None or y.due_at is None:
                continue
            if x.course_code != y.course_code:
                continue
            if normalize_title(x.title) != normalize_title(y.title):
                continue
            d = abs(x.due_at - y.due_at)
            if d <= MATCH_WINDOW and (best is None or d < best):
                best = d
    return best


def _build_units(
    recs: list[SourceItem], links: Mapping[SourceKey, int]
) -> tuple[dict[int, _Unit], dict[tuple[str, str], _Unit]]:
    """Linked records form existing units; unlinked ones join by identity or form new units."""
    existing: dict[int, _Unit] = {}
    identity_owner: dict[tuple[str, str], int] = {}
    unlinked: dict[tuple[str, str], _Unit] = {}
    for r in recs:
        item_id = links.get(r.key)
        if item_id is not None:
            existing.setdefault(item_id, _Unit(item_id)).records.append(r)
            identity_owner.setdefault(_identity(r), item_id)
    for r in recs:
        if r.key in links:
            continue
        owner = identity_owner.get(_identity(r))
        if owner is not None:
            existing[owner].records.append(r)
        else:
            unlinked.setdefault(_identity(r), _Unit(None)).records.append(r)
    return existing, unlinked


def _fuzzy_join(existing: list[_Unit], fresh: list[_Unit]) -> set[int]:
    """Greedily pair canvas-side and gradescope-side units by smallest gap.

    Mutates the surviving unit of each pair. Returns the ids of absorbed fresh units.
    """
    candidates: list[tuple[timedelta, tuple[str, ...], tuple[str, ...], _Unit, _Unit]] = []
    for u in fresh:
        for v in existing + fresh:
            if v is u or not v.families <= _FUZZY_FAMILIES or u.families & v.families:
                continue
            if v.item_id is None and u.sort_key > v.sort_key:
                continue  # consider each fresh-fresh pair once
            gap = _gap(u, v)
            if gap is not None:
                a, b = sorted((u, v), key=lambda x: x.sort_key)
                candidates.append((gap, a.sort_key, b.sort_key, a, b))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    used: set[int] = set()
    absorbed: set[int] = set()
    for _, _, _, a, b in candidates:
        if id(a) in used or id(b) in used:
            continue
        target, other = (a, b) if a.item_id is not None else (b, a)
        target.records.extend(other.records)
        used.update({id(a), id(b)})
        absorbed.add(id(other))
    return absorbed


def _group(u: _Unit) -> MergeGroup:
    return MergeGroup(item_id=u.item_id, keys=tuple(sorted((r.key for r in u.records), key=str)))


def merge_sources(
    records: Iterable[SourceItem], links: Mapping[SourceKey, int]
) -> list[MergeGroup]:
    recs = sorted(records, key=lambda r: str(r.key))
    keys = [r.key for r in recs]
    if len(set(keys)) != len(keys):
        dupes = sorted({str(k) for k in keys if keys.count(k) > 1})
        raise ValueError(f"duplicate source records {dupes}")

    existing, unlinked = _build_units(recs, links)
    fresh = [u for u in unlinked.values() if u.families <= _FUZZY_FAMILIES]
    # Existing units only ever pair with fresh ones, so two existing items never fuse.
    absorbed = _fuzzy_join(list(existing.values()), fresh)

    groups = [_group(u) for u in sorted(existing.values(), key=lambda u: u.item_id or 0)]
    new_groups = [_group(u) for u in unlinked.values() if id(u) not in absorbed]
    groups += sorted(new_groups, key=lambda g: tuple(str(k) for k in g.keys))
    return groups


# ---------------------------------------------------------------- field resolution


def _rank(source: Source) -> int:
    return PRECEDENCE.index(source) if source in PRECEDENCE else len(PRECEDENCE)


def _tri_state(values: Iterable[bool | None]) -> bool | None:
    vals = [v for v in values if v is not None]
    if any(vals):
        return True
    return False if vals else None


def _differs(r: SourceItem, due_at: datetime) -> bool:
    """A date-only record (e.g. "the quiz is Friday") agrees with any time on that local day."""
    assert r.due_at is not None
    if r.all_day and not r.source.is_official:
        return to_local(r.due_at).date() != to_local(due_at).date()
    return abs(r.due_at - due_at) > CONFLICT_TOLERANCE


def resolve_item(records: Sequence[SourceItem]) -> ResolvedItem:
    """Derive an item's fields from its source records.

    - Due date: the earliest official due date (Canvas, Gradescope), so reminders are never
      late. Without an official date: manual, then finding. Any record whose due date differs
      from the chosen one by more than `CONFLICT_TOLERANCE` puts the item in conflict.
    - Kind: the most serious kind any record reports (exam > quiz > assignment > event).
    - Submitted / graded: True if any record says so, False if any says not, else unknown.
    - Other fields: first non-empty value in `PRECEDENCE` order.
    """
    if not records:
        raise ValueError("cannot resolve an item with no records")
    ordered = sorted(records, key=lambda r: (_rank(r.source), str(r.key)))

    dated = [r for r in ordered if r.due_at is not None]
    official = [r for r in dated if r.source.is_official]
    chosen: SourceItem | None
    if official:
        chosen = min(official, key=lambda r: (r.due_at, _rank(r.source)))
    else:
        chosen = dated[0] if dated else None
    due_at = chosen.due_at if chosen else None

    conflict: tuple[tuple[Source, datetime], ...] = ()
    if due_at is not None:
        disagree = [r for r in dated if r.due_at and _differs(r, due_at)]
        if disagree:
            conflict = tuple(
                sorted({(r.source, r.due_at) for r in dated if r.due_at}, key=lambda p: p[1])
            )

    late = [r.late_due_at for r in ordered if r.late_due_at is not None]
    late_due_at = max(late) if late else None
    if late_due_at is not None and due_at is not None and late_due_at <= due_at:
        late_due_at = None
    releases = [r.release_at for r in ordered if r.release_at is not None]

    def first[T](attr: str) -> T | None:
        for r in ordered:
            value = getattr(r, attr)
            if value not in (None, ""):
                return value  # type: ignore[no-any-return]
        return None

    return ResolvedItem(
        course_code=ordered[0].course_code,
        title=first("title") or ordered[0].title,
        kind=max((r.kind for r in ordered), key=lambda k: k.weight),
        due_at=due_at,
        due_source=chosen.source if chosen else None,
        release_at=min(releases) if releases else None,
        late_due_at=late_due_at,
        points=first("points"),
        submitted=_tri_state(r.submitted for r in ordered),
        graded=_tri_state(r.graded for r in ordered),
        description=first("description"),
        date_conflict=conflict,
    )


def group_records(
    groups: Sequence[MergeGroup], records: Iterable[SourceItem]
) -> list[tuple[MergeGroup, list[SourceItem]]]:
    by_key = {r.key: r for r in records}
    out: list[tuple[MergeGroup, list[SourceItem]]] = []
    for g in groups:
        out.append((g, [by_key[k] for k in g.keys]))
    return out
