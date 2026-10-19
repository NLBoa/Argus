"""Note rendering (pure): data in, `DesiredNote` out. Wording follows docs/design.md.

Concept notes follow the study template: the idea with citations, a picture, a worked
example, why it works, common mistakes, and "Test yourself" questions folded as callouts.
After the Argus region come two sections Argus never writes again: "Explain it yourself"
(self-explanation) and "My notes".
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from argus.clock import to_local
from argus.vault import layout
from argus.vault.ownership import DesiredNote

CONCEPT_TAIL = (
    "\n## Explain it yourself\n\n"
    "_Close the note above and explain the idea in your own words. Then check what you missed._\n\n"
    "## My notes\n"
)
PLAIN_TAIL = "\n## My notes\n"
_ID_LINE = re.compile(r"^<!--ID: (\d+)-->$")


def _when(dt: datetime | None) -> str:
    if dt is None:
        return "no due date"
    local = to_local(dt)
    hour = local.hour % 12 or 12
    minute = f":{local.minute:02d}" if local.minute else ""
    return f"{local:%a %b} {local.day}, {hour}{minute} {'am' if local.hour < 12 else 'pm'}"


def _cites(cites: Sequence[str]) -> str:
    return f" ({'; '.join(cites)})" if cites else ""


def _claim_line(claim: Mapping[str, Any]) -> str:
    flag = "" if claim.get("verified", True) else " _(source unconfirmed)_"
    return f"{claim['text']}{_cites(claim.get('cites', []))}{flag}"


@dataclass(frozen=True)
class ConceptData:
    course: str
    module: str | None
    name: str
    body: Mapping[str, Any]
    sources: Sequence[Mapping[str, str]]
    verified_at: datetime | None
    practice: Sequence[tuple[str, str, str]]  # (prompt, answer, source ref)
    related: Sequence[str] = field(default=())  # note paths


def concept_note(c: ConceptData) -> DesiredNote:
    parts = ["**The idea.** " + " ".join(_claim_line(x) for x in c.body.get("idea", []))]
    if c.body.get("diagram"):
        parts.append(f"## Picture\n\n```mermaid\n{c.body['diagram']}\n```")
    if c.body.get("worked_example"):
        steps = "\n".join(
            f"{n}. {_claim_line(x)}" for n, x in enumerate(c.body["worked_example"], start=1)
        )
        parts.append(f"## Worked example\n\n{steps}")
    if c.body.get("why_it_works"):
        parts.append(
            "## Why it works\n\n" + " ".join(_claim_line(x) for x in c.body["why_it_works"])
        )
    if c.body.get("common_mistakes"):
        parts.append(
            "## Common mistakes\n\n"
            + "\n".join(f"- {_claim_line(x)}" for x in c.body["common_mistakes"])
        )
    if c.practice:
        qs = "\n\n".join(
            f"> [!question]- {prompt}\n> {answer}\n>\n> _Source: {ref}_"
            for prompt, answer, ref in c.practice
        )
        parts.append(f"## Test yourself\n\n{qs}")
    if c.related:
        parts.append(
            "**Related:** "
            + ", ".join(layout.link(p, p.rsplit("/", 1)[-1][:-3]) for p in c.related)
        )
    fm: dict[str, Any] = {
        "course": c.course,
        "module": c.module or "",
        "sources": [s["ref"] for s in c.sources],
        "verified": to_local(c.verified_at).date().isoformat() if c.verified_at else False,
    }
    return DesiredNote(fm, (("core", "\n\n".join(parts)),), CONCEPT_TAIL)


@dataclass(frozen=True)
class ItemLine:
    title: str
    kind: str
    due_at: datetime | None
    status: str
    note: str | None  # vault path of its note
    url: str | None


def _item_line(i: ItemLine) -> str:
    name = layout.link(i.note, i.title) if i.note else i.title
    done = (
        " (done)"
        if i.status in ("submitted", "graded")
        else " (not marked done)"
        if i.status == "not_marked_done"
        else ""
    )
    return f"- {name}, {i.kind}, due {_when(i.due_at)}{done}"


@dataclass(frozen=True)
class CourseData:
    code: str
    title: str | None
    meetings: Sequence[str]
    links: Sequence[tuple[str, str]]
    items: Sequence[ItemLine]
    modules: Sequence[tuple[str, str]]  # (name, note path)
    exams: Sequence[tuple[str, str]]  # (title, note path)
    grade: str | None
    concept_map: bool = True  # study notes can exist for this course (D055)


def course_note(c: CourseData) -> DesiredNote:
    parts = [f"# {c.code}{': ' + c.title if c.title else ''}"]
    if c.meetings:
        parts.append("**Meets:** " + "; ".join(c.meetings))
    if c.links:
        parts.append(", ".join(f"[{label}]({url})" for label, url in c.links))
    if c.grade:
        parts.append(f"**Current grade:** {c.grade}")
    upcoming = [i for i in c.items if i.status not in ("submitted", "graded")]
    parts.append(
        "## Coming up\n\n"
        + ("\n".join(_item_line(i) for i in upcoming) or "Nothing open right now.")
    )
    if c.exams:
        parts.append("## Exam prep\n\n" + "\n".join(f"- {layout.link(p, t)}" for t, p in c.exams))
    if c.modules:
        parts.append("## Modules\n\n" + "\n".join(f"- {layout.link(p, n)}" for n, p in c.modules))
    if c.concept_map:
        parts.append(f"Concept map: {layout.link(layout.concept_map(c.code), 'Concept map')}")
    return DesiredNote({"course": c.code}, (("core", "\n\n".join(parts)),), PLAIN_TAIL)


def module_note(
    course: str,
    name: str,
    materials: Sequence[tuple[str, str]],
    concepts: Sequence[str],
) -> DesiredNote:
    parts = []
    parts.append(
        "## Materials\n\n"
        + ("\n".join(f"- [{t}]({u})" for t, u in materials) or "No files in this module yet.")
    )
    if concepts:
        parts.append(
            "## Concepts\n\n"
            + "\n".join(f"- {layout.link(p, p.rsplit('/', 1)[-1][:-3])}" for p in concepts)
        )
    return DesiredNote(
        {"course": course, "module": name}, (("core", "\n\n".join(parts)),), PLAIN_TAIL
    )


@dataclass(frozen=True)
class AssignmentData:
    course: str
    title: str
    kind: str
    due_at: datetime | None
    status: str
    estimate: str | None
    links: Sequence[tuple[str, str]]
    scope: str | None
    logistics: str | None


def assignment_note(a: AssignmentData) -> DesiredNote:
    """Logistics only. Argus never drafts answers or solutions to graded work."""
    lines = [f"**Due:** {_when(a.due_at)}", f"**Status:** {a.status.replace('_', ' ')}"]
    if a.estimate:
        lines.append(f"**Estimate:** {a.estimate}")
    if a.scope:
        lines.append(f"**Covers:** {a.scope}")
    if a.logistics:
        lines.append(f"**Logistics:** {a.logistics}")
    if a.links:
        lines.append("**Open in:** " + ", ".join(f"[{label}]({url})" for label, url in a.links))
    fm = {
        "course": a.course,
        "kind": a.kind,
        "due": to_local(a.due_at).isoformat() if a.due_at else None,
    }
    return DesiredNote(fm, (("core", "\n".join(lines)),), "\n## My plan\n\n## Notes\n")


@dataclass(frozen=True)
class ExamConcept:
    name: str
    note: str
    readiness: str


@dataclass(frozen=True)
class ExamData:
    course: str
    title: str
    at: datetime | None
    scope: str | None
    logistics: str | None
    concepts: Sequence[ExamConcept]
    sessions: Sequence[tuple[datetime, str]]  # (start, label)
    practice: Sequence[tuple[str, str, str]]  # (prompt, answer, concept)
    study: bool = True  # concept notes can exist; without them the study regions stay empty


def exam_note(e: ExamData) -> DesiredNote:
    counts = {
        r: sum(1 for c in e.concepts if c.readiness == r) for r in ("solid", "shaky", "untested")
    }
    scope = [f"**When:** {_when(e.at)}"]
    if e.scope:
        scope.append(f"**Covers:** {e.scope}")
    if e.logistics:
        scope.append(f"**Logistics:** {e.logistics}")
    if e.study:
        scope.append(
            f"**Readiness:** {counts['solid']} solid, {counts['shaky']} shaky, {counts['untested']} untested"
        )
    if e.concepts:
        scope.append(
            "\n| Concept | Readiness |\n|---|---|\n"
            + "\n".join(f"| {layout.link(c.note, c.name)} | {c.readiness} |" for c in e.concepts)
        )
    plan = (
        "\n".join(f"- {_when(at)}: {label}" for at, label in e.sessions)
        or "Review sessions appear here once concepts exist."
    )
    if e.practice:
        qs = "\n\n".join(
            f"**{n}.** {prompt}\n\n> [!check]- Answer\n> {answer}\n> _Concept: {concept}_"
            for n, (prompt, answer, concept) in enumerate(e.practice, start=1)
        )
        practice = "Answer each question on paper first, then open the answer.\n\n" + qs
    else:
        practice = "Practice questions appear here once concept notes are verified."
    fm = {
        "course": e.course,
        "exam": e.title,
        "date": to_local(e.at).date().isoformat() if e.at else None,
    }
    # Without study notes (no course files or no AI, D055) the plan and practice regions are
    # left empty: the markers stay, so the regions exist if study notes arrive later.
    regions = (
        ("scope", "\n".join(scope)),
        ("plan", "## Review plan\n\n" + plan if e.study or e.sessions else ""),
        ("practice", "## Practice exam\n\n" + practice if e.study else ""),
    )
    return DesiredNote(fm, regions, PLAIN_TAIL)


def existing_card_ids(text: str | None) -> dict[str, str]:
    """front text -> Anki note id, from blocks the Obsidian_to_Anki plugin has annotated."""
    if not text:
        return {}
    ids: dict[str, str] = {}
    for block in re.findall(r"^START\n(.*?)^END$", text, flags=re.MULTILINE | re.DOTALL):
        lines = block.strip("\n").split("\n")
        if len(lines) < 2:
            continue
        front = lines[1]
        for line in lines:
            m = _ID_LINE.match(line)
            if m:
                ids[front] = m.group(1)
    return ids


def flashcards_note(
    course: str, cards: Sequence[tuple[str, str, str]], current: str | None
) -> DesiredNote:
    """Cards in Obsidian_to_Anki's format (see docs/anki.md). Known Anki ids are kept."""
    ids = existing_card_ids(current)

    def one_line(s: str) -> str:
        return " ".join(s.split())

    blocks = []
    for prompt, answer, tag in cards:
        front = one_line(prompt)
        lines = [
            "START",
            "Basic",
            front,
            f"Back: {one_line(answer)}",
            f"Tags: argus {course} {tag}",
        ]
        if front in ids:
            lines.append(f"<!--ID: {ids[front]}-->")
        lines.append("END")
        blocks.append("\n".join(lines))
    body = f"TARGET DECK: Argus::{course}\n\n" + (
        "\n\n".join(blocks) if blocks else "No cards yet."
    )
    return DesiredNote({"course": course}, (("cards", body),), PLAIN_TAIL)


