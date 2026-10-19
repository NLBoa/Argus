"""Where things live in the vault, and links into it."""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote

_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def safe(name: str, limit: int = 80) -> str:
    """A file name Obsidian, git, macOS, and Windows all accept."""
    cleaned = " ".join(_UNSAFE.sub(" ", name).split()).strip(" .")
    return cleaned[:limit].rstrip(" .") or "Untitled"


def course_dir(code: str) -> str:
    return f"Courses/{code}"


def course_note(code: str) -> str:
    return f"Courses/{code}/{code}.md"


def concept_map(code: str) -> str:
    return f"Courses/{code}/Concept map.canvas"


def concept_note(code: str, name: str) -> str:
    return f"Courses/{code}/Concepts/{safe(name)}.md"


def module_note(code: str, name: str) -> str:
    return f"Courses/{code}/Modules/{safe(name)}.md"


def assignment_note(code: str, title: str) -> str:
    return f"Courses/{code}/Assignments/{safe(title)}.md"


def exam_note(code: str, title: str) -> str:
    return f"Courses/{code}/Exams/{safe(title)}.md"


def flashcards(code: str) -> str:
    return f"Courses/{code}/Flashcards.md"


REVIEW_TODAY = "Review/Today.md"
PROPOSALS = "Argus/Proposals.md"
INBOX_DIR = "Argus/Inbox"
INBOX_README = "Argus/Inbox/README.md"


def weekly(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"Weekly/{year}-W{week:02d}.md"


def link(rel: str, label: str | None = None) -> str:
    """An Obsidian wikilink by full path (no ambiguity between same-named notes)."""
    target = rel[:-3] if rel.endswith(".md") else rel
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def obsidian_uri(vault: str, rel: str) -> str:
    target = rel[:-3] if rel.endswith(".md") else rel
    return f"obsidian://open?vault={quote(vault)}&file={quote(target)}"
