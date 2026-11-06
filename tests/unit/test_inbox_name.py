"""File names for the morning planner's Drive inbox (D074): the planner parses them, so the
format is exact. `<COURSE> · <what> · <type> · by <YYYY-MM-DD HH:MM> · <depth>.<ext>`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.clock import local_datetime
from argus.domain.inbox_name import (
    MAX_WHAT,
    SEP,
    Depth,
    FileType,
    build_name,
    build_stem,
    clean_what,
    content_matches,
    extension_of,
    mime_type,
)

# Monday 2026-11-09 09:00 EST (after the 2026-11-01 fall-back) is 14:00 UTC.
MON_9AM = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)


def name(**over: object) -> str:
    args: dict[str, object] = {
        "course": "STAT400",
        "what": "Ch5 lecture slides",
        "file_type": FileType.SLIDES,
        "by": MON_9AM,
        "depth": Depth.DEEP,
        "ext": "pdf",
    }
    args.update(over)
    return build_name(**args)  # type: ignore[arg-type]


# ---------------------------------------------------------------- the format


def test_the_documented_example() -> None:
    assert name() == "STAT400 · Ch5 lecture slides · slides · by 2026-11-09 09:00 · deep.pdf"


def test_separator_is_space_middle_dot_space() -> None:
    assert SEP == " \u00b7 "
    stem = name().removesuffix(".pdf")
    assert stem.split(SEP) == [
        "STAT400",
        "Ch5 lecture slides",
        "slides",
        "by 2026-11-09 09:00",
        "deep",
    ]


@pytest.mark.parametrize(
    ("file_type", "text"),
    [
        (FileType.SLIDES, "slides"),
        (FileType.NOTES, "notes"),
        (FileType.ASSIGNMENT, "assignment"),
        (FileType.EXAM_PREP, "exam-prep"),
        (FileType.READING, "reading"),
        (FileType.OTHER, "other"),
    ],
)
def test_every_type(file_type: FileType, text: str) -> None:
    assert f"{SEP}{text}{SEP}" in name(file_type=file_type)


@pytest.mark.parametrize("depth", ["deep", "normal", "skim"])
def test_every_depth_and_strings_are_accepted(depth: str) -> None:
    assert name(depth=depth, file_type="exam-prep").endswith(f"{SEP}{depth}.pdf")


def test_unknown_type_or_depth_is_rejected() -> None:
    with pytest.raises(ValueError, match="slide"):
        name(file_type="slide")
    with pytest.raises(ValueError, match="medium"):
        name(depth="medium")


def test_course_is_uppercased_without_spaces() -> None:
    assert name(course="stat 400").startswith("STAT400 · ")


@pytest.mark.parametrize("course", ["", "STAT", "STAT4000", "ST·AT400", "STAT400 · x"])
def test_bad_course_is_rejected(course: str) -> None:
    with pytest.raises(ValueError, match="course"):
        name(course=course)


def test_extension_is_kept_lowercase() -> None:
    assert name(ext="PPTX").endswith(" · deep.pptx")
    assert name(ext="jpeg").endswith(".jpeg")


def test_unsupported_extension_is_rejected() -> None:
    with pytest.raises(ValueError, match="PDF"):
        name(ext="exe")


def test_stem_has_no_extension() -> None:
    stem = build_stem(
        course="STAT400",
        what="Ch5",
        file_type=FileType.NOTES,
        by=MON_9AM,
        depth=Depth.NORMAL,
    )
    assert stem == "STAT400 · Ch5 · notes · by 2026-11-09 09:00 · normal"


# ---------------------------------------------------------------- cleaning "what"


@pytest.mark.parametrize(
    ("typed", "clean"),
    [
        ("Ch5 lecture slides", "Ch5 lecture slides"),
        ("  padded   out  ", "padded out"),
        ("HW 3 · part 2", "HW 3 part 2"),
        ("HW·3", "HW 3"),
        ("notes/week 4", "notes week 4"),
        ("a / b // c", "a b c"),
        ("line one\nline two", "line one line two"),
        ("crlf\r\nand\ttab", "crlf and tab"),
        ("u2028\u2028and\u2029u2029", "u2028 and u2029"),
        ("greek\u0387dot", "greek dot"),  # U+0387 normalizes to the middle dot
        ("bullet \u2022 dot \u2219 op \u22c5 kata \u30fb half \uff65", "bullet dot op kata half"),
        ("frac\u2044slash div\u2215slash wide\uff0fslash", "frac slash div slash wide slash"),
        ("zero\u200bwidth", "zerowidth"),
        ("nbsp\u00a0space", "nbsp space"),
        ("Café ü", "Café ü"),
        ("Midterm 2 (ch. 1-4) & HW: 7?", "Midterm 2 (ch. 1-4) & HW: 7?"),
    ],
)
def test_clean_what(typed: str, clean: str) -> None:
    assert clean_what(typed) == clean


def test_clean_what_composes_accents() -> None:
    assert clean_what("Cafe\u0301") == "Caf\u00e9"


def test_clean_what_caps_the_length_without_a_trailing_space() -> None:
    long = "word " * 40
    out = clean_what(long)
    assert len(out) <= MAX_WHAT
    assert out == out.strip()


@pytest.mark.parametrize("typed", ["", "   ", "·", " / \n ", "\u200b"])
def test_empty_what_is_rejected(typed: str) -> None:
    with pytest.raises(ValueError, match="what"):
        name(what=typed)


@given(st.text())
def test_cleaned_what_never_breaks_the_format(typed: str) -> None:
    out = clean_what(typed)
    assert "\u00b7" not in out
    assert "/" not in out
    assert "\n" not in out and "\r" not in out
    assert out == out.strip()
    assert "  " not in out
    assert len(out) <= MAX_WHAT
    if out:
        stem = name(what=typed).removesuffix(".pdf")
        assert len(stem.split(SEP)) == 5


# ---------------------------------------------------------------- "by" is local time


def test_by_is_new_york_time_in_daylight_time() -> None:
    # 2026-10-20 03:59 UTC is 23:59 EDT on the 19th.
    by = datetime(2026, 10, 20, 3, 59, tzinfo=UTC)
    assert "by 2026-10-19 23:59" in name(by=by)


def test_by_is_new_york_time_in_standard_time() -> None:
    # 2026-12-01 04:59 UTC is 23:59 EST on November 30.
    by = datetime(2026, 12, 1, 4, 59, tzinfo=UTC)
    assert "by 2026-11-30 23:59" in name(by=by)


def test_by_accepts_any_aware_timezone() -> None:
    pacific = timezone(timedelta(hours=-8))
    by = datetime(2026, 11, 9, 6, 0, tzinfo=pacific)  # 14:00 UTC = 09:00 EST
    assert "by 2026-11-09 09:00" in name(by=by)


def test_naive_by_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        name(by=datetime(2026, 11, 9, 9, 0))  # noqa: DTZ001


def test_seconds_are_dropped_not_rounded() -> None:
    by = datetime(2026, 11, 9, 14, 0, 59, tzinfo=UTC)
    assert "by 2026-11-09 09:00" in name(by=by)


def test_dst_fall_back_ambiguous_hour_is_the_first_one() -> None:
    # 2026-11-01: 01:30 happens twice. The form's local time resolves to the first (EDT).
    by = local_datetime(datetime(2026, 11, 1).date(), 1, 30)  # noqa: DTZ001
    assert by == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert "by 2026-11-01 01:30" in name(by=by)
    # The second 01:30 (EST) is 06:30 UTC and still reads 01:30 on the wall.
    assert "by 2026-11-01 01:30" in name(by=datetime(2026, 11, 1, 6, 30, tzinfo=UTC))


def test_dst_spring_forward_gap_moves_forward() -> None:
    # 2027-03-14: 02:30 doesn't exist; it is read as 03:30 EDT.
    by = local_datetime(datetime(2027, 3, 14).date(), 2, 30)  # noqa: DTZ001
    assert "by 2027-03-14 03:30" in name(by=by)


def test_midnight_is_00_00_on_the_new_day() -> None:
    by = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)  # 00:00 EST on the 10th
    assert "by 2026-11-10 00:00" in name(by=by)


# ---------------------------------------------------------------- file types


@pytest.mark.parametrize(
    ("filename", "ext"),
    [
        ("slides.pdf", "pdf"),
        ("Deck.PPTX", "pptx"),
        ("essay.final.docx", "docx"),
        ("IMG_0001.JPG", "jpg"),
        ("photo.jpeg", "jpeg"),
        ("scan.png", "png"),
        ("C:\\Users\\me\\notes.pdf", "pdf"),
    ],
)
def test_extension_of(filename: str, ext: str) -> None:
    assert extension_of(filename) == ext


@pytest.mark.parametrize(
    "filename", ["", "noext", "movie.mov", "a.heic", "archive.pdf.zip", ".pdf"]
)
def test_extension_of_rejects_others(filename: str) -> None:
    with pytest.raises(ValueError, match="PDF, PPTX, DOCX, PNG, or JPG"):
        extension_of(filename)


def test_mime_types_keep_the_original_format() -> None:
    assert mime_type("pdf") == "application/pdf"
    assert mime_type("pptx") == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert mime_type("docx") == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert mime_type("png") == "image/png"
    assert mime_type("jpg") == mime_type("jpeg") == "image/jpeg"
    assert not any("google-apps" in mime_type(e) for e in ("pdf", "pptx", "docx", "png", "jpg"))


@pytest.mark.parametrize(
    ("ext", "head", "ok"),
    [
        ("pdf", b"%PDF-1.7\n", True),
        ("pdf", b"\xef\xbb\xbf\n%PDF-1.4", True),  # junk before the header is allowed
        ("pdf", b"PK\x03\x04", False),
        ("pptx", b"PK\x03\x04rest", True),
        ("docx", b"PK\x03\x04rest", True),
        ("docx", b"%PDF-1.7", False),
        ("png", b"\x89PNG\r\n\x1a\nrest", True),
        ("png", b"\xff\xd8\xff\xe0", False),
        ("jpg", b"\xff\xd8\xff\xe0rest", True),
        ("jpeg", b"\xff\xd8\xff\xe1rest", True),
        ("jpg", b"", False),
    ],
)
def test_content_matches(ext: str, head: bytes, ok: bool) -> None:
    assert content_matches(ext, head) is ok
