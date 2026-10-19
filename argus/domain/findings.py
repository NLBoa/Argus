"""Announcement scanning: the deterministic pre-filter, and the rules for AI findings.

The AI extracts candidate findings; everything here is deterministic code that decides
whether a finding is trustworthy, which item it refers to, and what happens to it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from argus.ai.schemas import FindingOut
from argus.clock import LOCAL_TZ, to_local
from argus.domain.models import AuthorRole, ItemKind, SourceItem, SourceKey
from argus.domain.titles import infer_kind, normalize_title

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


# ---------------------------------------------------------------- findings

HIGH_CONFIDENCE = 0.8
SAME_TIME = timedelta(minutes=5)
DATE_WINDOW = (timedelta(days=7), timedelta(days=200))  # plausible: [posted - 7d, posted + 200d]
_DATE_WORDS = re.compile(
    r"\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|rsday|urday|sday)?\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}(?:st|nd|rd|th)\b"
    r"|\b(?:today|tonight|tomorrow|yesterday|next week|this week|next class|end of (?:the )?week)\b"
    r"|\bin (?:a|one|two|three|four|\d+) (?:days?|weeks?)\b",
    re.IGNORECASE,
)
_QUOTES = str.maketrans(
    {
        chr(0x2018): "'",
        chr(0x2019): "'",
        chr(0x201C): '"',
        chr(0x201D): '"',
        chr(0x2013): "-",
        chr(0x2014): "-",
    }
)


@dataclass(frozen=True, slots=True)
class PostContext:
    course_code: str
    author_role: AuthorRole
    pinned: bool
    posted_at: datetime
    title: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    course_code: str
    title: str
    date: datetime | None  # UTC; a date-only finding is 23:59 local that day
    date_only: bool
    scope: str | None
    quote: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ItemRef:
    id: int
    course_code: str
    title: str
    kind: ItemKind
    due_at: datetime | None
    official: bool  # has a Canvas or Gradescope record


class Action(StrEnum):
    APPLY_NEW = "apply_new"  # create an "Announced" item
    APPLY_DATE = "apply_date"  # set the announced date on an item with no official date
    APPLY_INFO = "apply_info"  # attach scope or logistics text
    LINK = "link"  # agrees with what is known: nothing changes
    HOLD = "hold"  # wait for the student to confirm
    CONFLICT = "conflict"  # disagrees with an official date: the student resolves it
    STORE = "store"  # recorded and shown, no effect


_STATUS = {
    Action.APPLY_NEW: "applied",
    Action.APPLY_DATE: "applied",
    Action.APPLY_INFO: "applied",
    Action.LINK: "linked",
    Action.HOLD: "pending",
    Action.CONFLICT: "conflict",
    Action.STORE: "stored",
}


def status_for(action: Action) -> str:
    return _STATUS[action]


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    item_id: int | None
    reason: str


def _norm_text(s: str) -> str:
    return " ".join(s.translate(_QUOTES).casefold().split())


def has_date_expression(text: str) -> bool:
    return _DATE_WORDS.search(text) is not None


def _parse_date(value: str) -> tuple[datetime, bool]:
    if len(value) == 10:
        d = date.fromisoformat(value)
        return datetime.combine(d, time(23, 59), tzinfo=LOCAL_TZ).astimezone(UTC), True
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC), False


def normalize(out: FindingOut, post: PostContext) -> Finding | None:
    """Apply the code-side guards to one AI finding. None means: discard it."""
    if out.course.upper().replace(" ", "") != post.course_code:
        return None
    if _norm_text(out.quote) not in _norm_text(post.text):
        return None  # the quote is not in the post: the finding is not grounded
    when: datetime | None = None
    date_only = False
    if out.date is not None and has_date_expression(post.text):
        when, date_only = _parse_date(out.date)
        lo, hi = post.posted_at - DATE_WINDOW[0], post.posted_at + DATE_WINDOW[1]
        if not lo <= when <= hi:
            when, date_only = None, False
    return Finding(
        kind=out.kind,
        course_code=post.course_code,
        title=out.title.strip(),
        date=when,
        date_only=date_only,
        scope=(out.scope or "").strip() or None,
        quote=out.quote.strip(),
        confidence=out.confidence,
    )


def _numbers(tokens: list[str]) -> set[str]:
    return {t for t in tokens if t.isdigit()}


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if na == nb:
        return 1.0
    ta, tb = na.split(), nb.split()
    if _numbers(ta) != _numbers(tb):
        return 0.0
    sa, sb = set(ta), set(tb)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def _compatible(f_kind: ItemKind, item_kind: ItemKind) -> bool:
    return f_kind == item_kind or ItemKind.EVENT in (f_kind, item_kind)


def match_item(f: Finding, items: Sequence[ItemRef]) -> ItemRef | None:
    """The item a finding refers to: same course, compatible kind, similar title."""
    f_kind = infer_kind(f.title, ItemKind.ASSIGNMENT)
    best: tuple[float, float, int, ItemRef] | None = None
    for it in items:
        if it.course_code != f.course_code or not _compatible(f_kind, it.kind):
            continue
        score = title_similarity(f.title, it.title)
        if score < 0.6:
            continue
        distance = abs((it.due_at - f.date).total_seconds()) if it.due_at and f.date else 1e12
        key = (-score, distance, it.id, it)
        if best is None or key[:3] < best[:3]:
            best = key
    return best[3] if best else None


def same_due(f: Finding, due_at: datetime | None) -> bool:
    if f.date is None or due_at is None:
        return False
    if f.date_only:
        return to_local(f.date).date() == to_local(due_at).date()
    return abs(f.date - due_at) <= SAME_TIME


def decide(f: Finding, post: PostContext, item: ItemRef | None) -> Decision:
    """The decision table. Official dates are never changed without the student."""
    trusted = post.author_role.is_staff and f.confidence >= HIGH_CONFIDENCE
    iid = item.id if item else None

    if f.kind == "cancellation":
        if item is None:
            return Decision(Action.STORE, None, "Cancellation of something Argus doesn't track")
        return Decision(Action.HOLD, iid, "Cancellations always need your confirmation")
    if f.kind in ("scope", "logistics"):
        if item is None:
            return Decision(Action.STORE, None, "No matching item")
        if trusted:
            return Decision(Action.APPLY_INFO, iid, "From staff, stated plainly")
        return Decision(Action.HOLD, iid, "Not from staff, or hedged")
    if f.kind == "other":
        return Decision(Action.STORE, iid, "Shown for reference")

    # new_assessment and date_change
    if item is None:
        if trusted:
            return Decision(Action.APPLY_NEW, None, "Announced by staff")
        return Decision(Action.HOLD, None, "Not from staff, or hedged")
    if f.date is None or same_due(f, item.due_at):
        return Decision(Action.LINK, iid, "Matches what is already known")
    if item.official:
        return Decision(Action.CONFLICT, iid, "Disagrees with the official date")
    if trusted:
        return Decision(Action.APPLY_DATE, iid, "Updates an announced date")
    return Decision(Action.HOLD, iid, "Not from staff, or hedged")


LINK_WINDOW = timedelta(days=14)


def link_announced(
    records: Sequence[SourceItem], announced: Sequence[ItemRef]
) -> dict[SourceKey, int]:
    """Attach newly seen official records to items that so far exist only from announcements.

    Once Canvas or Gradescope lists the item, its date takes over (official sources win) and
    any disagreement with the announced date is flagged by `resolve_item`. Matching: same
    course, compatible kind, similar title, and (when both have dates) within two weeks.
    Greedy by best score; each record and each item is used at most once.
    """
    candidates: list[tuple[float, float, str, int, SourceItem, ItemRef]] = []
    for it in announced:
        for r in records:
            if r.course_code != it.course_code or not _compatible(r.kind, it.kind):
                continue
            score = title_similarity(r.title, it.title)
            if score < 0.6:
                continue
            gap = abs((r.due_at - it.due_at).total_seconds()) if r.due_at and it.due_at else 0.0
            if gap > LINK_WINDOW.total_seconds():
                continue
            candidates.append((-score, gap, str(r.key), it.id, r, it))
    candidates.sort(key=lambda c: c[:4])
    links: dict[SourceKey, int] = {}
    used_items: set[int] = set()
    for _, _, _, _, r, it in candidates:
        if r.key in links or it.id in used_items:
            continue
        links[r.key] = it.id
        used_items.add(it.id)
    return links
