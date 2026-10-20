"""The work-window form: reading, checking, and describing (D057)."""

from __future__ import annotations

from datetime import time

from argus.planner.windows import WorkWindow
from argus.web.settings_form import (
    ALL_DAYS,
    WindowRow,
    check,
    describe,
    describe_days,
    read_rows,
    rows_from_windows,
)


def test_every_day_wins_over_single_days() -> None:
    form = {"every_0": ["on"], "days_0": ["2"], "start_0": ["19:00"], "end_0": ["23:00"]}
    [row] = read_rows(form, 1)
    assert row.days == ALL_DAYS and row.every_day


def test_reads_days_and_ignores_junk() -> None:
    form = {"days_0": ["0", "3", "9", "x"], "start_0": ["15:00"], "end_0": ["18:00"]}
    [row] = read_rows(form, 1)
    assert row.days == frozenset({0, 3})


def test_valid_rows_become_windows_and_blank_rows_are_ignored() -> None:
    result = check(
        [
            WindowRow(ALL_DAYS, "19:00", "23:00"),
            WindowRow(),  # an added row never filled in
            WindowRow(frozenset({6}), "22:00", "01:00"),  # past midnight is fine
        ]
    )
    assert result.ok
    assert result.windows == [
        WorkWindow(ALL_DAYS, time(19), time(23)),
        WorkWindow(frozenset({6}), time(22), time(1)),
    ]
    assert len(result.rows) == 2


def test_each_mistake_is_explained_on_its_row() -> None:
    result = check(
        [
            WindowRow(frozenset(), "19:00", "23:00"),
            WindowRow(frozenset({1}), "19:00", ""),
            WindowRow(frozenset({1}), "19:00", "19:00"),
            WindowRow(frozenset({1}), "7pm", "11pm"),
        ]
    )
    assert not result.ok
    assert [r.error for r in result.rows] == [
        "Pick at least one day.",
        "Enter a start and an end time.",
        "The end time must differ from the start.",
        "Enter a start and an end time.",
    ]
    assert result.windows == []


def test_at_most_three_windows_a_day() -> None:
    rows = [WindowRow(frozenset({1}), f"{h:02d}:00", f"{h:02d}:30") for h in (8, 10, 12, 14)]
    result = check(rows)
    assert not result.ok and result.error == "At most 3 times a day (Tue has more)."


def test_round_trip_from_saved_windows() -> None:
    saved = [
        WorkWindow(frozenset({6}), time(13), time(18)),
        WorkWindow(ALL_DAYS, time(19), time(23)),
    ]
    rows = rows_from_windows(saved)
    assert [r.start for r in rows] == ["19:00", "13:00"]  # ordered by first day, then time
    assert check(rows).windows == sorted(saved, key=lambda w: (min(w.days), w.start))


def test_descriptions_read_like_a_person_would_say_them() -> None:
    assert describe(WorkWindow(ALL_DAYS, time(19), time(23))) == "Every day, 7 pm to 11 pm"
    assert describe(WorkWindow(frozenset({6}), time(22), time(1, 30))) == (
        "Sun, 10 pm to 1:30 am the next day"
    )
    assert describe_days(frozenset(range(5))) == "Weekdays"
    assert describe_days(frozenset({5, 6})) == "Weekends"
    assert describe_days(frozenset({0, 1, 2, 3})) == "Mon-Thu"
    assert describe_days(frozenset({0, 2, 4})) == "Mon, Wed, Fri"
    assert describe_days(frozenset({0, 1})) == "Mon, Tue"
