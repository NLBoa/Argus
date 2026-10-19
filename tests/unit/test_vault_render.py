"""Vault note rendering: layout paths, the study template, exam prep, and Anki card syntax."""

import json
from datetime import UTC, date, datetime

from argus.vault import layout, render
from argus.vault.regions import compose, parse

DUE = datetime(2026, 10, 22, 3, 59, tzinfo=UTC)  # Wed Oct 21, 11:59 pm Eastern


def text_of(note: render.DesiredNote) -> str:
    return compose(note.frontmatter, note.regions, note.tail)


def test_layout_paths_are_safe_and_stable() -> None:
    assert layout.safe('HW 3: "Graphs" / Trees?') == "HW 3 Graphs Trees"
    assert layout.safe("...") == "Untitled"
    assert layout.safe("x" * 200).__len__() == 80
    assert (
        layout.concept_note("CMSC351", "Master theorem")
        == "Courses/CMSC351/Concepts/Master theorem.md"
    )
    assert layout.course_note("CMSC351") == "Courses/CMSC351/CMSC351.md"
    assert layout.weekly(date(2026, 10, 19)) == "Weekly/2026-W43.md"
    assert layout.weekly(date(2027, 1, 1)) == "Weekly/2026-W53.md"
    assert layout.link("Courses/X/A.md", "A") == "[[Courses/X/A|A]]"
    assert layout.link("Review/Today.md") == "[[Review/Today]]"
    assert layout.obsidian_uri("My Vault", "Courses/CMSC351/CMSC351.md") == (
        "obsidian://open?vault=My%20Vault&file=Courses/CMSC351/CMSC351"
    )


CONCEPT = render.ConceptData(
    course="CMSC351",
    module="Recurrences",
    name="Master theorem",
    body={
        "idea": [
            {
                "text": "Solves T(n) = aT(n/b) + f(n).",
                "cites": ["Lecture 7, slide 3"],
                "verified": True,
            }
        ],
        "diagram": "graph TD\n  A-->B",
        "worked_example": [{"text": "a=2, b=2, f=n.", "cites": [], "verified": True}],
        "why_it_works": [
            {
                "text": "Compare f(n) to n^log_b a.",
                "cites": ["Lecture 7, slide 4"],
                "verified": False,
            }
        ],
        "common_mistakes": [
            {"text": "Forgetting the regularity condition.", "cites": [], "verified": True}
        ],
    },
    sources=[{"ref": "Lecture 7, slide 3", "url": "https://x"}],
    verified_at=DUE,
    practice=[("What is T(n) for a=2, b=2, f=n?", "Theta(n log n)", "Lecture 7, slide 5")],
    related=["Courses/CMSC351/Concepts/Recursion trees.md"],
)


def test_concept_note_follows_the_study_template() -> None:
    note = render.concept_note(CONCEPT)
    text = text_of(note)
    assert note.frontmatter["verified"] == "2026-10-21"
    assert note.frontmatter["sources"] == ["Lecture 7, slide 3"]
    core = parse(text).regions["core"].content
    assert core.startswith("**The idea.** Solves T(n) = aT(n/b) + f(n). (Lecture 7, slide 3)")
    assert "```mermaid\ngraph TD" in core
    assert "1. a=2, b=2, f=n." in core
    assert "_(source unconfirmed)_" in core
    assert "> [!question]- What is T(n) for a=2, b=2, f=n?\n> Theta(n log n)" in core
    assert "[[Courses/CMSC351/Concepts/Recursion trees|Recursion trees]]" in core
    # The student's sections sit outside the Argus region.
    assert text.index("## Explain it yourself") > text.index("%% argus:end core %%")
    assert "## My notes" in note.tail


def test_unverified_concept_and_minimal_body() -> None:
    bare = render.ConceptData("CMSC351", None, "X", {"idea": [{"text": "x"}]}, [], None, [])
    note = render.concept_note(bare)
    assert note.frontmatter["verified"] is False and note.frontmatter["module"] == ""
    assert "## Picture" not in note.regions[0][1] and "## Test yourself" not in note.regions[0][1]


