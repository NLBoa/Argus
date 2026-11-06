"""Turn course files into text per page or slide, in memory. Nothing is written to disk.

Canvas materials use `extract` (one line of text per page). Files for the planner inbox (D077)
use `extract_inbox`, which keeps line breaks, caps its work, and never raises."""

from __future__ import annotations

import io
import re
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, BinaryIO
from xml.parsers import expat

from argus.collectors.text import html_to_text
from argus.domain.inbox_companion import Extracted, Extraction, Section, Unit
from argus.domain.models import MaterialPage
from argus.logging import get_logger

if TYPE_CHECKING:
    from pptx.shapes.base import BaseShape
    from pptx.slide import Slide

log = get_logger(__name__)

MAX_BYTES = 30 * 1024 * 1024
PDF_TYPES = ("application/pdf",)
PPTX_TYPES = ("application/vnd.openxmlformats-officedocument.presentationml.presentation",)


def kind_for(content_type: str, filename: str) -> str | None:
    name = filename.lower()
    if content_type in PDF_TYPES or name.endswith(".pdf"):
        return "pdf"
    if content_type in PPTX_TYPES or name.endswith(".pptx"):
        return "pptx"
    return None


def pdf_pages(data: bytes) -> tuple[MaterialPage, ...]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for n, page in enumerate(reader.pages, start=1):
        text = " ".join((page.extract_text() or "").split())
        if text:
            out.append(MaterialPage(f"page {n}", text))
    return tuple(out)


def pptx_slides(data: bytes) -> tuple[MaterialPage, ...]:
    from pptx import Presentation

    deck = Presentation(io.BytesIO(data))
    out = []
    for n, slide in enumerate(deck.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.extend(p.text for p in shape.text_frame.paragraphs if p.text.strip())
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"(speaker notes) {notes}")
        text = " ".join(" ".join(parts).split())
        if text:
            out.append(MaterialPage(f"slide {n}", text))
    return tuple(out)


def extract(kind: str, data: bytes) -> tuple[MaterialPage, ...]:
    if len(data) > MAX_BYTES:
        raise ValueError("file too large to extract")
    if kind == "pdf":
        return pdf_pages(data)
    if kind == "pptx":
        return pptx_slides(data)
    raise ValueError(f"cannot extract {kind}")


def page_text(html: str | None) -> tuple[MaterialPage, ...]:
    text = html_to_text(html)
    return (MaterialPage("page", text),) if text else ()


# ---------------------------------------------------------------- the planner inbox (D077)
#
# A file the student adds (D074) gets a text companion. Such files come from anywhere, so this
# reader is defensive: it reads from the spooled upload rather than a copy in memory, caps
# pages, characters, and time (checked between pages: a Pi 4B is slow), refuses a zip that
# unpacks to too much and a Word XML part with a DTD, and turns any failure into an
# `Extraction` with status none. It never raises, and it never logs text from the file.

IMAGE_REASON = "image — read the file itself"
SCAN_REASON = "no text layer (a scanned PDF?) — read the file itself"
PAGE_LIMIT = "page limit"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main "
_MC_FALLBACK = "http://schemas.openxmlformats.org/markup-compatibility/2006 Fallback"
_DOCX_PAGES = re.compile(rb"<(?:\w+:)?Pages>\s*(\d{1,6})\s*</")
_READ = 256 * 1024
UNITS: Mapping[str, Unit] = {
    "pdf": Unit.PAGE,
    "pptx": Unit.SLIDE,
    "docx": Unit.DOCUMENT,
    "png": Unit.IMAGE,
    "jpg": Unit.IMAGE,
    "jpeg": Unit.IMAGE,
}


@dataclass(frozen=True, slots=True)
class Limits:
    pages: int = 300  # pages or slides read
    chars: int = 250_000  # characters of text kept, in all
    seconds: float = 30.0  # checked between pages (and every 200 Word paragraphs)
    unzipped: int = 200 * 1024 * 1024  # a DOCX or PPTX's parts, unpacked, in all
    entries: int = 5_000  # parts in a DOCX or PPTX


class _RefusedError(Exception):
    """Nothing can be extracted; `reason` says why, for the companion."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Budget:
    def __init__(self, limits: Limits, timer: Callable[[], float]) -> None:
        self.limits = limits
        self._timer = timer
        self._start = timer()
        self.chars = 0
        self.stop: str | None = None  # why reading stopped early

    def more(self, index: int) -> bool:
        """Whether to read page `index` (from 0)."""
        if self.stop is None and index >= self.limits.pages:
            self.stop = PAGE_LIMIT
        if self.stop is None and self._timer() - self._start > self.limits.seconds:
            self.stop = "time limit"
        return self.stop is None

    def take(self, text: str) -> str:
        """`text`, cut to what's left of the character cap."""
        left = self.limits.chars - self.chars
        if len(text) > left:
            text = text[: max(left, 0)]
            self.stop = "text limit"
        self.chars += len(text)
        return text


