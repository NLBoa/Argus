from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from argus.domain.merge import group_records, merge_sources, resolve_item
from argus.domain.models import ItemKind, Source, SourceItem

T0 = datetime(2026, 10, 23, 3, 59, tzinfo=UTC)


def rec(source: Source, ext: str = "1", **kw: object) -> SourceItem:
    base = SourceItem(
        source=source,
        external_id=ext,
        course_code="CMSC351",
        title="HW 3",
        kind=ItemKind.ASSIGNMENT,
        due_at=T0,
        raw_hash="h",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_single_record() -> None:
    r = resolve_item([rec(Source.CANVAS_ICAL, points=10.0)])
    assert r.due_at == T0
    assert r.due_source == Source.CANVAS_ICAL
    assert r.points == 10.0
    assert r.submitted is None
    assert r.date_conflict == ()


def test_earliest_official_due_wins_and_conflict_flagged() -> None:
    later = T0 + timedelta(minutes=30)
    r = resolve_item(
        [rec(Source.CANVAS_ICAL, due_at=later), rec(Source.GRADESCOPE, "g", due_at=T0)]
    )
    assert r.due_at == T0
    assert r.due_source == Source.GRADESCOPE
    assert r.date_conflict == ((Source.GRADESCOPE, T0), (Source.CANVAS_ICAL, later))


def test_small_difference_is_not_a_conflict() -> None:
    r = resolve_item(
        [rec(Source.CANVAS_ICAL), rec(Source.GRADESCOPE, "g", due_at=T0 + timedelta(minutes=1))]
    )
    assert r.date_conflict == ()


def test_official_beats_finding_and_manual_even_if_later() -> None:
    official = T0 + timedelta(days=1)
    r = resolve_item(
        [
            rec(Source.FINDING, "f", due_at=T0),
            rec(Source.MANUAL, "m", due_at=T0),
            rec(Source.CANVAS_ICAL, due_at=official),
        ]
    )
    assert r.due_at == official
    assert r.due_source == Source.CANVAS_ICAL
    assert (Source.FINDING, T0) in r.date_conflict


def test_without_official_manual_beats_finding() -> None:
    r = resolve_item(
        [
            rec(Source.FINDING, "f", due_at=T0),
            rec(Source.MANUAL, "m", due_at=T0 + timedelta(hours=2)),
        ]
    )
    assert r.due_source == Source.MANUAL


def test_no_due_anywhere() -> None:
    r = resolve_item(
        [rec(Source.CANVAS_ICAL, due_at=None), rec(Source.GRADESCOPE, "g", due_at=None)]
    )
    assert r.due_at is None
    assert r.due_source is None
    assert r.date_conflict == ()


def test_field_precedence_and_aggregation() -> None:
    r = resolve_item(
        [
            rec(
                Source.GRADESCOPE,
                "g",
                title="HW3",
                points=20.0,
                submitted=True,
                graded=False,
                late_due_at=T0 + timedelta(days=2),
                release_at=T0 - timedelta(days=7),
            ),
            rec(
                Source.CANVAS_API,
                "1",
                title="Homework 3",
                points=None,
                submitted=False,
                description="Do the thing",
                release_at=T0 - timedelta(days=5),
            ),
            rec(Source.CANVAS_ICAL, "1", title="Homework 3", kind=ItemKind.QUIZ),
        ]
    )
    assert r.title == "Homework 3"
    assert r.points == 20.0
    assert r.description == "Do the thing"
    assert r.kind == ItemKind.QUIZ
    assert r.submitted is True
    assert r.graded is False
    assert r.release_at == T0 - timedelta(days=7)
    assert r.late_due_at == T0 + timedelta(days=2)


def test_late_due_before_due_is_dropped() -> None:
    r = resolve_item([rec(Source.GRADESCOPE, "g", late_due_at=T0 - timedelta(hours=1))])
    assert r.late_due_at is None


def test_empty_records_rejected() -> None:
    with pytest.raises(ValueError, match="no records"):
        resolve_item([])


def test_group_records_pairs_groups_with_records() -> None:
    a, b = rec(Source.CANVAS_ICAL), rec(Source.GRADESCOPE, "g")
    pairs = group_records(merge_sources([a, b], {}), [a, b])
    assert len(pairs) == 1
    assert {r.key for r in pairs[0][1]} == {a.key, b.key}
