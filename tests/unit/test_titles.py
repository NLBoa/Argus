import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.domain.models import ItemKind
from argus.domain.titles import clean_display_title, infer_kind, normalize_title


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Homework 3 [CMSC351-0101-Fall2026]", "HW3"),
        ("HW #03: Graphs", "homework 3 graphs"),
        ("Problem Set 2", "PSet 2"),
        ("Problem Set 2", "PS2"),
        ("CMSC351 Quiz 1", "Quiz 1"),
        ("Homework 3 \u2013 Graphs", "Homework 3 - Graphs"),  # en dash
        ("  Project   1  ", "project 1"),
        ("Lab_04.", "lab 4"),
    ],
)
def test_equivalent_titles_normalize_equal(a: str, b: str) -> None:
    assert normalize_title(a) == normalize_title(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Quiz 3", "Quiz 3 (makeup)"),
        ("HW 1", "HW 10"),
        ("Midterm 1", "Midterm 2"),
        ("Project 1", "Project 1 Proposal"),
    ],
)
def test_distinct_titles_stay_distinct(a: str, b: str) -> None:
    assert normalize_title(a) != normalize_title(b)


def test_normalize_examples() -> None:
    assert normalize_title("HW #03: Graphs") == "hw 3 graphs"
    assert normalize_title("Homework 10 [MATH240-0201]") == "hw 10"
    assert normalize_title("") == ""
    assert normalize_title("100") == "100"
    assert normalize_title("Quiz 0") == "quiz 0"


@given(st.text())
def test_normalize_is_idempotent(s: str) -> None:
    once = normalize_title(s)
    assert normalize_title(once) == once


@given(st.text(alphabet=string.ascii_letters + string.digits + string.punctuation + " "))
def test_normalize_ignores_ascii_case(s: str) -> None:
    assert normalize_title(s.upper()) == normalize_title(s.lower())


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Quiz 3", ItemKind.QUIZ),
        ("Weekly quiz: recursion", ItemKind.QUIZ),
        ("Midterm 1", ItemKind.EXAM),
        ("Final Exam", ItemKind.EXAM),
        ("Exam 2 (in class)", ItemKind.EXAM),
        ("Final", ItemKind.EXAM),
        ("Final Project Report", ItemKind.ASSIGNMENT),
        ("Homework 4", ItemKind.ASSIGNMENT),
        ("Examples worksheet", ItemKind.ASSIGNMENT),
    ],
)
def test_infer_kind(title: str, expected: ItemKind) -> None:
    assert infer_kind(title, ItemKind.ASSIGNMENT) == expected


def test_infer_kind_keeps_default_for_events() -> None:
    assert infer_kind("Office hours", ItemKind.EVENT) == ItemKind.EVENT
    assert infer_kind("Midterm review session", ItemKind.EVENT) == ItemKind.EVENT
    assert infer_kind("Midterm", ItemKind.EVENT) == ItemKind.EXAM


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Homework 3 [CMSC351-0101-Fall2026]", "Homework 3"),
        ("Homework 3", "Homework 3"),
        ("  Quiz [draft] 2 [MATH240] ", "Quiz [draft] 2"),
    ],
)
def test_clean_display_title(raw: str, clean: str) -> None:
    assert clean_display_title(raw) == clean