def item(title: str, status: str = "upcoming", note: str | None = None) -> render.ItemLine:
    return render.ItemLine(title, "assignment", DUE, status, note, None)


def test_course_note_lists_open_work_exams_and_modules() -> None:
    c = render.CourseData(
        code="CMSC351",
        title="Algorithms",
        meetings=["Mon/Wed 10 am, IRB 0324"],
        links=[("Canvas", "https://canvas/1"), ("Piazza", "https://piazza/1")],
        items=[item("HW 3", note="Courses/CMSC351/Assignments/HW 3.md"), item("HW 2", "submitted")],
        modules=[("Week 1", "Courses/CMSC351/Modules/Week 1.md")],
        exams=[("Midterm 1", "Courses/CMSC351/Exams/Midterm 1.md")],
        grade="91.2% (A-)",
    )
    core = render.course_note(c).regions[0][1]
    assert core.startswith("# CMSC351: Algorithms")
    assert "[Canvas](https://canvas/1), [Piazza](https://piazza/1)" in core
    assert (
        "- [[Courses/CMSC351/Assignments/HW 3|HW 3]], assignment, due Wed Oct 21, 11:59 pm" in core
    )
    assert "HW 2" not in core
    assert "[[Courses/CMSC351/Exams/Midterm 1|Midterm 1]]" in core and "Week 1" in core
    assert "**Current grade:** 91.2% (A-)" in core
    empty = render.course_note(render.CourseData("X", None, [], [], [], [], [], None)).regions[0][1]
    assert "Nothing open right now." in empty


def test_module_and_assignment_notes() -> None:
    m = render.module_note(
        "CMSC351", "Week 1", [("Slides", "https://s")], ["Courses/CMSC351/Concepts/A.md"]
    )
    assert m.frontmatter == {"course": "CMSC351", "module": "Week 1"}
    assert (
        "- [Slides](https://s)" in m.regions[0][1]
        and "[[Courses/CMSC351/Concepts/A|A]]" in m.regions[0][1]
    )
    assert "No files" in render.module_note("C", "M", [], []).regions[0][1]
    a = render.assignment_note(
        render.AssignmentData(
            "CMSC351",
            "HW 3",
            "assignment",
            DUE,
            "in_progress",
            "3 h (p80 4.5 h)",
            [("Gradescope", "https://g")],
            "Ch. 4",
            None,
        )
    )
    body = a.regions[0][1]
    assert (
        "**Status:** in progress" in body
        and "**Estimate:** 3 h" in body
        and "[Gradescope](https://g)" in body
    )
    assert a.frontmatter["due"] == "2026-10-21T23:59:00-04:00"
    assert "## My plan" in a.tail
    undated = render.assignment_note(
        render.AssignmentData("C", "T", "quiz", None, "upcoming", None, [], None, "Bring a pencil")
    )
    assert "no due date" in undated.regions[0][1] and "Bring a pencil" in undated.regions[0][1]


def test_exam_note_has_scope_readiness_plan_and_folded_practice() -> None:
    e = render.ExamData(
        course="CMSC351",
        title="Midterm 1",
        at=DUE,
        scope="Chapters 1-4",
        logistics="IRB 0324, one cheat sheet",
        concepts=[
            render.ExamConcept(
                "Master theorem", "Courses/CMSC351/Concepts/Master theorem.md", "shaky"
            ),
            render.ExamConcept("Big-O", "Courses/CMSC351/Concepts/Big-O.md", "solid"),
        ],
        sessions=[(datetime(2026, 10, 15, 18, tzinfo=UTC), "Review: Master theorem, Big-O")],
        practice=[("Q?", "A.", "Big-O")],
    )
    note = render.exam_note(e)
    regions = dict(note.regions)
    assert "**Readiness:** 1 solid, 1 shaky, 0 untested" in regions["scope"]
    assert (
        "| [[Courses/CMSC351/Concepts/Master theorem|Master theorem]] | shaky |" in regions["scope"]
    )
    assert "- Thu Oct 15, 2 pm: Review: Master theorem, Big-O" in regions["plan"]
    assert "**1.** Q?\n\n> [!check]- Answer\n> A." in regions["practice"]
    assert note.frontmatter["date"] == "2026-10-21"
    empty = dict(render.exam_note(render.ExamData("C", "E", None, None, None, [], [], [])).regions)
    assert "appears here" in empty["plan"] and "appear here" in empty["practice"]


