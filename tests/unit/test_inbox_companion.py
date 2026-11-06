"""The text companion to a planner inbox file (D077): frontmatter a scheduled task parses
without guessing, and a body it can split on `## Page n` / `## Slide n` headings."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from argus.domain.inbox_companion import (
    COMPANION_MIME,
    SCHEMA_VERSION,
    CompanionMeta,
    Extracted,
    Extraction,
    Section,
    Unit,
    clean_text,
    companion_name,
    frontmatter,
    one_line,
    render_companion,
    utc_iso,
)

NAME = "CMSC351 · Week 9 slides · slides · by 2026-11-09 09:00 · deep.pptx"
# Monday 2026-11-09 09:00 EST is 14:00 UTC.
META = CompanionMeta(
    file_name=NAME,
    course="CMSC351",
    title="Week 9 slides",
    file_type="slides",
    depth="deep",
    done_by=datetime(2026, 11, 9, 14, 0, tzinfo=UTC),
    original_name="lecture9.pptx",
    mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    size_bytes=48213,
    uploaded_at=datetime(2026, 11, 6, 15, 4, 5, 999, tzinfo=UTC),
    drive_file_id="file-1",
    drive_link="https://drive.google.com/file/d/file-1/view",
)
SLIDES = Extraction(
    Unit.SLIDE,
    Extracted.FULL,
    "all 3 slides read",
    3,
    (
        Section(1, "Heaps\nA heap is a tree", notes="Say it twice"),
        Section(2, ""),
        Section(3, "Build\nLinear time", hidden=True),
    ),
)


def split(text: str) -> tuple[dict[str, Any], str]:
    """What a reader does: the YAML between the first two `---` lines, then the body."""
    assert text.startswith("---\n")
    head, body = text[4:].split("\n---\n", 1)
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data, body


def headings(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("#")]


def test_frontmatter_has_every_field_typed() -> None:
    data, _ = split(render_companion(META, SLIDES))
    assert data == {
        "schema_version": SCHEMA_VERSION,
        "format": "argus-planner-inbox-companion",
        "file": NAME,
        "course": "CMSC351",
        "title": "Week 9 slides",
        "type": "slides",
        "depth": "deep",
        "done_by_local": "2026-11-09 09:00",
        "done_by_utc": "2026-11-09T14:00:00Z",
        "timezone": "America/New_York",
        "original_name": "lecture9.pptx",
        "mime_type": META.mime_type,
        "size_bytes": 48213,
        "drive_file_id": "file-1",
        "drive_link": "https://drive.google.com/file/d/file-1/view",
        "unit": "slide",
        "count": 3,
        "sections": 3,
        "extracted": "full",
        "extracted_reason": "all 3 slides read",
        "uploaded_at": "2026-11-06T15:04:05Z",
    }
    assert list(data) == list(frontmatter(META, SLIDES))  # written in a fixed order


def test_every_string_is_quoted_so_nothing_reads_as_a_date() -> None:
    text = render_companion(META, SLIDES)
    assert 'done_by_utc: "2026-11-09T14:00:00Z"\n' in text
    assert "schema_version: 1\n" in text
    assert "count: 3\n" in text


def test_slides_body() -> None:
    _, body = split(render_companion(META, SLIDES))
    assert headings(body) == [
        "# Week 9 slides (CMSC351)",
        "## Slide 1",
        "### Speaker notes",
        "## Slide 2",
        "## Slide 3 (hidden)",
    ]
    assert "Speaker notes follow their slide." in body
    assert "## Slide 1\n\nHeaps\nA heap is a tree\n\n### Speaker notes\n\nSay it twice\n" in body
    assert "## Slide 2\n\n_(No text on this slide.)_\n" in body
    assert body.endswith("## Slide 3 (hidden)\n\nBuild\nLinear time\n")


def test_pages_partial_and_failed() -> None:
    ex = Extraction(
        Unit.PAGE,
        Extracted.PARTIAL,
        "1 page of 2 couldn't be read",
        2,
        (Section(1, "The master theorem"), Section(2, failed=True)),
    )
    _, body = split(render_companion(META, ex))
    assert headings(body)[1:] == ["## Page 1", "## Page 2"]
    assert "Not everything was extracted: 1 page of 2 couldn't be read." in body
    assert "Speaker notes" not in body
    assert "## Page 2\n\n_(Argus couldn't read this page.)_\n" in body


def test_a_document_is_one_section() -> None:
    ex = Extraction(Unit.DOCUMENT, Extracted.FULL, "all text read", None, (Section(None, "Hi"),))
    data, body = split(render_companion(META, ex))
    assert data["count"] is None
    assert data["sections"] == 1
    assert body.endswith("## Document\n\nHi\n")


def test_nothing_extracted() -> None:
    ex = Extraction(Unit.IMAGE, Extracted.NONE, "image — read the file itself")
    meta = replace(META, drive_file_id="", drive_link=None, original_name="", course="")
    data, body = split(render_companion(meta, ex))
    assert data["extracted"] == "none"
    assert data["sections"] == 0
    assert data["count"] is None
    assert data["drive_file_id"] is None
    assert data["drive_link"] is None
    assert data["original_name"] is None
    assert headings(body) == ["# Week 9 slides"]
    assert f"No text was extracted from {NAME}: image — read the file itself." in body


def test_companion_name_sorts_right_after_its_file() -> None:
    assert companion_name(NAME) == NAME + ".md"
    others = [NAME.replace("Week 9", w) for w in ("Week 10", "Week 9b", "Week 90")]
    names = sorted([NAME + ".md", *others, NAME, NAME.replace(".pptx", ".pdf")])
    assert names[names.index(NAME) + 1] == NAME + ".md"
    assert COMPANION_MIME == "text/markdown"


def test_utc_iso() -> None:
    assert utc_iso(datetime(2026, 3, 8, 7, 0, 0, 5, tzinfo=UTC)) == "2026-03-08T07:00:00Z"
    with pytest.raises(ValueError, match="naive"):
        utc_iso(datetime(2026, 3, 8, 7, 0))  # noqa: DTZ001 - the point of the test


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("  a  b \r\n\r\n\r\n c\u2028d\x0be  ", "a b\n\nc\nd\ne"),
        ("# Heading\n## Page 9\n   ### x", "\\# Heading\n\\## Page 9\n\\### x"),
        ("Title\n---\n===\n***\n___\n- item", "Title\n\\---\n\\===\n\\***\n\\___\n- item"),
        ("tab\there\x00zero\u200bwidth\ufeff", "tab here zerowidth"),
        ("\n\n\n", ""),
        ("a\n \n", "a"),
        ("e\u0301", "\u00e9"),
    ],
)
def test_clean_text(raw: str, clean: str) -> None:
    assert clean_text(raw) == clean


def test_one_line() -> None:
    assert one_line("a\nb\u2029c\x00d\ufffee", 300) == "a b c de"
    assert one_line("x" * 400) == "x" * 300
    assert one_line("  ") == ""


_TEXT = st.text(st.characters(codec="utf-8"), max_size=60)


@given(title=_TEXT, original=_TEXT, reason=_TEXT, name=_TEXT)
def test_frontmatter_round_trips_whatever_the_text(
    title: str, original: str, reason: str, name: str
) -> None:
    meta = replace(META, title=title, original_name=original, file_name=name)
    ex = replace(SLIDES, reason=reason)
    data, _ = split(render_companion(meta, ex))
    assert data["title"] == one_line(title)
    assert data["original_name"] == (one_line(original) or None)
    assert data["extracted_reason"] == one_line(reason)
    assert data["file"] == one_line(name, 500)
    assert data["done_by_utc"] == "2026-11-09T14:00:00Z"


@given(pages=st.lists(st.tuples(_TEXT, _TEXT), min_size=1, max_size=6))
def test_only_argus_writes_headings(pages: list[tuple[str, str]]) -> None:
    ex = Extraction(
        Unit.SLIDE,
        Extracted.FULL,
        "ok",
        len(pages),
        tuple(Section(n, text, notes) for n, (text, notes) in enumerate(pages, start=1)),
    )
    _, body = split(render_companion(META, ex))
    slides = [h for h in headings(body) if h.startswith("## ")]
    assert slides == [f"## Slide {n}" for n in range(1, len(pages) + 1)]
    assert all(re.fullmatch(r"# .*|## Slide \d+|### Speaker notes", h) for h in headings(body)), (
        headings(body)
    )
