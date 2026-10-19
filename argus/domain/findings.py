"""Announcement scanning: the deterministic pre-filter (Phase 1), plus finding rules (Phase 4)."""

from __future__ import annotations

import re

PREFILTER_KEYWORDS = (
    "quiz",
    "exam",
    "midterm",
    "final",
    "due",
    "extension",
    "extended",
    "cancel",
    "covers",
    "cheat sheet",
)
_KEYWORDS = re.compile(
    r"\b(?:" + "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in PREFILTER_KEYWORDS) + r")",
    re.IGNORECASE,
)


def matches_keywords(text: str) -> bool:
    """True if the text mentions anything that may change what the student needs to do.

    Matches word starts, so "cancelled", "exams", and "covers" all count.
    """
    return _KEYWORDS.search(text) is not None