def test_flashcards_use_obsidian_to_anki_syntax_and_keep_ids() -> None:
    cards = [("What is  Big-O?", "An upper\nbound.", "big-o"), ("New card?", "Yes.", "x")]
    first = render.flashcards_note("CMSC351", cards, None).regions[0][1]
    assert first.startswith("TARGET DECK: Argus::CMSC351\n\n")
    assert (
        "START\nBasic\nWhat is Big-O?\nBack: An upper bound.\nTags: argus CMSC351 big-o\nEND"
        in first
    )
    annotated = first.replace(
        "Tags: argus CMSC351 big-o\nEND", "Tags: argus CMSC351 big-o\n<!--ID: 1566052191670-->\nEND"
    )
    again = render.flashcards_note("CMSC351", cards, annotated).regions[0][1]
    assert again == annotated  # the plugin's id survives a rewrite
    assert render.existing_card_ids(annotated) == {"What is Big-O?": "1566052191670"}
    assert render.existing_card_ids(None) == {} and render.existing_card_ids("START\nEND") == {}
    assert "No cards yet." in render.flashcards_note("C", [], None).regions[0][1]


def test_concept_map_is_json_canvas() -> None:
    canvas = json.loads(
        render.concept_map_canvas(
            [
                ("Courses/C/Concepts/A.md", "A", 0),
                ("Courses/C/Concepts/B.md", "B", 1),
                ("Courses/C/Concepts/C.md", "C", 0),
            ],
            [
                ("Courses/C/Concepts/A.md", "Courses/C/Concepts/B.md"),
                ("Courses/C/Concepts/A.md", "Courses/C/Missing.md"),
            ],
        )
    )
    assert [n["file"] for n in canvas["nodes"]] == [
        "Courses/C/Concepts/A.md",
        "Courses/C/Concepts/C.md",
        "Courses/C/Concepts/B.md",
    ]
    assert canvas["nodes"][2]["x"] == 320 and canvas["nodes"][1]["y"] == 120
    assert canvas["edges"] == [{"id": "e1", "fromNode": "n1", "toNode": "n3"}]


def test_today_weekly_and_proposals() -> None:
    day = date(2026, 10, 19)
    start, end = datetime(2026, 10, 19, 18, tzinfo=UTC), datetime(2026, 10, 19, 19, 30, tzinfo=UTC)
    today = render.review_today(
        day, [(start, end, "HW 3", "Courses/C/Assignments/HW 3.md")], [item("HW 3")]
    )
    body = today.regions[0][1]
    assert (
        body.startswith("# Monday, October 19")
        and "- 2 pm to 3:30 pm: HW 3 ([[Courses/C/Assignments/HW 3]])" in body
    )
    assert "No work windows today." in render.review_today(day, [], []).regions[0][1]
    weekly = dict(render.weekly_note(day, [], ["Courses/C/C.md: rewrote core"]).regions)
    assert (
        "Nothing due this week." in weekly["summary"]
        and "- Courses/C/C.md: rewrote core" in weekly["changes"]
    )
    assert render.weekly_note(day, [], []).frontmatter == {"week": "2026-W43"}
    none = render.proposals_note([], None).regions[0][1]
    assert none.startswith("No proposals right now.")
    one = render.proposals_note(
        [(1, "Courses/C/C.md", "Argus has a new version of core", "-a\n+b\n")], "http://pi:8000"
    ).regions[0][1]
    assert "Argus wants to change 1 note you edited." in one and "(http://pi:8000/proposals)" in one
    assert "```diff\n-a\n+b\n```" in one
    two = render.proposals_note([(1, "a.md", "r", "d"), (2, "b.md", "r", "d")], None).regions[0][1]
    assert "2 notes" in two and "dashboard's Proposals page" in two