def concept_map_canvas(
    nodes: Sequence[tuple[str, str, int]], edges: Sequence[tuple[str, str]]
) -> str:
    """JSON Canvas 1.0: one file node per concept note, grouped in columns by module order."""
    columns: dict[int, list[str]] = {}
    for path, _, column in nodes:
        columns.setdefault(column, []).append(path)
    out_nodes: list[dict[str, Any]] = []
    ids: dict[str, str] = {}
    for col, paths in sorted(columns.items()):
        for row, path in enumerate(sorted(paths)):
            node_id = f"n{len(out_nodes) + 1}"
            ids[path] = node_id
            out_nodes.append(
                {
                    "id": node_id,
                    "type": "file",
                    "file": path,
                    "x": col * 320,
                    "y": row * 120,
                    "width": 260,
                    "height": 80,
                }
            )
    out_edges = [
        {"id": f"e{n}", "fromNode": ids[a], "toNode": ids[b]}
        for n, (a, b) in enumerate(sorted(set(edges)), start=1)
        if a in ids and b in ids
    ]
    return json.dumps({"nodes": out_nodes, "edges": out_edges}, indent=2) + "\n"


def review_today(
    day: date,
    sessions: Sequence[tuple[datetime, datetime, str, str | None]],
    due: Sequence[ItemLine],
) -> DesiredNote:
    plan = (
        "\n".join(
            f"- {_when(start).split(', ', 1)[1]} to {_when(end).split(', ', 1)[1]}: {label}"
            + (f" ({layout.link(note)})" if note else "")
            for start, end, label, note in sessions
        )
        or "No sessions planned today."
    )
    due_lines = "\n".join(_item_line(i) for i in due) or "Nothing due today."
    body = f"# {day:%A, %B} {day.day}\n\n## Sessions\n\n{plan}\n\n## Due today\n\n{due_lines}"
    return DesiredNote({"date": day.isoformat()}, (("today", body),), "")


