"""Text from planner inbox files (D077): pages and slides in order, Word paragraphs read with
the standard library, and every limit and hostile file ending in a companion, never an error.
All files are built here from synthetic text."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from argus.collectors.extract_text import (
    IMAGE_REASON,
    SCAN_REASON,
    Limits,
    extract_inbox,
)
from argus.domain.inbox_companion import Extracted, Extraction, Unit
from tests.material_fakes import docx, docx_xml, para, pdf, pptx


class Ticks:
    """A timer that moves `step` seconds each time it is read."""

    def __init__(self, step: float) -> None:
        self.now = -step
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def inbox(ext: str, data: bytes, **limits: Any) -> Extraction:
    timer = limits.pop("timer", Ticks(0))
    return extract_inbox(ext, io.BytesIO(data), limits=Limits(**limits), timer=timer)


# ---------------------------------------------------------------- PDF


def test_pdf_keeps_pages_in_order() -> None:
    ex = inbox("pdf", pdf(["The master theorem", "Case two applies"]))
    assert (ex.unit, ex.status, ex.reason, ex.count) == (
        Unit.PAGE,
        Extracted.FULL,
        "all 2 pages read",
        2,
    )
    assert [(s.number, s.text.strip()) for s in ex.sections] == [
        (1, "The master theorem"),
        (2, "Case two applies"),
    ]


def test_default_limits_read_from_the_start() -> None:
    data = io.BytesIO(pdf(["Page one"]))
    data.seek(0, 2)  # the upload has just been streamed to Drive
    assert extract_inbox("pdf", data).status is Extracted.FULL


def test_scanned_pdf_has_no_text() -> None:
    ex = inbox("pdf", pdf(["", ""]))
    assert (ex.status, ex.reason, ex.sections) == (Extracted.NONE, SCAN_REASON, ())


@pytest.mark.parametrize(
    ("limits", "kept", "reason"),
    [
        ({"pages": 1}, 1, "1 page of 3 extracted (Argus reads at most 1 page)"),
        ({"pages": 2}, 2, "2 pages of 3 extracted (Argus reads at most 2 pages)"),
        ({"chars": 20}, 2, "2 pages of 3 extracted (text limit)"),
        ({"seconds": 25, "timer": Ticks(10)}, 2, "2 pages of 3 extracted (time limit)"),
    ],
)
def test_pdf_limits(limits: dict[str, Any], kept: int, reason: str) -> None:
    cap = limits.get("chars", 10**6)
    ex = inbox("pdf", pdf(["The master theorem", "Case two", "Case three"]), **limits)
    assert (ex.status, ex.reason, ex.count, len(ex.sections)) == (
        Extracted.PARTIAL,
        reason,
        3,
        kept,
    )
    assert sum(len(s.text) for s in ex.sections) <= cap


def test_pdf_page_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PageObject

    real = PageObject.extract_text
    calls = {"n": 0}

    def flaky(self: PageObject, *args: Any, **kw: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RecursionError("deep")
        return real(self, *args, **kw)

    monkeypatch.setattr(PageObject, "extract_text", flaky)
    ex = inbox("pdf", pdf(["One", "Two", "Three"]))
    assert ex.status is Extracted.PARTIAL
    assert ex.reason == "1 page of 3 couldn't be read"
    assert [s.failed for s in ex.sections] == [False, True, False]

    def broken(self: PageObject, *args: Any, **kw: Any) -> str:
        raise ValueError("bad stream")

    monkeypatch.setattr(PageObject, "extract_text", broken)
    ex = inbox("pdf", pdf(["One"]))
    assert ex.status is Extracted.NONE
    assert ex.reason.startswith("Argus couldn't read the PDF's pages")


def _encrypted(password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf(["Locked text"]))))
    w.encrypt(user_password=password, owner_password="owner", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_pdf_locked_open_and_empty() -> None:
    from pypdf import PdfWriter

    locked = inbox("pdf", _encrypted("secret"))
    assert (locked.status, locked.reason) == (
        Extracted.NONE,
        "the PDF is password-protected — read the file itself",
    )
    assert inbox("pdf", _encrypted("")).sections[0].text.strip() == "Locked text"

    empty = io.BytesIO()
    PdfWriter().write(empty)
    assert inbox("pdf", empty.getvalue()).reason.startswith("the PDF has no pages")


def test_pdf_with_a_cipher_pypdf_cant_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PdfReader

    def unsupported(self: PdfReader, password: str) -> int:
        raise NotImplementedError("cipher")

    monkeypatch.setattr(PdfReader, "decrypt", unsupported)
    assert inbox("pdf", _encrypted("")).reason.startswith("the PDF is password-protected")


@pytest.mark.parametrize(
    ("ext", "data"),
    [
        ("pdf", b"%PDF-1.7\n" + bytes(range(256)) * 4),
        ("pptx", b"PK\x03\x04junk"),
        ("docx", b"PK\x03\x04junk"),
    ],
)
def test_damaged_files_never_raise(ext: str, data: bytes) -> None:
    ex = inbox(ext, data)
    assert ex.status is Extracted.NONE
    assert ex.reason.endswith("read the file itself")


@pytest.mark.parametrize("ext", ["png", "jpg", "JPEG"])
def test_images(ext: str) -> None:
    ex = inbox(ext, b"\x89PNG")
    assert (ex.unit, ex.status, ex.reason) == (Unit.IMAGE, Extracted.NONE, IMAGE_REASON)
    assert IMAGE_REASON == "image — read the file itself"


# ---------------------------------------------------------------- PPTX


def _deck() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    d = Presentation()
    one = d.slides.add_slide(d.slide_layouts[5])  # title only
    one.shapes.title.text = "Agenda"
    box = one.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    box.text_frame.text = "First point\nSecond point"
    group = one.shapes.add_group_shape()
    inner = group.shapes.add_textbox(Inches(1), Inches(3), Inches(2), Inches(1))
    inner.text_frame.text = "Grouped"
    table = one.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    for (r, c), text in {(0, 0): "n", (0, 1): "T(n)", (1, 0): "8", (1, 1): "24"}.items():
        table.cell(r, c).text = text
    one.notes_slide.notes_text_frame.text = "Mention the quiz"
    d.slides.add_slide(d.slide_layouts[6])  # blank: a picture-only slide
    three = d.slides.add_slide(d.slide_layouts[5])
    three.shapes.title.text = "Backup"
    three.element.set("show", "0")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_pptx_slides_in_order_with_notes_tables_groups_and_hidden() -> None:
    ex = inbox("pptx", _deck())
    assert (ex.unit, ex.status, ex.reason, ex.count) == (
        Unit.SLIDE,
        Extracted.FULL,
        "all 3 slides read",
        3,
    )
    first, blank, hidden = ex.sections
    assert first.text.splitlines() == [
        "Agenda",
        "First point",
        "Second point",
        "Grouped",
        "n | T(n)",
        "8 | 24",
    ]
    assert first.notes == "Mention the quiz"
    assert (blank.number, blank.text, blank.hidden) == (2, "", False)
    assert (hidden.text, hidden.hidden) == ("Backup", True)


def test_pptx_limits_and_zip_bombs() -> None:
    data = pptx([("Alpha", "a"), ("Beta", "b")])
    assert inbox("pptx", data, pages=1).reason == (
        "1 slide of 2 extracted (Argus reads at most 1 slide)"
    )
    assert inbox("pptx", data, chars=3).reason == "1 slide of 2 extracted (text limit)"
    for limit in ({"unzipped": 10_000}, {"entries": 3}):
        ex = inbox("pptx", data, **limit)
        assert ex.status is Extracted.NONE
        assert ex.reason.startswith("the file unpacks to more than Argus reads safely")


def test_pptx_with_no_slides() -> None:
    from pptx import Presentation

    buf = io.BytesIO()
    Presentation().save(buf)
    assert inbox("pptx", buf.getvalue()).reason.startswith("the deck has no slides")


# ---------------------------------------------------------------- DOCX (standard library only)

TEXTBOX = (
    "<w:p><w:r><mc:AlternateContent>"
    "<mc:Choice><w:txbxContent>" + para("In a box") + "</w:txbxContent></mc:Choice>"
    "<mc:Fallback><w:txbxContent>" + para("In a box") + "</w:txbxContent></mc:Fallback>"
    "</mc:AlternateContent></w:r><w:r><w:t>Anchor</w:t></w:r></w:p>"
)


def test_docx_paragraphs_in_order() -> None:
    body = (
        para("Problem set ", "4")
        + "<w:p><w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r></w:p>"
        + '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'
        + "<w:r><w:t>first item</w:t></w:r></w:p>"
        + "<w:p><w:pPr><w:numPr/></w:pPr></w:p>"
        + "<w:p><w:r><w:delText>deleted</w:delText><w:instrText>PAGE</w:instrText></w:r></w:p>"
        + "<w:tbl><w:tr><w:tc>"
        + para("cell 1")
        + "</w:tc><w:tc>"
        + para("cell 2")
        + "</w:tc></w:tr></w:tbl>"
        + TEXTBOX
        + "<w:sectPr/>"
    )
    ex = inbox("docx", docx(docx_xml(body), pages=3))
    assert (ex.unit, ex.status, ex.reason, ex.count) == (
        Unit.DOCUMENT,
        Extracted.FULL,
        "all text read",
        3,
    )
    [section] = ex.sections
    assert section.number is None
    assert section.text.splitlines() == [
        "Problem set 4",
        "a b",
        "c",
        "- first item",
        "cell 1",
        "cell 2",
        "In a box",
        "Anchor",
    ]


@pytest.mark.parametrize(
    ("app", "pages"),
    [
        (None, None),
        ("<Properties><Pages>0</Pages></Properties>", None),
        ("<Properties/>", None),
        ("<Properties><Pages>7</Pages>" + " " * 70_000 + "</Properties>", None),
        ("<ep:Properties><ep:Pages> 12 </ep:Pages></ep:Properties>", 12),
    ],
)
def test_docx_page_count(app: str | None, pages: int | None) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", docx_xml(para("x")))
        if app is not None:
            z.writestr("docProps/app.xml", app)
    assert inbox("docx", buf.getvalue()).count == pages


@pytest.mark.parametrize(
    ("data", "limits", "reason"),
    [
        (docx(None), {}, "not a Word document"),
        (docx(docx_xml("<w:p/>" + para(" "))), {}, "the document has no text"),
        (
            docx(docx_xml(para("x")), extra=200_000),
            {"unzipped": 100_000},
            "the file unpacks to more than Argus reads safely",
        ),
        (
            docx(docx_xml(para("&lol;"), prolog='<!DOCTYPE w:document [<!ENTITY lol "lol">]>')),
            {},
            "the document declares a DTD",
        ),
        (docx(b"<w:document><unclosed>"), {}, "Argus couldn't open this DOCX"),
    ],
)
def test_docx_refusals(data: bytes, limits: dict[str, Any], reason: str) -> None:
    ex = inbox("docx", data, **limits)
    assert ex.status is Extracted.NONE
    assert ex.reason.startswith(reason)
    assert ex.reason.endswith("read the file itself")


def test_docx_limits() -> None:
    long = docx(docx_xml(para("abcdefghij") * 5))
    ex = inbox("docx", long, chars=25)
    assert (ex.status, ex.reason) == (
        Extracted.PARTIAL,
        "the document's text was cut short (text limit)",
    )
    assert ex.sections[0].text.replace("\n", "") == "abcdefghij" * 2 + "abcde"

    many = docx(docx_xml(para("line") * 450))
    slow = inbox("docx", many, seconds=5, timer=Ticks(10))
    assert slow.reason == "the document's text was cut short (time limit)"
    assert len(slow.sections[0].text.splitlines()) == 200
    assert len(inbox("docx", many).sections[0].text.splitlines()) == 450
