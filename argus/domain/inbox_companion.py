"""The companion to a planner inbox file (D077): a Markdown copy of its text that a scheduled
Claude task can read without opening the PDF, deck, or document. Pure: no I/O.

Each file the file adder sends (D074) gets a companion in the same Drive folder, named
`<the file's name>.md` so it sorts right after its file. It is YAML frontmatter (format
`schema_version: 1`, every value on one line) and a body:

    # <title> (<COURSE>)
    <one paragraph saying where the text came from>
    ## Page 1 | ## Slide 1 | ## Slide 4 (hidden) | ## Document
    <the text, line by line, in reading order>
    ### Speaker notes
    <a slide's notes>

Lines that start with `#` are only these headings: a line of extracted text that starts with
`#`, or is only `-`, `=`, `*`, or `_` (which Markdown would read as a heading or a rule), gets
a backslash in front.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from argus.clock import LOCAL_TZ, ensure_utc
from argus.domain.inbox_name import format_by

SCHEMA_VERSION = 1
FORMAT = "argus-planner-inbox-companion"
SUFFIX = ".md"
COMPANION_MIME = "text/markdown"

_ESCAPE = re.compile(r"^(#|[-=]+$|[*_]{3,}$)")
_NONCHARACTERS = frozenset("\ufffe\uffff")  # YAML can't hold these, even escaped


class Unit(StrEnum):
    PAGE = "page"  # a PDF
    SLIDE = "slide"  # a PPTX
    DOCUMENT = "document"  # a DOCX: no fixed pages, one section
    IMAGE = "image"  # a PNG or JPG: no text


class Extracted(StrEnum):
    FULL = "full"  # every page or slide was read (some may have no text)
    PARTIAL = "partial"  # stopped early at a limit, or some pages couldn't be read
    NONE = "none"  # no text: an image, a scan, or a file that couldn't be opened


@dataclass(frozen=True, slots=True)
class Section:
    """One page or slide (number from 1), or a whole document (number None)."""

    number: int | None
    text: str = ""
    notes: str = ""  # a slide's speaker notes
    hidden: bool = False  # a hidden slide
    failed: bool = False  # this page couldn't be read


@dataclass(frozen=True, slots=True)
class Extraction:
    unit: Unit
    status: Extracted
    reason: str  # one line, for the reader: "all 12 slides read", "image — read the file itself"
    count: int | None = None  # pages or slides in the file; None when unknown
    sections: tuple[Section, ...] = ()


@dataclass(frozen=True, slots=True)
class CompanionMeta:
    """What the companion says about its file. Datetimes must be aware."""

    file_name: str  # the name in Drive (D074)
    course: str
    title: str  # the student's "what"
    file_type: str
    depth: str
    done_by: datetime
    original_name: str  # the name on the student's device
    mime_type: str
    size_bytes: int
    uploaded_at: datetime
    drive_file_id: str | None = None
    drive_link: str | None = None


def companion_name(file_name: str) -> str:
    """`CMSC351 · … · deep.pdf` → `CMSC351 · … · deep.pdf.md` (sorts right after it)."""
    return file_name + SUFFIX


def utc_iso(dt: datetime) -> str:
    """`2026-10-20T13:30:00Z`: UTC to the second. Rejects naive datetimes."""
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text: str, limit: int = 300) -> str:
    """A frontmatter value: control characters and line breaks become spaces, invisible
    format characters go, runs of space collapse, and it is cut to `limit` characters."""
    out = "".join(_one_line_char(c) for c in unicodedata.normalize("NFC", text))
    return " ".join(out.split())[:limit].rstrip()


def _one_line_char(c: str) -> str:
    category = unicodedata.category(c)
    if category == "Cf" or c in _NONCHARACTERS:  # zero-width and other invisible characters
        return ""
    if category in ("Cc", "Cs", "Zl", "Zp"):  # controls, lone surrogates, line breaks
        return " "
    return c


def clean_text(text: str) -> str:
    """Extracted text as body lines: every kind of line break a newline, each line cleaned
    like `one_line`, at most one blank line in a row, none at either end, and no line that
    Markdown would read as a heading or a rule."""
    lines: list[str] = []
    for raw in unicodedata.normalize("NFC", text).splitlines():
        line = one_line(raw, limit=len(raw))
        if _ESCAPE.match(line):
            line = "\\" + line
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def frontmatter(meta: CompanionMeta, ex: Extraction) -> dict[str, Any]:
    """The frontmatter as data, in the order it is written."""
    return {
        "schema_version": SCHEMA_VERSION,
        "format": FORMAT,
        "file": one_line(meta.file_name, 500),
        "course": one_line(meta.course),
        "title": one_line(meta.title),
        "type": one_line(meta.file_type),
        "depth": one_line(meta.depth),
        "done_by_local": format_by(meta.done_by).removeprefix("by "),
        "done_by_utc": utc_iso(meta.done_by),
        "timezone": str(LOCAL_TZ),
        "original_name": one_line(meta.original_name) or None,
        "mime_type": one_line(meta.mime_type),
        "size_bytes": meta.size_bytes,
        "drive_file_id": one_line(meta.drive_file_id or "") or None,
        "drive_link": one_line(meta.drive_link or "", 500) or None,
        "unit": ex.unit.value,
        "count": ex.count,
        "sections": len(ex.sections) if ex.status is not Extracted.NONE else 0,
        "extracted": ex.status.value,
        "extracted_reason": one_line(ex.reason),
        "uploaded_at": utc_iso(meta.uploaded_at),
    }


def render_companion(meta: CompanionMeta, ex: Extraction) -> str:
    """The companion's full text."""
    # A JSON string is a YAML double-quoted scalar once `one_line` has removed what YAML
    # can't hold, so every string is quoted (no reader takes a date, a time, or `no` for
    # anything but text), and numbers and nulls stay plain.
    head = "".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}\n"
        for key, value in frontmatter(meta, ex).items()
    )
    title = one_line(meta.title) or "Untitled"
    course = one_line(meta.course)
    parts = [f"---\n{head}---", f"# {title} ({course})" if course else f"# {title}"]
    file = one_line(meta.file_name, 500)
    if ex.status is Extracted.NONE:
        parts.append(f"No text was extracted from {file}: {one_line(ex.reason)}.")
    else:
        intro = f"Text Argus extracted from {file}, in reading order."
        if ex.unit is Unit.SLIDE:
            intro += " Speaker notes follow their slide."
        if ex.status is Extracted.PARTIAL:
            intro += f" Not everything was extracted: {one_line(ex.reason)}."
        parts.append(intro)
        parts.extend(_section(s, ex.unit) for s in ex.sections)
    return "\n\n".join(parts) + "\n"


def _section(section: Section, unit: Unit) -> str:
    if section.number is None:
        heading = "## Document"
    else:
        heading = f"## {unit.value.capitalize()} {section.number}"
        if section.hidden:
            heading += " (hidden)"
    body = clean_text(section.text)
    if section.failed:
        body = f"_(Argus couldn't read this {unit.value}.)_"
    elif not body:
        body = f"_(No text on this {unit.value}.)_"
    out = [heading, body]
    notes = clean_text(section.notes)
    if notes:
        out += ["### Speaker notes", notes]
    return "\n\n".join(out)
