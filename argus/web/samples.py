"""Synthetic data for /styleguide: every component in every state. Not real course content."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from functools import cache
from typing import Any

from argus.clock import to_local
from argus.config import AppConfig, CourseConfig
from argus.domain.models import Item, ItemKind, ItemLink, ItemStatus, MeetingRecord, Source, Weekday
from argus.planner.freetime import meetings_on
from argus.planner.risk import RiskLevel
from argus.web.deps import STATIC_DIR
from argus.web.format import due_text
from argus.web.viewmodels import (
    CourseView,
    EstimateView,
    FindingView,
    PracticeView,
    StaleNotice,
    WorkloadDay,
    course_views,
    diff_view,
    item_view,
    timeline,
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
            "links": (ItemLink(Source.CANVAS_ICAL, canvas),),
        }
        base.update(kw)
        return Item(**base)

    rows = [
        item_view(
            it(1, "Homework 4: dynamic programming", "CMSC351", timedelta(days=3)),
            now,
            courses,
            estimate=EstimateView(4.5, 3.5),
        ),
        item_view(
            it(2, "Problem Set 6", "MATH240", timedelta(hours=14)),
            now,
            courses,
            estimate=EstimateView(2.0, is_default=True),
        ),
        item_view(
            it(3, "Essay draft", "ENGL393", timedelta(hours=30), status=ItemStatus.IN_PROGRESS),
            now,
            courses,
            running_item_id=9003,
            estimate=EstimateView(3.0, 3.0),
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
        ),
        item_view(it(6, "Lab report 2", "MATH240", timedelta(hours=-3)), now, courses),
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
        ),
        item_view(it(8, "Course survey", "ENGL393", None), now, courses),
        # D056: no source reports submissions for it, so Argus asks instead of saying "overdue".
        item_view(
            it(9, "Peer review", "ENGL393", timedelta(hours=-5)),
            now,
            courses,
            risk=RiskLevel.UNCONFIRMED,
        ),
    ]
    lecture = MeetingRecord(
        "CMSC351", "lecture", tuple(Weekday), time(9, 30), time(10, 45), "IRB", "1101"
    )
    lab = MeetingRecord("MATH240", "lab", tuple(Weekday), time(13, 0), time(13, 50), "MTH", "B0425")
    day = to_local(now).date()
    tl = timeline(
        day, meetings_on(day, [lecture, lab]), now, config=cfg, courses=courses, due_items=[rows[1]]
    )
    workload = []
    for d in range(7):
        date = day + timedelta(days=d)
        need = [3.5, 5.0, 2.0, 6.5, 1.0, 0.0, 4.0][d]
        free = [4.0, 3.5, 6.0, 5.0, 3.0, 8.0, 7.5][d]
        workload.append(WorkloadDay(f"{date:%A}", f"{date:%a}", need, free))
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
        "courses": [
            *courses.values(),
            CourseView("HIST289", "rose"),
            CourseView("BMGT110", "ochre"),
        ],
        "risks": list(RiskLevel),
        "est_default": EstimateView(2.0, is_default=True),
        "est_cal": EstimateView(4.5, 3.5),
        "est_same": EstimateView(3.0, 3.0),
        "rows": rows,
        "timeline": tl,
        "workload": workload,
        "findings": findings,
        "diff": diff,
        "practice": practice,
        "stale": [StaleNotice("Gradescope last synced 3h ago", failing=True)],
    }
