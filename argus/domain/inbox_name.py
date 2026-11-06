"""File names for the morning planner's Drive inbox (D074). Pure: no I/O.

The planner reads the course, deadline, and depth from the name, so the format is exact:

    <COURSE> · <what> · <type> · by <YYYY-MM-DD HH:MM> · <depth>.<ext>

`by` is local America/New_York wall time. `what` is cleaned so it can never add a separator,
a path, or a line break. The file keeps its own format: the MIME type sent to Drive is the
file's real one, so Drive never converts it to a Google Doc or Slides.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum

from argus.clock import to_local

SEP = " \u00b7 "  # space, middle dot, space
MAX_WHAT = 80
MAX_BYTES = 50 * 1024 * 1024

_COURSE = re.compile(r"^[A-Z]{4}\d{3}[A-Z]?$")
# The middle dot and characters that look like it; "/" and its lookalikes.
_DOTS = "\u00b7\u0387\u2022\u2027\u2219\u22c5\u30fb\uff65"
_SLASHES = "/\u2044\u2215\uff0f"
_TO_SPACE = re.compile(f"[{_DOTS}{_SLASHES}]")
_SPACES = re.compile(r"\s+")


class FileType(StrEnum):
    SLIDES = "slides"
    NOTES = "notes"
    ASSIGNMENT = "assignment"
    EXAM_PREP = "exam-prep"
    READING = "reading"
    OTHER = "other"

    @property
    def label(self) -> str:
        return "Exam prep" if self is FileType.EXAM_PREP else self.value.capitalize()


class Depth(StrEnum):
    DEEP = "deep"
    NORMAL = "normal"
    SKIM = "skim"

    @property
    def label(self) -> str:
        return self.value.capitalize()


_ZIP = b"PK\x03\x04"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_TYPES: Mapping[str, str] = {
    "pdf": "application/pdf",
    "pptx": _PPTX,
    "docx": _DOCX,
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}
KINDS_TEXT = "PDF, PPTX, DOCX, PNG, or JPG"


def clean_what(text: str) -> str:
    """The description as it goes in the name: middle dots, slashes, line breaks, and control
    characters become spaces, invisible format characters go, runs of space collapse, and it is
    cut to MAX_WHAT characters. May return "" (the caller says what's missing)."""
    out = unicodedata.normalize("NFC", text)
    out = _TO_SPACE.sub(" ", out)
    out = "".join(_clean_char(c) for c in out)
    out = _SPACES.sub(" ", out).strip()
    return unicodedata.normalize("NFC", out[:MAX_WHAT].rstrip())


def _clean_char(c: str) -> str:
    category = unicodedata.category(c)
    if category == "Cf":  # zero-width and other invisible format characters
        return ""
    if category in ("Cc", "Zl", "Zp"):  # control characters, line and paragraph separators
        return " "
    return c


def build_stem(
    *,
    course: str,
    what: str,
    file_type: FileType | str,
    by: datetime,
    depth: Depth | str,
) -> str:
    """The name without its extension. Raises ValueError on a bad part."""
    code = course.upper().replace(" ", "")
    if not _COURSE.match(code):
        raise ValueError(f"course must look like STAT400, got {course!r}")
    text = clean_what(what)
    if not text:
        raise ValueError("say what the file is")
    kind = FileType(file_type)
    level = Depth(depth)
    return SEP.join((code, text, kind.value, format_by(by), level.value))


def format_by(by: datetime) -> str:
    """ "by 2026-11-09 09:00": local wall time, to the minute. Rejects naive datetimes."""
    return "by " + to_local(by).strftime("%Y-%m-%d %H:%M")


def build_name(
    *,
    course: str,
    what: str,
    file_type: FileType | str,
    by: datetime,
    depth: Depth | str,
    ext: str,
) -> str:
    """The full file name. Raises ValueError on a bad part."""
    suffix = ext.lower()
    if suffix not in MIME_TYPES:
        raise ValueError(f"{KINDS_TEXT} files only, not .{ext}")
    stem = build_stem(course=course, what=what, file_type=file_type, by=by, depth=depth)
    return f"{stem}.{suffix}"


def extension_of(filename: str) -> str:
    """The lowercase extension of an uploaded file's name, if Argus accepts it."""
    base = re.split(r"[\\/]", filename)[-1]
    if "." not in base[1:]:
        raise ValueError(f"{KINDS_TEXT} files only")
    ext = base.rsplit(".", 1)[1].lower()
    if ext not in MIME_TYPES:
        raise ValueError(f"{KINDS_TEXT} files only, not .{ext}")
    return ext


def mime_type(ext: str) -> str:
    return MIME_TYPES[ext.lower()]


def content_matches(ext: str, head: bytes) -> bool:
    """Whether the file's first bytes fit its extension (a renamed file is refused)."""
    match ext.lower():
        case "pdf":
            return b"%PDF-" in head[:1024]
        case "pptx" | "docx":
            return head.startswith(_ZIP)
        case "png":
            return head.startswith(b"\x89PNG\r\n\x1a\n")
        case "jpg" | "jpeg":
            return head.startswith(b"\xff\xd8\xff")
    return False
