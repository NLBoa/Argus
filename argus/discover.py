"""Find the student's courses for config.yaml: codes, sections, and ids.

The ELMS feed tags events "[CMSC216-0101-Fall2026]" and links to /courses/<canvas id>; Gradescope
and Piazza list the student's own classes, whose names contain the course code. Read-only: it
logs in and lists, exactly as the collectors do. `argus discover` prints what it found and,
with --config, the current config with its `courses:` block replaced.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from icalendar import Calendar

_TAG = re.compile(r"\[([A-Z]{4})\s?(\d{3}[A-Z]?)(?:-(\d{4}))?([^\]]*)")
_TERM = re.compile(r"(winter|spring|summer|fall)\s?(20\d\d)", re.IGNORECASE)
_COURSE_ID = re.compile(r"(?:include_contexts=course_|/courses/)(\d+)")
_CODE = re.compile(r"\b([A-Za-z]{4})\s?-?(\d{3}[A-Za-z]?)\b")
ACCENTS = ("indigo", "teal", "plum", "ochre", "rose", "sky", "olive", "slate")


@dataclass(frozen=True, slots=True)
class Found:
    code: str
    section: str | None
    canvas_id: int | None
    gradescope_id: str | None
    piazza_network_id: str | None


FeedCourse = tuple[str | None, int | None, str | None]


def feed_courses(body: bytes) -> dict[str, FeedCourse]:
    """code -> (section, canvas id, term like "Fall 2026"), by the most common value seen."""
    sections: dict[str, Counter[str]] = {}
    ids: dict[str, Counter[int]] = {}
    terms: dict[str, Counter[str]] = {}
    for ev in Calendar.from_ical(body).walk("VEVENT"):
        tag = _TAG.search(str(ev.get("SUMMARY", "")))
        if tag is None:
            continue
        code = (tag.group(1) + tag.group(2)).upper()
        sections.setdefault(code, Counter())
        ids.setdefault(code, Counter())
        terms.setdefault(code, Counter())
        if (t := _TERM.search(tag.group(4))) is not None:
            terms[code][f"{t.group(1).capitalize()} {t.group(2)}"] += 1
        if tag.group(3):
            sections[code][tag.group(3)] += 1
        if (m := _COURSE_ID.search(str(ev.get("URL", "")))) is not None:
            ids[code][int(m.group(1))] += 1
    return {
        code: (
            sections[code].most_common(1)[0][0] if sections[code] else None,
            ids[code].most_common(1)[0][0] if ids[code] else None,
            terms[code].most_common(1)[0][0] if terms[code] else None,
        )
        for code in sorted(sections)
    }


def current_term(
    courses: dict[str, FeedCourse],
) -> tuple[dict[str, FeedCourse], list[str]]:
    """Only the latest term's courses: the feed still carries finished ones. Returns those
    and the ones left out ("MATH140 (Spring 2026)"). A course whose tag has no term stays."""
    ranks = [term_rank(t) for _, _, t in courses.values() if t]
    if not ranks:
        return courses, []
    latest = max(ranks)
    kept = {c: v for c, v in courses.items() if v[2] is None or term_rank(v[2]) == latest}
    return kept, [f"{c} ({v[2]})" for c, v in courses.items() if c not in kept]


def match_code(name: str) -> str | None:
    m = _CODE.search(name)
    return (m.group(1) + m.group(2)).upper() if m else None


_TERMS = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def term_rank(term: str) -> tuple[int, int]:
    """'Fall 2026' -> (2026, 3), so the latest term sorts last. Unknown terms sort first."""
    year = re.search(r"(20\d\d)", term)
    season = next((v for k, v in _TERMS.items() if k in term.casefold()), -1)
    return (int(year.group(1)) if year else 0, season)


def pick(
    codes: Sequence[str], listed: Sequence[tuple[str, str, str]]
) -> tuple[dict[str, str], list[str]]:
    """For each code, the id of the latest-term class whose name carries it. Also returns the
    codes listed there that aren't among `codes` (not in the ELMS feed), for the student to see."""
    best: dict[str, tuple[tuple[int, int], str]] = {}
    others: set[str] = set()
    for ident, name, term in listed:
        code = match_code(name)
        if code is None:
            continue
        if code not in codes:
            others.add(code)
            continue
        rank = term_rank(term)
        if code not in best or rank > best[code][0]:
            best[code] = (rank, ident)
    return {code: ident for code, (_, ident) in best.items()}, sorted(others)


def render_courses(found: Sequence[Found]) -> str:
    lines = ["courses:"]
    for n, f in enumerate(found):
        lines.append(f"  - code: {f.code}")
        if f.section:
            lines.append(f'    section: "{f.section}"')
        else:
            lines.append(
                '    section: "0101"  # not in the ELMS feed: check your section on Testudo'
            )
        if f.canvas_id is not None:
            lines.append(f"    canvas_id: {f.canvas_id}")
        if f.gradescope_id:
            lines.append(f'    gradescope_id: "{f.gradescope_id}"')
        if f.piazza_network_id:
            lines.append(f'    piazza_network_id: "{f.piazza_network_id}"')
        lines.append(f"    accent: {ACCENTS[n % len(ACCENTS)]}")
    return "\n".join(lines) + "\n"


def replace_courses(config_text: str, block: str) -> str:
    """The config with its top-level `courses:` block swapped for `block` (other lines untouched)."""
    lines = config_text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("courses:")), None)
    if start is None:
        return block + config_text
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t-"):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1  # keep the blank line before the next section
    return "".join(lines[:start]) + block + "".join(lines[end:])
