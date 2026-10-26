"""Today's view models: the week of deadlines filed by the day they're worked on (D072), and the
kiosk rail's lanes and real hours (D063). Pure."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

from argus.clock import local_datetime
from argus.demo import DEMO_CONFIG
from argus.domain.models import ItemKind, ItemStatus
from argus.planner.freetime import Busy
from argus.planner.risk import RiskLevel
from argus.web.viewmodels import (
    CourseView,
    ItemView,
    timeline,
    today_summary,
    week_groups,
    work_day,
)

MON = date(2026, 10, 19)
NOW = local_datetime(MON, 9)
COURSE = CourseView("CMSC351", "teal")


def view(item_id: int, title: str, due: datetime | None = None) -> ItemView:
    return ItemView(
        id=item_id,
        title=title,
        course=COURSE,
        kind=ItemKind.ASSIGNMENT,
        kind_label="Assignment",
        status=ItemStatus.UPCOMING,
        status_label="Not started",
        risk=RiskLevel.OK,
        due_at=due,
        due_text=None,
        due_relative=None,
        sources=(),
        announced=False,
        conflict=(),
    )


WEEK_NOW = local_datetime(MON, 10, 15)


def at(days: int, h: int, m: int = 0) -> datetime:
    return local_datetime(MON + timedelta(days=days), h, m)


def test_morning_deadlines_are_the_previous_days_work() -> None:
    assert work_day(at(1, 7, 30), MON) == MON  # Tue 7:30 am is Monday's work
    assert work_day(at(1, 11, 59), MON) == MON
    assert work_day(at(1, 12), MON) == MON + timedelta(days=1)  # noon is its own day
    assert work_day(at(1, 23, 59), MON) == MON + timedelta(days=1)
    assert work_day(at(0, 11), MON) == MON  # this morning can't be yesterday's work


def test_the_week_runs_through_the_same_weekday_next_week() -> None:
    views = [
        view(1, "Tonight", at(0, 23, 59)),
        view(2, "Early Tue", at(1, 7, 30)),
        view(3, "Next Mon", at(7, 18)),
        view(4, "Next Tue morning", at(8, 7, 30)),  # Monday Nov 2's work: in
        view(5, "Next Tue afternoon", at(8, 13)),  # out
        view(6, "Past", at(0, 9)),  # past due: not a day group
        view(7, "Undated"),
    ]
    groups = week_groups(views, WEEK_NOW)
    assert [(g.label, [v.title for v in g.items]) for g in groups] == [
        ("Today", ["Tonight", "Early Tue"]),
        ("Mon, Oct 26", ["Next Mon", "Next Tue morning"]),
    ]
    assert groups[0].early == frozenset({2}) and groups[1].early == frozenset({4})


def test_the_week_leaves_out_done_work() -> None:
    done = replace(view(1, "Submitted", at(2, 23, 59)), status=ItemStatus.SUBMITTED)
    assert week_groups([done], WEEK_NOW) == []


def test_the_summary_counts_today_the_week_and_past_due() -> None:
    groups = week_groups(
        [view(1, "A", at(0, 23)), view(2, "B", at(1, 8)), view(3, "C", at(3, 23))], WEEK_NOW
    )
    assert (
        today_summary(groups, MON, past_due=1, to_confirm=0)
        == "2 to do today, 3 this week, 1 past due"
    )
    later = week_groups([view(3, "C", at(3, 23))], WEEK_NOW)
    assert today_summary(later, MON, past_due=0, to_confirm=0) == "Nothing to do today, 1 this week"
    assert today_summary([], MON, past_due=0, to_confirm=0) == "Nothing due this week"


def test_the_summary_asks_about_work_no_source_can_confirm() -> None:
    """D056: past-due work without a submission source is "to confirm", never "past due"."""
    assert today_summary([], MON, past_due=0, to_confirm=1) == "Nothing to do today, 1 to confirm"


def busy(h1: int, m1: int, h2: int, m2: int, title: str = "CMSC351 lecture") -> Busy:
    return Busy(local_datetime(MON, h1, m1), local_datetime(MON, h2, m2), "class", title, "CMSC351")


def test_the_kiosk_rail_puts_overlaps_in_columns_and_takes_in_late_deadlines() -> None:
    cfg = DEMO_CONFIG.model_copy(
        update={
            "schedule": DEMO_CONFIG.schedule.model_copy(
                update={
                    "waking_window": DEMO_CONFIG.schedule.waking_window.model_copy(
                        update={"start": time(8), "end": time(22)}
                    )
                }
            )
        }
    )
    tl = timeline(
        MON,
        [busy(10, 0, 11, 0), busy(10, 30, 11, 30, "MATH240 lab")],
        NOW,
        config=cfg,
        courses={"CMSC351": COURSE},
        due_items=[view(1, "Homework 3", local_datetime(MON, 23, 59))],
    )
    assert {(b.lane, b.lanes) for b in tl.blocks} == {(0, 2), (1, 2)}
    assert tl.end_hour == 24 and tl.span_hours == 16  # stretched from 10 pm to midnight
    assert [m.text for m in tl.due_marks] == ["11:59 pm"]


def test_the_rail_labels_real_hours_on_the_fall_back_day() -> None:
    cfg = DEMO_CONFIG.model_copy(
        update={
            "schedule": DEMO_CONFIG.schedule.model_copy(
                update={
                    "waking_window": DEMO_CONFIG.schedule.waking_window.model_copy(
                        update={"start": time(0), "end": time(23, 59)}
                    )
                }
            )
        }
    )
    tl = timeline(
        date(2026, 11, 1), [], local_datetime(date(2026, 11, 1), 9), config=cfg, courses={}
    )
    assert tl.span_hours == 25
    assert [h for h, _ in tl.hours[:4]] == ["12 am", "1 am", "1 am", "2 am"]
