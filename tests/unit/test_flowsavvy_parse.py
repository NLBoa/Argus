"""FlowSavvy parsing (D079): titles, "Done when:" lines, grouping blocks into sessions, and what a
lookup says about a session missing from the schedule."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from argus.collectors.flowsavvy import (
    classify_missing,
    fetch_window,
    group_blocks,
    parse_done_when,
    parse_title,
)
from argus.domain.models import GoalSessionGone
from tests.conftest import FIXTURES

BLOCKS = json.loads((FIXTURES / "flowsavvy" / "schedule.json").read_text())["scheduleItems"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[Projects] Reducer tests", ("Projects", "Reducer tests")),
        ("[Spanish]   Weekend session ", ("Spanish", "Weekend session")),
        ("  [Apps] 3 applications + 1 referral ask", ("Apps", "3 applications + 1 referral ask")),
        ("[CMSC351] HW3", ("CMSC351", "HW3")),
        ("Groceries", None),
        ("[ ] nothing", None),
        ("[Projects]", None),
        ("[Get good] spaces", None),
        (None, None),
    ],
)
def test_title_parsing(title: str | None, expected: tuple[str, str] | None) -> None:
    assert parse_title(title) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            "<p>Goal: X</p><p><strong>Done when:</strong> carcass glued up square</p>",
            "carcass glued up square",
        ),
        ("Goal: Talk<br>Done when: 20 new verbs", "20 new verbs"),
        ("<p>done WHEN :  it compiles &amp; runs</p>", "it compiles & runs"),
        ("<ul><li>Done when: one</li><li>Done when: two</li></ul>", "one"),
        ("<p>Goal: only a goal</p>", None),
        ("<p>Done when:</p>", None),
        (None, None),
        ("", None),
    ],
)
def test_done_when_from_notes_html(html: str | None, expected: str | None) -> None:
    assert parse_done_when(html) == expected


def test_done_when_is_capped() -> None:
    done = parse_done_when("Done when: " + "x" * 300)
    assert done is not None and len(done) == 200


def test_group_blocks() -> None:
    records = {r.external_id: r for r in group_blocks(BLOCKS, notes={}, alias={}, tags=None)}
    assert sorted(records) == ["c-hw", "r-weekend@2026-11-14", "t-late", "t-mon", "t-split"]
    mon = records["t-mon"]
    assert (mon.tag, mon.what, mon.minutes, mon.completed) == (
        "Woodwork",
        "Practice dovetails",
        60,
        True,
    )


def test_offsetless_times_are_new_york() -> None:
    records = {r.external_id: r for r in group_blocks(BLOCKS, notes={}, alias={}, tags=None)}
    assert records["t-mon"].start_at == datetime(2026, 11, 10, 0, 0, tzinfo=UTC)  # 19:00 EST
    late = records["t-late"]  # 23:30 on the fall-back day is EST: 04:30Z
    assert late.start_at == datetime(2026, 11, 2, 4, 30, tzinfo=UTC) and late.minutes == 45


def test_split_task_parts_sum_minutes() -> None:
    split = next(
        r for r in group_blocks(BLOCKS, notes={}, alias={}, tags=None) if r.external_id == "t-split"
    )
    assert split.minutes == 90
    assert split.start_at == datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
    assert split.end_at == datetime(2026, 11, 14, 18, 30, tzinfo=UTC)


def test_recurring_occurrence_key_and_alias() -> None:
    records = group_blocks(BLOCKS, notes={}, alias={"t-mon": "r-series@2026-11-09"}, tags=None)
    ids = {r.external_id for r in records}
    assert "r-weekend@2026-11-14" in ids and "r-series@2026-11-09" in ids and "t-mon" not in ids


def test_tags_filter_and_notes_attach() -> None:
    records = group_blocks(BLOCKS, notes={"t-split": "carcass glued"}, alias={}, tags={"Woodwork"})
    assert {r.tag for r in records} == {"Woodwork"}
    split = next(r for r in records if r.external_id == "t-split")
    assert split.done_when == "carcass glued"


def test_hash_changes_with_completion() -> None:
    a = group_blocks(BLOCKS[:1], notes={}, alias={}, tags=None)[0]
    b = group_blocks([{**BLOCKS[0], "completed": False}], notes={}, alias={}, tags=None)[0]
    assert a.raw_hash != b.raw_hash


@pytest.mark.parametrize(
    ("key", "status", "item", "expected"),
    [
        ("t-1", 404, None, GoalSessionGone("t-1", "deleted")),
        ("t-1", 200, {"id": "t-1", "completed": True}, GoalSessionGone("t-1", "completed")),
        ("t-1", 200, {"id": "t-1", "completed": False}, GoalSessionGone("t-1", "moved")),
        (
            "r-1@2026-11-07",
            200,
            {"id": "r-1", "completed": False, "cancelledOccurrenceDates": ["2026-11-07"]},
            GoalSessionGone("r-1@2026-11-07", "cancelled"),
        ),
        (
            "r-1@2026-11-07",
            200,
            {"id": "r-1", "completed": False, "cancelledOccurrenceDates": ["2026-11-14T00:00:00"]},
            GoalSessionGone("r-1@2026-11-07", "moved"),
        ),
        ("r-1@2026-11-07", 404, None, GoalSessionGone("r-1@2026-11-07", "deleted")),
        ("t-1", 200, "not a mapping", None),
    ],
)
def test_classify_missing(
    key: str, status: int, item: object, expected: GoalSessionGone | None
) -> None:
    assert classify_missing(key, status, item) == expected


def test_fetch_window_is_last_this_and_next_week() -> None:
    now = datetime(2026, 11, 12, 23, 40, tzinfo=UTC)  # Thu 18:40 local
    first, last, start, end = fetch_window(now)
    assert (first, last) == (date(2026, 11, 2), date(2026, 11, 22))
    assert start == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 23, 5, 0, tzinfo=UTC)


def test_group_blocks_skips_malformed_blocks() -> None:
    bad = [
        {
            "itemId": "a",
            "itemType": "task",
            "title": "[W] x",
            "startTime": "soon",
            "endTime": "later",
        },
        {"itemId": "b", "itemType": "task", "title": "[W] y"},
        {
            "itemId": "c",
            "itemType": "task",
            "title": "[W] z",
            "startTime": "2026-11-10T19:00:00",
            "endTime": "2026-11-10T20:00:00",
        },
    ]
    assert [r.external_id for r in group_blocks(bad, notes={}, alias={}, tags=None)] == ["c"]
