"""Extraction eval set: synthetic but realistic course posts with labeled findings.

Times: posts are mostly made on Monday, Oct 19, 2026 (EDT). `dates` lists every acceptable
local date for a finding: an empty list means the post states no date (any date is invented).
Ambiguous phrases ("next Friday" said on a Monday) accept either reading.
"""

from __future__ import annotations

from typing import Any

MON_10AM = "2026-10-19T14:00:00Z"
FRI_4PM = "2026-10-23T20:00:00Z"
SUN_11AM = "2026-10-18T15:00:00Z"
WED_9PM = "2026-10-22T01:00:00Z"  # Wed Oct 21, 9 pm EDT
OCT_30_NOON = "2026-10-30T16:00:00Z"  # Fri Oct 30 noon EDT

KNOWN_351 = [
    {"title": "Quiz 2", "kind": "quiz", "due": "2026-10-23T16:00:00Z"},
    {"title": "Midterm 1", "kind": "exam", "due": "2026-10-28T13:30:00Z"},
    {"title": "Homework 4", "kind": "assignment", "due": "2026-10-25T03:59:00Z"},
    {"title": "Project 1", "kind": "assignment", "due": "2026-11-06T04:59:00Z"},
]
KNOWN_240 = [
    {"title": "Problem Set 6", "kind": "assignment", "due": "2026-10-22T03:59:00Z"},
    {"title": "Quiz 3", "kind": "quiz", "due": "2026-10-23T15:00:00Z"},
    {"title": "Exam 2", "kind": "exam", "due": "2026-11-12T15:00:00Z"},
]
KNOWN_393 = [{"title": "Essay draft", "kind": "assignment", "due": "2026-10-21T21:00:00Z"}]


def post(
    pid: str,
    course: str,
    body: str,
    expect: list[dict[str, Any]],
    *,
    title: str = "",
    role: str = "instructor",
    posted: str = MON_10AM,
    tags: tuple[str, ...] = (),
) -> dict[str, Any]:
    known = {"CMSC351": KNOWN_351, "MATH240": KNOWN_240, "ENGL393": KNOWN_393}[course]
    return {
        "id": pid,
        "course": course,
        "role": role,
        "posted": posted,
        "title": title or body.split(".", maxsplit=1)[0][:60],
        "body": body,
        "known": known,
        "expect": expect,
        "tags": list(tags),
    }


def f(kind: str, title: str, *dates: str) -> dict[str, Any]:
    return {"kind": kind, "title": title, "dates": list(dates)}


