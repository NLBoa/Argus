"""Verification of generated study notes (pure). The verifier's verdict, applied by code.

Rules: a claim that is supported with a correct citation is kept; supported but miscited is
kept and marked unverified; unsupported is removed. A practice item is kept only if its
answer is correct. Anything the verifier did not rule on counts as unsupported. A concept
whose core idea does not survive is dropped. The diagram is checked like a claim.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from argus.ai.schemas import ConceptDraft, PracticeDraft, VerificationOut

_SLUG = re.compile(r"[^a-z0-9]+")
_MERMAID_START = re.compile(
    r"^\s*(graph|flowchart|sequenceDiagram|stateDiagram(-v2)?|classDiagram|erDiagram|mindmap)\b"
)


@dataclass(frozen=True, slots=True)
class CheckedClaim:
    text: str
    sources: tuple[str, ...]
    verified: bool  # False: supported, but the citation is wrong


@dataclass(frozen=True, slots=True)
class CheckedPractice:
    kind: str
    prompt: str
    answer: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedConcept:
    name: str
    slug: str
    idea: tuple[CheckedClaim, ...]
    diagram: str | None
    worked_example: tuple[CheckedClaim, ...]
    why_it_works: tuple[CheckedClaim, ...]
    common_mistakes: tuple[CheckedClaim, ...]
    practice: tuple[CheckedPractice, ...]
    related: tuple[str, ...]
    removed_claims: int
    dropped_practice: int

    @property
    def all_sources(self) -> tuple[str, ...]:
        seen: list[str] = []
        for c in (*self.idea, *self.worked_example, *self.why_it_works, *self.common_mistakes):
            seen += [s for s in c.sources if s not in seen]
        for p in self.practice:
            seen += [s for s in p.sources if s not in seen]
        return tuple(seen)


def slugify(name: str) -> str:
    return _SLUG.sub("-", name.casefold()).strip("-")[:60] or "concept"


@dataclass(frozen=True, slots=True)
class Numbered:
    """The note as the verifier sees it: claims C1.., diagram D1, practice P1.."""

    claims: tuple[tuple[str, str, str, tuple[str, ...]], ...]  # (id, section, text, sources)
    practice: tuple[tuple[str, PracticeDraft], ...]


def number(draft: ConceptDraft) -> Numbered:
    claims: list[tuple[str, str, str, tuple[str, ...]]] = []
    n = 0
    for section in ("idea", "worked_example", "why_it_works", "common_mistakes"):
        for c in getattr(draft, section):
            n += 1
            claims.append((f"C{n}", section, c.text, tuple(c.sources)))
    if draft.diagram:
        all_sources = tuple(sorted({s for _, _, _, srcs in claims for s in srcs}))
        claims.append(("D1", "diagram", f"Diagram: {draft.diagram}", all_sources))
    practice = tuple((f"P{i}", p) for i, p in enumerate(draft.practice, start=1))
    return Numbered(tuple(claims), practice)


def valid_mermaid(source: str | None) -> str | None:
    if not source or "```" in source or not _MERMAID_START.match(source):
        return None
    return source.strip()


def apply_verification(
    draft: ConceptDraft, checks: VerificationOut, valid_sources: set[str]
) -> VerifiedConcept | None:
    numbered = number(draft)
    claim_verdicts = {c.id: c for c in checks.claims}
    kept: dict[str, list[CheckedClaim]] = {
        "idea": [],
        "worked_example": [],
        "why_it_works": [],
        "common_mistakes": [],
    }
    diagram: str | None = None
    removed = 0
    for cid, section, text, sources in numbered.claims:
        verdict = claim_verdicts.get(cid)
        cited_known = bool(sources) and all(s in valid_sources for s in sources)
        if verdict is None or not verdict.supported:
            removed += 1
            continue
        if section == "diagram":
            diagram = valid_mermaid(draft.diagram)
            continue
        kept[section].append(
            CheckedClaim(text, sources, verified=verdict.citation_ok and cited_known)
        )
    practice, dropped = kept_practice(draft, checks, valid_sources)
    if not kept["idea"]:
        return None
    return VerifiedConcept(
        name=draft.name.strip(),
        slug=slugify(draft.name),
        idea=tuple(kept["idea"]),
        diagram=diagram,
        worked_example=tuple(kept["worked_example"]),
        why_it_works=tuple(kept["why_it_works"]),
        common_mistakes=tuple(kept["common_mistakes"]),
        practice=tuple(practice),
        related=tuple(r.strip() for r in draft.related if r.strip()),
        removed_claims=removed,
        dropped_practice=dropped,
    )


def kept_practice(
    draft: ConceptDraft, checks: VerificationOut, valid_sources: set[str]
) -> tuple[list[CheckedPractice], int]:
    """Practice items whose answers the verifier confirmed (and whose sources exist)."""
    verdicts = {p.id: p for p in checks.practice}
    kept: list[CheckedPractice] = []
    dropped = 0
    for pid, p in number(draft).practice:
        verdict = verdicts.get(pid)
        if (
            verdict is None
            or not verdict.answer_correct
            or not all(s in valid_sources for s in p.sources)
        ):
            dropped += 1
            continue
        kept.append(CheckedPractice(p.kind, p.prompt.strip(), p.answer.strip(), tuple(p.sources)))
    return kept, dropped


# ---------------------------------------------------------------- graded work (review R10)

_GRADED = re.compile(
    r"\b(home ?works?|hw ?\d+|problem ?sets?|psets?|ps ?\d+|projects?|assignments?|lab reports?|"
    r"take-?home|answer ?keys?|solutions?|submissions?)\b",
    re.IGNORECASE,
)
_STUDY = re.compile(r"\b(practice|review|sample|study guide|worksheet)\b", re.IGNORECASE)
_WORDS = re.compile(r"[a-z0-9]+")


def _title_words(title: str) -> set[str]:
    return set(_WORDS.findall(title.casefold()))


def names_item(text: str, item_titles: Sequence[str]) -> bool:
    """Every word of some tracked item's title appears in the text ("HW 3", "Project 1")."""
    words = _title_words(text)
    return any((t := _title_words(title)) and t <= words for title in item_titles)


def looks_graded(title: str, item_titles: Sequence[str]) -> bool:
    """Material that is (or answers) graded work never feeds note or practice generation.
    Practice and review material is allowed unless it names a tracked item."""
    if names_item(title, item_titles):
        return True
    return bool(_GRADED.search(title)) and not _STUDY.search(title)
