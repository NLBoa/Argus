import pytest

from argus.collectors.extract_text import MAX_BYTES, extract, kind_for, page_text
from tests.material_fakes import pdf, pptx


def test_pdf_pages_are_referenced_by_page_number() -> None:
    pages = extract("pdf", pdf(["The master theorem", "Case two applies"]))
    assert [(p.ref, p.text) for p in pages] == [
        ("page 1", "The master theorem"),
        ("page 2", "Case two applies"),
    ]


def test_pptx_slides_include_speaker_notes() -> None:
    slides = extract(
        "pptx",
        pptx([("Heaps", "A heap is a tree"), ("Build", "Linear time")], notes="Say it twice"),
    )
    assert slides[0].ref == "slide 1"
    assert slides[0].text == "Heaps A heap is a tree (speaker notes) Say it twice"
    assert len(slides) == 2


def test_kinds_and_limits() -> None:
    assert kind_for("application/pdf", "x.bin") == "pdf"
    assert kind_for("application/octet-stream", "Lecture.PPTX") == "pptx"
    assert kind_for("image/png", "a.png") is None
    with pytest.raises(ValueError, match="too large"):
        extract("pdf", b"0" * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="cannot extract"):
        extract("png", b"x")
    assert page_text("<p>Hello <b>world</b></p>")[0].text == "Hello world"
    assert page_text("") == ()
