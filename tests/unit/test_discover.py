"""Course discovery: codes, sections, and ids from the feed and the student's own class lists."""

from argus.discover import (
    Found,
    current_term,
    feed_courses,
    match_code,
    render_courses,
    replace_courses,
)

FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-assignment-1
DTSTART:20261020T035900Z
SUMMARY:Project 2 [CMSC216-0101-Fall2026]
URL;VALUE=URI:https://umd.instructure.com/courses/1111111/assignments/1
END:VEVENT
BEGIN:VEVENT
UID:event-assignment-2
DTSTART:20261021T035900Z
SUMMARY:Lab 4 [CMSC216-0101-Fall2026]
URL;VALUE=URI:https://umd.instructure.com/courses/1111111/assignments/2
END:VEVENT
BEGIN:VEVENT
UID:event-calendar-event-3
DTSTART:20261022T140000Z
SUMMARY:Midterm [CMSC250-0201-Fall2026]
URL;VALUE=URI:https://umd.instructure.com/calendar?event_id=3&include_contexts=course_2222222
END:VEVENT
BEGIN:VEVENT
UID:event-assignment-4
DTSTART:20261023T035900Z
SUMMARY:Essay [ENGL 101 Fall 2026]
URL;VALUE=URI:https://umd.instructure.com/courses/3333333/assignments/4
END:VEVENT
BEGIN:VEVENT
UID:event-assignment-5
DTSTART:20260512T035900Z
SUMMARY:Final project [MATH140-0301-Spring2026]
URL;VALUE=URI:https://umd.instructure.com/courses/4444444/assignments/5
END:VEVENT
END:VCALENDAR
"""


def test_feed_courses_finds_code_section_and_canvas_id() -> None:
    found = feed_courses(FEED)
    assert found == {
        "CMSC216": ("0101", 1111111, "Fall 2026"),
        "CMSC250": ("0201", 2222222, "Fall 2026"),
        "ENGL101": (None, 3333333, "Fall 2026"),
        "MATH140": ("0301", 4444444, "Spring 2026"),
    }
    assert feed_courses(b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n") == {}


def test_current_term_leaves_out_finished_courses() -> None:
    kept, left_out = current_term(feed_courses(FEED))
    assert list(kept) == ["CMSC216", "CMSC250", "ENGL101"]
    assert left_out == ["MATH140 (Spring 2026)"]
    # A tag without a term can't be judged, so it stays (a missed deadline costs more).
    courses = {"CMSC216": ("0101", 1, "Fall 2026"), "HONR100": (None, 2, None)}
    assert current_term(courses) == (courses, [])
    assert current_term({}) == ({}, [])


def test_match_code_reads_names_people_use() -> None:
    assert match_code("CMSC 216: Introduction to Computer Systems") == "CMSC216"
    assert match_code("cmsc250 Discrete Structures (Fall 2026)") == "CMSC250"
    assert match_code("CMSC216-0101") == "CMSC216"
    assert match_code("Office hours") is None


def test_render_and_replace_keep_the_rest_of_the_config() -> None:
    found = [
        Found("CMSC216", "0101", 1111111, "654321", "abc123"),
        Found("CMSC250", None, 2222222, None, "xyz789"),
    ]
    block = render_courses(found)
    assert block == (
        "courses:\n"
        "  - code: CMSC216\n"
        '    section: "0101"\n'
        "    canvas_id: 1111111\n"
        '    gradescope_id: "654321"\n'
        '    piazza_network_id: "abc123"\n'
        "    accent: indigo\n"
        "  - code: CMSC250\n"
        '    section: "0101"  # not in the ELMS feed: check your section on Testudo\n'
        "    canvas_id: 2222222\n"
        '    piazza_network_id: "xyz789"\n'
        "    accent: teal\n"
    )
    config = (
        "# my settings\n"
        "courses:\n"
        "  - code: CMSC351\n"
        '    section: "0101"\n'
        "\n"
        "schedule:\n"
        '  morning_brief: "08:00"\n'
    )
    out = replace_courses(config, block)
    assert out == "# my settings\n" + block + '\nschedule:\n  morning_brief: "08:00"\n'
    assert replace_courses("schedule: {}\n", block) == block + "schedule: {}\n"


def test_pick_prefers_the_latest_term_and_reports_the_rest() -> None:
    from argus.discover import pick, term_rank

    listed = [
        ("111", "CMSC216 Intro to Computer Systems", "Spring 2026"),
        ("222", "CMSC216 Intro to Computer Systems", "Fall 2026"),
        ("333", "CMSC 250 Discrete Structures", "Fall 2026"),
        ("444", "MATH140 Calculus I", "Fall 2025"),
        ("555", "Office hours", "Fall 2026"),
    ]
    ids, others = pick(["CMSC216", "CMSC250"], listed)
    assert ids == {"CMSC216": "222", "CMSC250": "333"}
    assert others == ["MATH140"]
    assert (
        term_rank("Fall 2026")
        > term_rank("Spring 2026")
        > term_rank("Fall 2025")
        > term_rank("???")
    )