def weekly_note(day: date, due: Sequence[ItemLine], changes: Sequence[str]) -> DesiredNote:
    year, week, _ = day.isocalendar()
    summary = "## This week\n\n" + (
        "\n".join(_item_line(i) for i in due) or "Nothing due this week."
    )
    log = "## Changes Argus made in notes you opened to it\n\n" + (
        "\n".join(f"- {c}" for c in changes) or "None."
    )
    return DesiredNote(
        {"week": f"{year}-W{week:02d}"},
        (("summary", summary), ("changes", log)),
        "\n## Reflection\n",
    )


def proposals_note(
    proposals: Sequence[tuple[int, str, str, str]], dashboard: str | None
) -> DesiredNote:
    """(id, path, reason, diff) for each open proposal."""
    where = (
        f"Approve or reject them in the dashboard: [Proposals]({dashboard}/proposals)."
        if dashboard
        else "Approve or reject them on the dashboard's Proposals page."
    )
    if not proposals:
        body = "No proposals right now. Argus asks here before changing anything you wrote."
    else:
        body = (
            f"Argus wants to change {len(proposals)} note{'s' if len(proposals) != 1 else ''} you edited. {where}\n\n"
            + "\n\n".join(
                f"### {layout.link(path)}\n\n{reason}.\n\n```diff\n{diff.rstrip()}\n```"
                for _, path, reason, diff in proposals
            )
        )
    return DesiredNote({}, (("proposals", body),), "")


INBOX_README_TEXT = """# Confusion inbox

Drop anything you're confused about here: a line in any note in this folder, or a whole
file. Argus matches each line to a concept, adds a few practice questions aimed at it, and
moves that concept up in your review plan. Argus never edits or deletes what you put here.
"""
