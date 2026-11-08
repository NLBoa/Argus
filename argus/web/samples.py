"""Synthetic data for /styleguide: every component in every state. Not real course content."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from functools import cache
from typing import Any

from argus.clock import local_datetime, to_local
from argus.config import AppConfig, CourseConfig
from argus.domain.models import (
    SUBMISSION_SOURCES,
    Item,
    ItemKind,
    ItemLink,
    ItemStatus,
    MeetingRecord,
    Source,
    Weekday,
)
from argus.planner.freetime import meetings_on
from argus.planner.risk import RiskLevel
from argus.planner.workload import CurveDay
from argus.web.deps import STATIC_DIR
from argus.web.format import due_text
from argus.web.goals_data import (
    MeterView,
    MilestoneView,
    SessionView,
    TopicRowView,
    TrendBar,
    TrendRow,
    TrendView,
)
from argus.web.viewmodels import (
    Alert,
    CourseView,
    FindingView,
    PracticeView,
    StatusLine,
    StatusNote,
    course_views,
    diff_view,
    item_view,
    timeline,
    workload_chart,
    workload_days,
)

SAMPLE_CONFIG = AppConfig(
    courses=[
        CourseConfig(code="CMSC351", section="0101", accent="indigo"),
        CourseConfig(code="MATH240", section="0201", accent="teal"),
        CourseConfig(code="ENGL393", section="0301", accent="plum"),
    ]
)


@cache
def icon_names() -> list[str]:
    sprite = (STATIC_DIR / "icons" / "sprite.svg").read_text()
    return re.findall(r'<symbol id="i-([a-z0-9-]+)"', sprite)


def styleguide_samples(now: datetime) -> dict[str, Any]:
    cfg = SAMPLE_CONFIG
    courses = course_views(cfg)
    gs = "https://www.gradescope.com/courses/1"
    canvas = "https://umd.instructure.com/courses/1"

    def it(i: int, title: str, course: str, due: timedelta | None, **kw: Any) -> Item:
        base: dict[str, Any] = {
            "id": 9000 + i,
            "course_code": course,
            "kind": ItemKind.ASSIGNMENT,
            "title": title,
            "due_at": now + due if due is not None else None,
            "status": ItemStatus.UPCOMING,
            "links": (ItemLink(Source.CANVAS_API, canvas),),
        }
        base.update(kw)
        return Item(**base)

    rows = [
        item_view(
            it(1, "Homework 4: dynamic programming", "CMSC351", timedelta(days=3)),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
            left=5.5,
        ),
        item_view(
            it(2, "Problem Set 6", "MATH240", timedelta(hours=14)),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
            left=2.5,
        ),
        item_view(
            it(3, "Essay draft", "ENGL393", timedelta(hours=30), status=ItemStatus.IN_PROGRESS),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
            running_item_id=9003,
            left=1.5,
        ),
        item_view(
            it(
                4,
                "Quiz 3",
                "CMSC351",
                timedelta(days=4),
                kind=ItemKind.QUIZ,
                due_source=Source.FINDING,
                links=(
                    ItemLink(Source.FINDING, None),
                    ItemLink(Source.PIAZZA, "https://piazza.com/class/x/post/1"),
                ),
            ),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
        ),
        item_view(
            it(
                5,
                "Homework 3",
                "CMSC351",
                timedelta(days=2),
                links=(ItemLink(Source.CANVAS_ICAL, canvas), ItemLink(Source.GRADESCOPE, gs)),
                date_conflict=(
                    (Source.GRADESCOPE, now + timedelta(days=2)),
                    (Source.CANVAS_ICAL, now + timedelta(days=2, minutes=29)),
                ),
            ),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
        ),
        item_view(
            it(6, "Lab report 2", "MATH240", timedelta(hours=-3)),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
        ),
        item_view(
            it(
                7,
                "Reading response 5",
                "ENGL393",
                timedelta(hours=-20),
                status=ItemStatus.SUBMITTED,
            ),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
        ),
        item_view(
            it(8, "Course survey", "ENGL393", None),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
        ),
        # D056: no source reports submissions for it, so Argus asks instead of saying "overdue".
        item_view(
            it(9, "Peer review", "ENGL393", timedelta(hours=-5)),
            now,
            courses,
            tracking_sources=SUBMISSION_SOURCES,
            risk=RiskLevel.UNCONFIRMED,
        ),
    ]
    morning = local_datetime(to_local(now).date() + timedelta(days=1), 7, 30)
    early_row = item_view(
        it(10, "Pre-lab quiz", "MATH240", morning - now, kind=ItemKind.QUIZ),
        now,
        courses,
        tracking_sources=SUBMISSION_SOURCES,
        left=0.5,
    )
    lecture = MeetingRecord(
        "CMSC351", "lecture", tuple(Weekday), time(9, 30), time(10, 45), "IRB", "1101"
    )
    lab = MeetingRecord("MATH240", "lab", tuple(Weekday), time(13, 0), time(13, 50), "MTH", "B0425")
    day = to_local(now).date()
    tl = timeline(
        day, meetings_on(day, [lecture, lab]), now, config=cfg, courses=courses, due_items=[rows[1]]
    )
    window_h = [4.0, 4.0, 4.0, 4.0, 0.0, 0.0, 3.0, 4.0, 4.0, 4.0, 4.0, 0.0, 0.0, 3.0]
    due_h = [0.0, 3.5, 9.0, 2.0, 0.0, 0.0, 1.0, 4.0, 0.0, 6.0, 1.5, 0.0, 0.0, 2.0]
    curve_days, wt, dt = [], 0.0, 0.0
    for d, (w, h) in enumerate(zip(window_h, due_h, strict=True)):
        wt, dt = wt + w, dt + h
        curve_days.append(CurveDay(local_datetime(day + timedelta(days=d), 0), w, h, wt, dt))
    workload = workload_chart(workload_days(curve_days, day))
    post = "https://piazza.com/class/x/post/101"
    findings = [
        FindingView(
            1,
            courses["CMSC351"],
            "New quiz",
            "Quiz 3",
            "Quiz 3 will be next Friday in discussion, covering lectures 8 to 10.",
            "Piazza note by the instructor",
            post,
            "posted 2h ago",
            "pending",
            date_text=due_text(now + timedelta(days=11), now),
        ),
        FindingView(
            2,
            courses["MATH240"],
            "Date change",
            "Midterm 1",
            "The midterm is moving to Thursday, Nov 5.",
            "Canvas announcement",
            post,
            "posted yesterday",
            "conflict",
            date_text="Thu, Nov 5",
            official_name="ELMS",
            official_text="Tue, Nov 3",
            confirm_label="Use announced date",
        ),
        FindingView(
            3,
            courses["ENGL393"],
            "Scope",
            "Final portfolio",
            "The portfolio covers essays 1 through 3 only.",
            "Piazza note by a TA",
            post,
            "posted 3 days ago",
            "applied",
        ),
    ]
    diff = diff_view(
        "Courses/CMSC351/Concepts/Master theorem.md",
        "The master theorem solves recurrences\n"
        "of the form T(n) = aT(n/b) + f(n).\n"
        "My note: case 2 is the tricky one.",
        "The master theorem solves divide-and-conquer recurrences\n"
        "of the form T(n) = aT(n/b) + f(n).\n"
        "My note: case 2 is the tricky one.\n"
        "It does not apply when f(n) is not polynomially comparable.",
    )
    practice = [
        PracticeView(
            1,
            courses["CMSC351"],
            "Master theorem",
            "What is the solution to T(n) = 2T(n/2) + n?",
            "Theta(n log n), by case 2 of the master theorem.",
            "Lecture 6, slide 14",
        ),
        PracticeView(
            2,
            courses["MATH240"],
            "Eigenvalues",
            "Can a 2x2 real matrix have no real eigenvalues?",
            "Yes, for example a 90-degree rotation matrix.",
            "Textbook 5.5, p. 336",
            result="correct",
        ),
        PracticeView(
            3,
            courses["CMSC351"],
            "Heaps",
            "What is the cost of building a heap bottom-up?",
            "Theta(n).",
            "Lecture 9, slide 4",
            result="incorrect",
        ),
    ]
    return {
        "goals": goal_samples(now),
        "courses": [
            *courses.values(),
            CourseView("HIST289", "rose"),
            CourseView("BMGT110", "ochre"),
        ],
        "risks": list(RiskLevel),
        "rows": rows,
        "early_row": early_row,
        "timeline": tl,
        "workload": workload,
        "findings": findings,
        "diff": diff,
        "practice": practice,
        "status": StatusLine(
            [
                StatusNote("Gradescope 3h behind", "stale", "/health"),
                StatusNote("Piazza hasn't synced yet", "failing", "/health"),
                StatusNote("1 note edit to review", "info", "/proposals"),
            ],
            [
                Alert(
                    "Gradescope has failed 3 syncs in a row, so its dates may be out of date.",
                    "/health",
                    "Check health",
                )
            ],
        ),
    }


def goal_samples(now: datetime) -> dict[str, Any]:
    """Every goals-page state for /styleguide (D079)."""
    m = MilestoneView
    tracks = {
        "Nothing done yet": [
            m("Hand tools", "current", "Three clean cuts"),
            m("Joints", "next", None),
        ],
        "One done": [
            m("Hand tools", "done", None),
            m("Joints", "current", "Three clean dovetails"),
            m("Finishing", "next", None),
        ],
        "All done": [m("Hand tools", "done", None), m("Joints", "done", None)],
    }

    def s(
        what: str, state: str, label: str, hours: int, *, minutes: int = 60, nxt: bool = False
    ) -> SessionView:
        at = now + timedelta(hours=hours)
        return SessionView(
            what,
            f"{to_local(at):%a %b} {to_local(at).day}",
            "7 pm",
            f"{minutes} min",
            state,
            label,
            nxt,
            "Two joints with no gaps" if nxt else None,
            at,
        )

    sessions = [
        s("Practice dovetails", "done", "Done", -48),
        s("Glue up the carcass", "scheduled", "Next", 2, nxt=True),
        s("Weekend build", "scheduled", "Scheduled", 40, minutes=120),
        s("Sand the shelves", "not_done", "Not done", -20),
        s("Sharpen chisels", "dropped", "Dropped", -30, minutes=45),
        s("Finish oil test", "moved", "Moved", -10, minutes=45),
    ]
    meters = {
        "Week 1, nothing done": MeterView(0, 75, 100, "0 of 240 minutes done, 180 more scheduled"),
        "Mid-week": MeterView(40, 35, 100, "120 of 300 minutes done, 105 more scheduled"),
        "Missed the target": MeterView(50, 0, 100, "120 of 240 minutes done, 0 more scheduled"),
        "Past the target": MeterView(100, 0, 83.33, "360 of 300 minutes done, 0 more scheduled"),
    }

    def bars(*vals: tuple[int, int, bool]) -> list[TrendBar]:
        weeks = ["W46", "W47", "W48", "W49"][-len(vals) :]
        return [
            TrendBar(w, d, tg, p, round(100 * d / 300, 2), round(100 * tg / 300, 2))
            for w, (d, tg, p) in zip(weeks, vals, strict=True)
        ]

    trend = TrendView(
        ["W46", "W47", "W48", "W49"],
        [
            TrendRow(
                "Woodwork",
                "olive",
                bars((240, 300, False), (300, 300, False), (150, 300, False), (120, 300, True)),
            ),
            TrendRow(
                "Spanish",
                "plum",
                bars((105, 105, False), (60, 105, False), (90, 120, False), (0, 120, True)),
            ),
        ],
        300,
    )
    dash = "\N{EN DASH}"
    topics = [
        TopicRowView(
            "Woodwork",
            "olive",
            "Shop safety",
            ["72", "85", dash, dash],
            "review_due",
            "Review overdue: due Nov 28",
            True,
        ),
        TopicRowView(
            "Woodwork",
            "olive",
            "Dovetails",
            [dash, "55", "78", dash],
            "practice",
            "Practice more",
            False,
        ),
        TopicRowView(
            "Spanish",
            "plum",
            "Ser and estar",
            ["84", dash, "88", dash],
            "review_later",
            "Review Dec 12",
            False,
        ),
        TopicRowView(
            "Spanish",
            "plum",
            "Past tense",
            ["55", dash, dash, dash],
            "reteach",
            "Re-teach with something other than \u201cvideo\u201d",
            False,
        ),
        TopicRowView(
            "Woodwork", "olive", "Sharpening", ["82", "85", "90", "91"], "kept", "Kept", False
        ),
        TopicRowView(
            "Woodwork",
            "olive",
            "Finishing oils",
            ["90", dash, dash, dash],
            "review_due",
            "Review due Dec 5",
            False,
        ),
    ]
    return {
        "tracks": tracks,
        "sessions": sessions,
        "meters": meters,
        "trend": trend,
        "topics": topics,
        "topic_weeks": ["W46", "W47", "W48", "W49"],
    }
