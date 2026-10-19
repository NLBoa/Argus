"""Title normalization (for merging) and kind inference (for records that do not say)."""

from __future__ import annotations

import re
import unicodedata

from argus.domain.models import ItemKind

_TRAILING_CONTEXT = re.compile(r"\s*\[[^\[\]]*\]\s*$")  # Canvas iCal appends "[CMSC351-0101-…]"
_COURSE_CODE = re.compile(r"\b[a-z]{4}\s?\d{3}[a-z]?\b")
_LETTER_DIGIT = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_LEADING_ZEROS = re.compile(r"\b0+(?=\d)")

# Applied to the space-separated token stream; multi-word phrases first.
_SYNONYMS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bproblem set\b"), "ps"),
    (re.compile(r"\b(?:homework|hw|hwk)\b"), "hw"),
    (re.compile(r"\b(?:pset|ps)\b"), "ps"),
    (re.compile(r"\b(?:proj|project)\b"), "project"),
    (re.compile(r"\b(?:qz|quiz)\b"), "quiz"),
    (re.compile(r"\b(?:no|num|number)\b(?= \d)"), ""),
]

_EXAM = re.compile(r"\b(?:exam|midterm|final)\b")
_FINAL_NOT_EXAM = re.compile(r"\bfinal\b(?!\s*(?:exam\b|$))")
_NOT_AN_ASSESSMENT = re.compile(
    r"\b(?:review|session|office hours?|study guide|practice|solutions?|worksheet)\b"
)
_QUIZ = re.compile(r"\bquiz(?:zes)?\b")


def clean_display_title(title: str) -> str:
    """Strip the Canvas iCal course-context suffix, keeping the human title."""
    return _TRAILING_CONTEXT.sub("", title).strip()


def normalize_title(title: str) -> str:
    """Canonical form for equality matching across sources.

    NFKC, casefold, drop a trailing [context], drop course codes and punctuation,
    split letter-digit runs (hw3 -> hw 3), strip leading zeros, unify common synonyms.
    """
    s = unicodedata.normalize("NFKC", title).casefold()
    s = _TRAILING_CONTEXT.sub("", s)
    s = _NON_WORD.sub(" ", s)
    s = _COURSE_CODE.sub(" ", s)
    s = _LETTER_DIGIT.sub(" ", s)
    s = _LEADING_ZEROS.sub("", s)
    s = " ".join(s.split())
    for pattern, repl in _SYNONYMS:
        s = pattern.sub(repl, s)
    return " ".join(s.split())


def infer_kind(title: str, default: ItemKind) -> ItemKind:
    """Infer quiz/exam from the title; otherwise keep the source's default."""
    s = " ".join(_NON_WORD.sub(" ", title.casefold()).split())
    if _NOT_AN_ASSESSMENT.search(s):
        return default
    if _EXAM.search(s) and not _FINAL_NOT_EXAM.search(s):
        return ItemKind.EXAM
    if _QUIZ.search(s):
        return ItemKind.QUIZ
    return default