def extract_inbox(
    ext: str,
    stream: BinaryIO,
    *,
    limits: Limits | None = None,
    timer: Callable[[], float] = time.monotonic,
) -> Extraction:
    """The text of a planner inbox file, per page or slide. `stream` is the whole file and
    seekable. Never raises."""
    unit = UNITS.get(ext.lower(), Unit.IMAGE)
    if unit is Unit.IMAGE:
        return Extraction(unit, Extracted.NONE, IMAGE_REASON)
    budget = _Budget(limits or Limits(), timer)
    try:
        stream.seek(0)
        if unit is Unit.PAGE:
            return _pdf(stream, budget)
        if unit is Unit.SLIDE:
            return _pptx(stream, budget)
        return _docx(stream, budget)
    except _RefusedError as e:
        return Extraction(unit, Extracted.NONE, e.reason)
    except Exception as e:  # a damaged or hostile file can raise anything
        log.warning("inbox extraction failed", extra={"kind": ext, "error": type(e).__name__})
        return Extraction(
            unit, Extracted.NONE, f"Argus couldn't open this {ext.upper()} — read the file itself"
        )


def _finish(unit: Unit, sections: list[Section], total: int, budget: _Budget) -> Extraction:
    noun = unit.value
    failed = sum(s.failed for s in sections)
    if budget.stop is not None:
        why = budget.stop
        if why == PAGE_LIMIT:
            why = f"Argus reads at most {_count(budget.limits.pages, noun)}"
        reason = f"{_count(len(sections), noun)} of {total} extracted ({why})"
        status = Extracted.PARTIAL
    elif failed:
        reason = f"{_count(failed, noun)} of {total} couldn't be read"
        status = Extracted.PARTIAL
    else:
        reason = f"all {_count(total, noun)} read"
        status = Extracted.FULL
    return Extraction(unit, status, reason, total, tuple(sections))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _pdf(stream: BinaryIO, budget: _Budget) -> Extraction:
    from pypdf import PdfReader  # pypdf caps its own decompression

    reader = PdfReader(stream)
    if reader.is_encrypted:
        try:
            opened = bool(reader.decrypt(""))
        except Exception:  # an unsupported cipher is just a locked file
            opened = False
        if not opened:
            raise _RefusedError("the PDF is password-protected — read the file itself")
    total = len(reader.pages)
    if total == 0:
        raise _RefusedError("the PDF has no pages — read the file itself")
    sections: list[Section] = []
    for i in range(total):
        if not budget.more(i):
            break
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as e:  # one bad page shouldn't lose the rest
            log.warning(
                "inbox extraction: a page failed",
                extra={"page": i + 1, "error": type(e).__name__},
            )
            sections.append(Section(i + 1, failed=True))
            continue
        sections.append(Section(i + 1, budget.take(text)))
        if budget.stop is not None:
            break
    if not any(s.text.strip() for s in sections):
        if all(s.failed for s in sections):
            raise _RefusedError("Argus couldn't read the PDF's pages — read the file itself")
        raise _RefusedError(SCAN_REASON)
    return _finish(Unit.PAGE, sections, total, budget)


def _check_zip(z: zipfile.ZipFile, limits: Limits) -> None:
    infos = z.infolist()
    # zipfile stops reading a part at its declared size, so declared sizes bound what is read.
    if len(infos) > limits.entries or sum(i.file_size for i in infos) > limits.unzipped:
        raise _RefusedError(
            "the file unpacks to more than Argus reads safely — read the file itself"
        )


def _pptx(stream: BinaryIO, budget: _Budget) -> Extraction:
    from pptx import Presentation

    with zipfile.ZipFile(stream) as z:
        _check_zip(z, budget.limits)
    stream.seek(0)
    slides = list(Presentation(stream).slides)
    if not slides:
        raise _RefusedError("the deck has no slides — read the file itself")
    sections: list[Section] = []
    for i, slide in enumerate(slides):
        if not budget.more(i):
            break
        text = budget.take("\n".join(_slide_lines(slide)))
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = budget.take(slide.notes_slide.notes_text_frame.text)
        hidden = slide.element.get("show") in ("0", "false")
        sections.append(Section(i + 1, text, notes, hidden=hidden))
        if budget.stop is not None:
            break
    return _finish(Unit.SLIDE, sections, len(slides), budget)


