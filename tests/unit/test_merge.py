from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from argus.domain.merge import MATCH_WINDOW, MergeGroup, merge_sources
from argus.domain.models import ItemKind, Source, SourceItem, SourceKey
from argus.domain.titles import normalize_title

T0 = datetime(2026, 10, 23, 3, 59, tzinfo=UTC)  # Thu 23:59 EDT


def rec(
    source: Source,
    ext: str,
    title: str = "HW 3",
    due: datetime | None = T0,
    course: str = "CMSC351",
) -> SourceItem:
    return SourceItem(
        source=source,
        external_id=ext,
        course_code=course,
        title=title,
        kind=ItemKind.ASSIGNMENT,
        due_at=due,
        raw_hash="h",
    )


def partition(groups: list[MergeGroup]) -> set[frozenset[SourceKey]]:
    return {frozenset(g.keys) for g in groups}


CI, CA, GS = Source.CANVAS_ICAL, Source.CANVAS_API, Source.GRADESCOPE


# ---------------------------------------------------------------- examples


def test_canvas_and_gradescope_same_title_within_window_merge() -> None:
    a = rec(CI, "100", "Homework 3")
    b = rec(GS, "g1", "HW3", T0 + timedelta(minutes=59))
    assert partition(merge_sources([a, b], {})) == {frozenset({a.key, b.key})}


def test_exactly_sixty_minutes_merges_but_sixty_one_does_not() -> None:
    a = rec(CI, "100")
    assert len(merge_sources([a, rec(GS, "g1", due=T0 + MATCH_WINDOW)], {})) == 1
    assert (
        len(merge_sources([a, rec(GS, "g1", due=T0 + MATCH_WINDOW + timedelta(minutes=1))], {}))
        == 2
    )


@pytest.mark.parametrize(
    "other",
    [
        rec(GS, "g1", course="MATH240"),
        rec(GS, "g1", title="HW 4"),
        rec(GS, "g1", due=None),
    ],
    ids=["other-course", "other-title", "no-due"],
)
def test_no_merge_when_a_rule_fails(other: SourceItem) -> None:
    assert len(merge_sources([rec(CI, "100"), other], {})) == 2


def test_same_family_never_fuzzy_merges() -> None:
    groups = merge_sources([rec(GS, "g1"), rec(GS, "g2")], {})
    assert len(groups) == 2


def test_canvas_ical_and_api_join_by_assignment_id_regardless_of_title() -> None:
    a = rec(CI, "100", "Homework 3")
    b = rec(CA, "100", "Homework 3 (revised)", T0 + timedelta(days=2))
    assert partition(merge_sources([a, b], {})) == {frozenset({a.key, b.key})}


def test_three_way_canvas_pair_plus_gradescope() -> None:
    a, b, c = rec(CI, "100"), rec(CA, "100"), rec(GS, "g1", due=T0 - timedelta(minutes=30))
    assert partition(merge_sources([a, b, c], {})) == {frozenset({a.key, b.key, c.key})}


def test_closest_due_wins_when_two_candidates() -> None:
    canvas_a = rec(CI, "100", due=T0)
    canvas_b = rec(CI, "101", due=T0 + timedelta(minutes=50))
    gs = rec(GS, "g1", due=T0 + timedelta(minutes=45))
    groups = merge_sources([canvas_a, canvas_b, gs], {})
    assert frozenset({canvas_b.key, gs.key}) in partition(groups)
    assert frozenset({canvas_a.key}) in partition(groups)


def test_links_are_sticky_even_when_dates_diverge() -> None:
    a = rec(CI, "100", due=T0)
    b = rec(GS, "g1", due=T0 + timedelta(days=3))
    groups = merge_sources([a, b], {a.key: 7, b.key: 7})
    assert groups == [MergeGroup(item_id=7, keys=tuple(sorted([a.key, b.key], key=str)))]


def test_existing_items_are_never_fused() -> None:
    a, b = rec(CI, "100"), rec(GS, "g1")
    groups = merge_sources([a, b], {a.key: 1, b.key: 2})
    assert [g.item_id for g in groups] == [1, 2]


def test_new_gradescope_record_attaches_to_existing_canvas_item() -> None:
    a, b = rec(CI, "100"), rec(GS, "g1")
    groups = merge_sources([a, b], {a.key: 5})
    assert groups == [MergeGroup(item_id=5, keys=tuple(sorted([a.key, b.key], key=str)))]


def test_new_canvas_api_record_attaches_by_identity_to_existing_item() -> None:
    a, b = rec(CI, "100"), rec(CA, "100", title="Renamed", due=None)
    groups = merge_sources([a, b], {a.key: 5})
    assert [g.item_id for g in groups] == [5]


