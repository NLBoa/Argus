import pytest

from argus.domain.findings import matches_keywords


@pytest.mark.parametrize(
    "text",
    [
        "Quiz 3 moved",
        "the EXAM covers chapters 1-4",
        "Class cancelled Friday",
        "HW4 due date extended",
        "You may bring a cheat  sheet",
        "Midterms are graded",
        "final review",
    ],
)
def test_matches(text: str) -> None:
    assert matches_keywords(text)


@pytest.mark.parametrize(
    "text", ["Study group tonight?", "Office hours in IRB", "residue", "undue"]
)
def test_no_match(text: str) -> None:
    assert not matches_keywords(text)