def _slide_lines(slide: Slide) -> Iterator[str]:
    """The slide's text, title first, then each shape in the slide's own order."""
    title = slide.shapes.title
    if title is not None:
        yield from _shape_lines(title)
    for shape in slide.shapes:
        if title is None or shape.shape_id != title.shape_id:
            yield from _shape_lines(shape)


def _shape_lines(shape: BaseShape) -> Iterator[str]:
    from pptx.shapes.graphfrm import GraphicFrame
    from pptx.shapes.group import GroupShape

    if isinstance(shape, GroupShape):
        for inner in shape.shapes:
            yield from _shape_lines(inner)
    elif isinstance(shape, GraphicFrame):
        if shape.has_table:
            for row in shape.table.rows:
                yield " | ".join(" ".join(cell.text.split()) for cell in row.cells)
    elif shape.has_text_frame:
        yield from (p.text for p in shape.text_frame.paragraphs)  # type: ignore[attr-defined]


def _docx(stream: BinaryIO, budget: _Budget) -> Extraction:
    with zipfile.ZipFile(stream) as z:
        _check_zip(z, budget.limits)
        try:
            info = z.getinfo("word/document.xml")
        except KeyError:
            raise _RefusedError("not a Word document — read the file itself") from None
        pages = _docx_pages(z)
        with z.open(info) as part:
            lines = _DocxReader(budget).read(part)
    text = "\n".join(lines).strip()
    if not text:
        raise _RefusedError("the document has no text — read the file itself")
    if budget.stop is not None:
        status, reason = Extracted.PARTIAL, f"the document's text was cut short ({budget.stop})"
    else:
        status, reason = Extracted.FULL, "all text read"
    return Extraction(Unit.DOCUMENT, status, reason, pages, (Section(None, text),))


def _docx_pages(z: zipfile.ZipFile) -> int | None:
    """Word's own page count from `docProps/app.xml`, when it saved one."""
    try:
        info = z.getinfo("docProps/app.xml")
    except KeyError:
        return None
    if info.file_size > 64 * 1024:
        return None
    found = _DOCX_PAGES.search(z.read(info))
    pages = int(found.group(1)) if found else 0
    return pages or None


class _StopError(Exception):
    pass


class _DocxReader:
    """Streams `word/document.xml` through expat: one line per paragraph, in document order
    (table cells and text boxes too, but not a text box's fallback copy, deleted text, or field
    codes). A DTD is refused, so no entity is ever expanded."""

    def __init__(self, budget: _Budget) -> None:
        self.budget = budget
        self.lines: list[str] = []
        self._paras: list[list[str]] = []
        self._in_text = 0
        self._fallback = 0
        self._done = 0

    def read(self, part: IO[bytes]) -> list[str]:
        parser = expat.ParserCreate(namespace_separator=" ")
        parser.StartDoctypeDeclHandler = self._doctype
        parser.EntityDeclHandler = self._doctype
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._chars
        try:
            while chunk := part.read(_READ):
                parser.Parse(chunk, False)
            parser.Parse(b"", True)
        except _StopError:
            pass
        return self.lines

    def _doctype(self, *_: object) -> None:
        raise _RefusedError(
            "the document declares a DTD, which Word never does — read the file itself"
        )

    def _start(self, name: str, attrs: dict[str, str]) -> None:
        if name == _MC_FALLBACK:
            self._fallback += 1
        elif self._fallback:
            return
        elif name == _W + "p":
            self._paras.append([])
        elif not self._paras:
            return
        elif name == _W + "t":
            self._in_text += 1
        elif name == _W + "tab":
            self._paras[-1].append(" ")
        elif name in (_W + "br", _W + "cr"):
            self._paras[-1].append("\n")
        elif name == _W + "numPr" and not self._paras[-1]:
            self._paras[-1].append("- ")

    def _end(self, name: str) -> None:
        if name == _MC_FALLBACK:
            self._fallback -= 1
        elif self._fallback:
            return
        elif name == _W + "t" and self._in_text:
            self._in_text -= 1
        elif name == _W + "p" and self._paras:
            line = "".join(self._paras.pop())
            if line.strip() not in ("", "-"):
                self.lines.append(self.budget.take(line))
            self._done += 1
            if self.budget.stop is not None or (self._done % 200 == 0 and not self.budget.more(0)):
                raise _StopError

    def _chars(self, data: str) -> None:
        if self._in_text and self._paras and not self._fallback:
            self._paras[-1].append(data)