def test_existing_item_with_both_families_takes_no_more_gradescope() -> None:
    a, b, c = rec(CI, "100"), rec(GS, "g1"), rec(GS, "g2")
    groups = merge_sources([a, b, c], {a.key: 1, b.key: 1})
    assert partition(groups) == {frozenset({a.key, b.key}), frozenset({c.key})}


def test_manual_and_finding_records_never_fuzzy_merge() -> None:
    a = rec(CI, "100")
    m = rec(Source.MANUAL, "m1")
    f = rec(Source.FINDING, "f1")
    assert len(merge_sources([a, m, f], {})) == 3


def test_duplicate_keys_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        merge_sources([rec(CI, "100"), rec(CI, "100")], {})


def test_output_order_is_existing_by_id_then_new() -> None:
    a, b, c = rec(CI, "1", title="A"), rec(CI, "2", title="B"), rec(CI, "3", title="C")
    groups = merge_sources([c, a, b], {b.key: 9, c.key: 4})
    assert [g.item_id for g in groups] == [4, 9, None]


# ---------------------------------------------------------------- properties

TITLES = ["HW 1", "Homework 1", "hw01", "Quiz 2", "Project"]
records_st = st.lists(
    st.builds(
        rec,
        source=st.sampled_from([CI, CA, GS, Source.MANUAL]),
        ext=st.sampled_from([str(i) for i in range(6)]),
        title=st.sampled_from(TITLES),
        due=st.one_of(st.none(), st.integers(0, 12).map(lambda k: T0 + timedelta(minutes=20 * k))),
        course=st.sampled_from(["CMSC351", "MATH240"]),
    ),
    max_size=12,
).map(lambda rs: list({r.key: r for r in rs}.values()))


def _links_from(groups: list[MergeGroup]) -> dict[SourceKey, int]:
    links: dict[SourceKey, int] = {}
    next_id = 1000
    for g in groups:
        item_id = g.item_id
        if item_id is None:
            next_id += 1
            item_id = next_id
        for k in g.keys:
            links[k] = item_id
    return links


@settings(max_examples=300)
@given(records_st, st.randoms(use_true_random=False))
def test_partition_is_order_independent(records: list[SourceItem], rnd: object) -> None:
    shuffled = list(records)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert partition(merge_sources(records, {})) == partition(merge_sources(shuffled, {}))


@settings(max_examples=300)
@given(records_st)
def test_every_record_in_exactly_one_group(records: list[SourceItem]) -> None:
    groups = merge_sources(records, {})
    keys = [k for g in groups for k in g.keys]
    assert sorted(keys, key=str) == sorted((r.key for r in records), key=str)


@settings(max_examples=300)
@given(records_st)
def test_merge_is_idempotent_under_its_own_links(records: list[SourceItem]) -> None:
    first = merge_sources(records, {})
    links = _links_from(first)
    second = merge_sources(records, links)
    assert partition(second) == partition(first)
    assert all(g.item_id is not None for g in second)
    assert merge_sources(records, links) == second


@settings(max_examples=300)
@given(records_st)
def test_fuzzy_pairs_satisfy_the_rule(records: list[SourceItem]) -> None:
    by_key = {r.key: r for r in records}
    for g in merge_sources(records, {}):
        members = [by_key[k] for k in g.keys]
        families = [m.source.family for m in members]
        assert families.count("gradescope") <= 1
        assert {"manual"} & set(families) == set() or len(members) == 1
        canvas = [m for m in members if m.source.family == "canvas"]
        assert len({m.external_id for m in canvas}) <= 1
        gs = [m for m in members if m.source.family == "gradescope"]
        if canvas and gs:
            g0 = gs[0]
            assert any(
                c.course_code == g0.course_code
                and normalize_title(c.title) == normalize_title(g0.title)
                and c.due_at is not None
                and g0.due_at is not None
                and abs(c.due_at - g0.due_at) <= MATCH_WINDOW
                for c in canvas
            )


@settings(max_examples=300)
@given(records_st, st.data())
def test_links_are_respected(records: list[SourceItem], data: st.DataObject) -> None:
    links = {
        r.key: data.draw(st.integers(1, 3), label=str(r.key))
        for r in records
        if data.draw(st.booleans(), label=f"link {r.key}")
    }
    groups = merge_sources(records, links)
    group_of = {k: i for i, g in enumerate(groups) for k in g.keys}
    for k1, id1 in links.items():
        for k2, id2 in links.items():
            assert (group_of[k1] == group_of[k2]) == (id1 == id2)
    for g in groups:
        linked_ids = {links[k] for k in g.keys if k in links}
        assert linked_ids == ({g.item_id} if g.item_id is not None else set())