POSTS: list[dict[str, Any]] = [
    # ---------------------------------------------------------------- explicit announcements
    post(
        "a01",
        "CMSC351",
        "Quiz 3 will be held on Friday, October 30 during discussion. It covers lectures 10-12.",
        [f("new_assessment", "Quiz 3", "2026-10-30"), f("scope", "Quiz 3")],
        tags=("announcement",),
    ),
    post(
        "a02",
        "MATH240",
        "Quiz 4 is scheduled for Thursday, Nov 5, at the start of class.",
        [f("new_assessment", "Quiz 4", "2026-11-05")],
        tags=("announcement",),
    ),
    post(
        "a03",
        "CMSC351",
        "Homework 5 has been posted and is due Wednesday, November 4 at 11:59pm on Gradescope.",
        [f("new_assessment", "Homework 5", "2026-11-04")],
        tags=("announcement",),
    ),
    post(
        "a04",
        "ENGL393",
        "The final portfolio is due December 10. Instructions are on ELMS.",
        [f("new_assessment", "Final portfolio", "2026-12-10")],
        tags=("announcement",),
    ),
    post(
        "a05",
        "MATH240",
        "Reminder: Exam 2 is on 11/12 in our usual room. Bring a pencil.",
        [f("new_assessment", "Exam 2", "2026-11-12")],
        tags=("announcement",),
    ),
    post(
        "a06",
        "CMSC351",
        "Project 2 will be released Monday and is due 2026-11-20.",
        [f("new_assessment", "Project 2", "2026-11-20")],
        tags=("announcement",),
    ),
    post(
        "a07",
        "ENGL393",
        "Peer review session for the essay draft: bring two printed copies on Wednesday, October 28.",
        [f("logistics", "Peer review", "2026-10-28")],
        tags=("announcement",),
    ),
    post(
        "a08",
        "CMSC351",
        "There will be a pop quiz in lecture on Thursday, October 22. It will be short.",
        [f("new_assessment", "Pop quiz", "2026-10-22")],
        tags=("announcement",),
    ),
    # ---------------------------------------------------------------- relative dates
    post(
        "r01",
        "CMSC351",
        "Quiz 3 will be next Friday in discussion.",
        [f("new_assessment", "Quiz 3", "2026-10-23", "2026-10-30")],
        tags=("relative", "ambiguous"),
    ),
    post(
        "r02",
        "MATH240",
        "Quiz 4 is tomorrow at the start of class, covering eigenvectors.",
        [f("new_assessment", "Quiz 4", "2026-10-20"), f("scope", "Quiz 4")],
        tags=("relative",),
    ),
    post(
        "r03",
        "CMSC351",
        "Homework 5 is due in two weeks, on the Wednesday.",
        [f("new_assessment", "Homework 5", "2026-11-04")],
        tags=("relative",),
    ),
    post(
        "r04",
        "MATH240",
        "We'll have Quiz 5 this Thursday.",
        [f("new_assessment", "Quiz 5", "2026-10-22")],
        tags=("relative",),
    ),
    post(
        "r05",
        "ENGL393",
        "Your annotated bibliography is due a week from today.",
        [f("new_assessment", "Annotated bibliography", "2026-10-26")],
        tags=("relative",),
    ),
    post(
        "r06",
        "CMSC351",
        "Quiz 4 will be on Friday.",
        [f("new_assessment", "Quiz 4", "2026-10-23")],
        posted=SUN_11AM,
        tags=("relative",),
    ),
    post(
        "r07",
        "MATH240",
        "Since today is Wednesday, the problem set is due tomorrow night.",
        [f("date_change", "Problem Set 6", "2026-10-22")],
        posted=WED_9PM,
        tags=("relative", "date_change"),
    ),
    post(
        "r08",
        "CMSC351",
        "The Midterm 1 review session is next week; the midterm itself is the following Wednesday.",
        [f("new_assessment", "Midterm 1", "2026-11-04", "2026-10-28")],
        posted=FRI_4PM,
        tags=("relative", "ambiguous"),
    ),
    # ---------------------------------------------------------------- reschedules of official items
    post(
        "d01",
        "CMSC351",
        "Because of the career fair, Midterm 1 moves to Thursday, October 29, same time and room.",
        [f("date_change", "Midterm 1", "2026-10-29")],
        tags=("date_change",),
    ),
    post(
        "d02",
        "CMSC351",
        "Homework 4 deadline extended to Monday, October 26 at 11:59pm.",
        [f("date_change", "Homework 4", "2026-10-26")],
        tags=("date_change",),
    ),
    post(
        "d03",
        "MATH240",
        "Quiz 3 is pushed back one week to October 30.",
        [f("date_change", "Quiz 3", "2026-10-30")],
        tags=("date_change",),
    ),
    post(
        "d04",
        "MATH240",
        "Exam 2 has moved earlier, to Tuesday, November 10.",
        [f("date_change", "Exam 2", "2026-11-10")],
        tags=("date_change",),
    ),
    post(
        "d05",
        "ENGL393",
        "The essay draft is now due Friday, October 23 instead of Wednesday.",
        [f("date_change", "Essay draft", "2026-10-23")],
        tags=("date_change",),
    ),
    post(
        "d06",
        "CMSC351",
        "Project 1 extension: it is now due November 9.",
        [f("date_change", "Project 1", "2026-11-09")],
        tags=("date_change",),
    ),
    post(
        "d07",
        "CMSC351",
        "Quiz 2 will happen tomorrow instead of Friday.",
        [f("date_change", "Quiz 2", "2026-10-20")],
        tags=("date_change", "relative"),
    ),
    # ---------------------------------------------------------------- cancellations
    post(
        "c01",
        "MATH240",
        "Quiz 3 is cancelled. Its weight moves to the final.",
        [f("cancellation", "Quiz 3")],
        tags=("cancellation",),
    ),
    post(
        "c02",
        "CMSC351",
        "No class on Thursday, October 22 due to the instructor's travel. Discussion still meets.",
        [f("cancellation", "Lecture", "2026-10-22")],
        tags=("cancellation",),
    ),
    post(
        "c03",
        "ENGL393",
        "We will not have a quiz this week after all.",
        [f("cancellation", "Quiz")],
        tags=("cancellation",),
    ),
    post(
        "c04",
        "CMSC351",
        "Homework 4 is cancelled; everyone gets full credit.",
        [f("cancellation", "Homework 4")],
        tags=("cancellation",),
    ),
    # ---------------------------------------------------------------- scope (no dates)
    post(
        "s01",
        "CMSC351",
        "Midterm 1 covers lectures 1 through 12. One double-sided cheat sheet is allowed.",
        [f("scope", "Midterm 1")],
        tags=("scope",),
    ),
    post(
        "s02",
        "MATH240",
        "Exam 2 will cover sections 4.1-5.3 and you may use a non-graphing calculator.",
        [f("scope", "Exam 2")],
        tags=("scope",),
    ),
    post(
        "s03",
        "CMSC351",
        "Quiz 2 covers only recurrences, not the master theorem.",
        [f("scope", "Quiz 2")],
        tags=("scope",),
    ),
    post(
        "s04",
        "ENGL393",
        "The essay draft should be about 1,000 words and use MLA format.",
        [f("scope", "Essay draft")],
        tags=("scope",),
    ),
    post(
        "s05",
        "MATH240",
        "No cheat sheets are allowed on Quiz 3.",
        [f("scope", "Quiz 3")],
        tags=("scope",),
    ),
    # ---------------------------------------------------------------- logistics
    post(
        "l01",
        "CMSC351",
        "Midterm 1 will be in ESJ 0202 instead of our usual room.",
        [f("logistics", "Midterm 1")],
        tags=("logistics",),
    ),
    post(
        "l02",
        "MATH240",
        "Quiz 3 will be taken online through ELMS, not in person.",
        [f("logistics", "Quiz 3")],
        tags=("logistics",),
    ),
    post(
        "l03",
        "CMSC351",
        "Submit Project 1 as a single PDF on Gradescope; tag every page.",
        [f("logistics", "Project 1")],
        tags=("logistics",),
    ),
    # ---------------------------------------------------------------- multiple findings
    post(
        "m01",
        "CMSC351",
        "Two updates: Homework 4 is now due Monday, October 26, and Quiz 3 will be on Friday, October 30.",
        [f("date_change", "Homework 4", "2026-10-26"), f("new_assessment", "Quiz 3", "2026-10-30")],
        tags=("multi", "date_change"),
    ),
    post(
        "m02",
        "MATH240",
        "Exam 2 moves to November 13 and will cover sections 4.1-5.3.",
        [f("date_change", "Exam 2", "2026-11-13"), f("scope", "Exam 2")],
        tags=("multi", "date_change"),
    ),
    # ---------------------------------------------------------------- noise
    post(
        "n01",
        "CMSC351",
        "Office hours today are moved to 3pm in IRB 2107.",
        [],
        title="Office hours",
        tags=("noise",),
    ),
    post(
        "n02",
        "MATH240",
        "Great job on the last problem set, everyone. Solutions are posted.",
        [],
        tags=("noise",),
    ),
    post(
        "n03",
        "CMSC351",
        "Anyone want to form a study group for the midterm? I'm free most evenings.",
        [],
        role="student",
        tags=("noise",),
    ),
    post(
        "n04",
        "ENGL393",
        "Here is a helpful article on writing clear technical descriptions.",
        [],
        tags=("noise",),
    ),
    post(
        "n05",
        "CMSC351",
        "Lecture slides for today are up on ELMS under Week 8.",
        [],
        tags=("noise",),
    ),
    post(
        "n06",
        "MATH240",
        "Can someone explain why the determinant is multiplicative?",
        [],
        role="student",
        tags=("noise",),
    ),
    post(
        "n07",
        "CMSC351",
        "I heard the midterm might be moved but I'm not sure, has anyone confirmed?",
        [],
        role="student",
        tags=("noise", "hearsay"),
    ),
]

assert len(POSTS) >= 40
