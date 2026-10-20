"""Capabilities: which features exist for the configured sources (D054)."""

from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest
from pydantic import SecretStr

from argus.capabilities import AI, FEATURES, Capabilities, Feature, derive
from argus.config import AppConfig, CourseConfig, Secrets

COURSES = [
    CourseConfig(
        code="CMSC351",
        section="0101",
        canvas_id=1,
        gradescope_id="7",
        piazza_network_id="abc",
    ),
    CourseConfig(code="MATH240", section="0201", canvas_id=2),
    CourseConfig(code="ENGL393", section="0301"),
]
CONFIG = AppConfig(courses=COURSES)


def secrets(
    *,
    ical: bool = False,
    token: bool = False,
    gradescope: bool = False,
    piazza: bool = False,
    gmail: Path | None = None,
    ai: bool = False,
) -> Secrets:
    return Secrets(
        canvas_ical_url=SecretStr("https://example.test/feed.ics") if ical else None,
        canvas_token=SecretStr("tok") if token else None,
        gradescope_email="s@example.test" if gradescope else None,
        gradescope_password=SecretStr("pw") if gradescope else None,
        piazza_email="s@example.test" if piazza else None,
        piazza_password=SecretStr("pw") if piazza else None,
        gmail_token_path=gmail,
        anthropic_api_key=SecretStr("key") if ai else None,
    )


# The documented matrix (docs/DECISIONS.md D054). Each feature needs any one of these sets.
EXPECTED = {
    Feature.CALENDAR: (frozenset({"canvas_ical"}),),
    Feature.MODULES: (frozenset({"canvas_modules"}),),
    Feature.GRADE: (frozenset({"canvas_modules"}),),
    Feature.SYLLABUS: (frozenset({"canvas_modules"}),),
    Feature.CANVAS_ANNOUNCEMENTS: (frozenset({"canvas_api"}),),
    Feature.GRADESCOPE: (frozenset({"gradescope"}),),
    Feature.PIAZZA: (frozenset({"piazza"}), frozenset({"piazza_email"})),
    Feature.STAFF_POSTS: (
        frozenset({"canvas_api"}),
        frozenset({"piazza"}),
        frozenset({"piazza_email"}),
    ),
    Feature.SUBMISSION_TRACKING: (frozenset({"canvas_api"}), frozenset({"gradescope"})),
    Feature.AI: (frozenset({AI}),),
    Feature.STUDY_GENERATION: (frozenset({"canvas_modules", AI}),),
}


def test_matrix_is_the_documented_one() -> None:
    assert FEATURES == EXPECTED
    assert set(FEATURES) == set(Feature)


def test_nothing_configured() -> None:
    caps = derive(AppConfig(), Secrets())
    assert caps.features == frozenset()
    assert not caps.has("modules")
    assert caps.tracking_sources == frozenset()
    assert not caps.course("CMSC351").modules


def test_token_enables_modules_only_for_courses_with_a_canvas_id() -> None:
    caps = derive(CONFIG, secrets(ical=True, token=True))
    assert caps.has(Feature.MODULES) and caps.has("grade")
    assert caps.course("CMSC351").modules and caps.course("MATH240").modules
    assert not caps.course("ENGL393").modules  # no canvas_id
    assert not caps.course("ENGL393").canvas_api
    assert caps.tracking_sources == frozenset({"canvas_api"})


def test_without_token_no_canvas_api_features() -> None:
    caps = derive(CONFIG, secrets(ical=True, gradescope=True))
    for f in (Feature.MODULES, Feature.GRADE, Feature.SYLLABUS, Feature.CANVAS_ANNOUNCEMENTS):
        assert not caps.has(f)
    assert caps.has(Feature.SUBMISSION_TRACKING)  # Gradescope still reports submissions
    assert caps.tracking_sources == frozenset({"gradescope"})


def test_staff_posts_from_any_post_source(tmp_path: Path) -> None:
    assert not derive(CONFIG, secrets(ical=True)).has(Feature.STAFF_POSTS)
    assert derive(CONFIG, secrets(token=True)).has(Feature.STAFF_POSTS)
    assert derive(CONFIG, secrets(piazza=True)).has(Feature.STAFF_POSTS)
    token = tmp_path / "gmail.json"
    assert not derive(CONFIG, secrets(gmail=token)).has(Feature.STAFF_POSTS)  # file missing
    token.write_text("{}")
    caps = derive(CONFIG, secrets(gmail=token))
    assert caps.has(Feature.STAFF_POSTS) and caps.has(Feature.PIAZZA)
    assert caps.course("CMSC351").piazza and not caps.course("MATH240").piazza


def test_study_generation_needs_modules_and_ai() -> None:
    assert not derive(CONFIG, secrets(token=True)).has(Feature.STUDY_GENERATION)
    assert not derive(CONFIG, secrets(ai=True)).has(Feature.STUDY_GENERATION)
    assert derive(CONFIG, secrets(token=True, ai=True)).has(Feature.STUDY_GENERATION)
    # A test or demo transport counts as AI even without a key.
    assert derive(CONFIG, secrets(token=True), ai=True).has(Feature.STUDY_GENERATION)


@pytest.mark.parametrize(
    "flags", list(product([False, True], repeat=5)), ids=lambda f: "".join("01"[x] for x in f)
)
def test_every_combination_matches_the_matrix(flags: tuple[bool, ...]) -> None:
    ical, token, gradescope, piazza, ai = flags
    caps = derive(
        CONFIG,
        secrets(ical=ical, token=token, gradescope=gradescope, piazza=piazza, ai=ai),
    )
    on = {
        "canvas_ical": ical,
        "canvas_api": token,
        "canvas_modules": token,
        "gradescope": gradescope,
        "piazza": piazza,
        "piazza_email": False,
        AI: ai,
    }
    for feature, options in EXPECTED.items():
        expected = any(all(on[s] for s in option) for option in options)
        assert caps.has(feature) is expected, feature


def test_budget_hides_only_new_calls() -> None:
    caps = derive(CONFIG, secrets(token=True, ai=True))
    assert caps.ai_calls_left
    spent = caps.with_budget(spent=10.0, cap=10.0)
    assert not spent.ai_calls_left
    assert spent.has(Feature.AI)  # cached content stays visible
    assert spent.has(Feature.STUDY_GENERATION)
    assert caps.with_budget(spent=2.0, cap=10.0).ai_calls_left
    assert not derive(CONFIG, Secrets()).with_budget(spent=0.0, cap=10.0).ai_calls_left


def test_unknown_course_has_nothing() -> None:
    caps = derive(CONFIG, secrets(token=True, gradescope=True, piazza=True))
    assert caps.course("HIST200") == caps.course("NOPE")
    assert not caps.course("HIST200").modules


def test_capabilities_are_frozen() -> None:
    caps = derive(CONFIG, secrets(token=True))
    assert isinstance(caps, Capabilities)
    with pytest.raises(AttributeError):
        caps.features = frozenset()  # type: ignore[misc]


def test_shows_post_follows_the_source() -> None:
    full = derive(CONFIG, secrets(token=True, piazza=True))
    none = derive(CONFIG, secrets(ical=True))
    assert full.shows_post(platform="canvas", kind="announcement", course="CMSC351")
    assert full.shows_post(platform="canvas", kind="syllabus", course="MATH240")
    assert full.shows_post(platform="piazza", kind="note", course="CMSC351")
    assert not full.shows_post(platform="piazza", kind="note", course="MATH240")  # no network id
    assert not full.shows_post(platform="canvas", kind="announcement", course="ENGL393")
    assert not full.shows_post(platform="email", kind="note", course="CMSC351")
    for kind in ("announcement", "syllabus"):
        assert not none.shows_post(platform="canvas", kind=kind, course="CMSC351")
    assert not none.shows_post(platform="piazza", kind="note", course="CMSC351")


def test_source_names_for_copy() -> None:
    assert derive(CONFIG, secrets(ical=True, gradescope=True)).deadline_sources() == (
        "ELMS or Gradescope"
    )
    assert derive(CONFIG, secrets(gradescope=True)).deadline_sources() == "Gradescope"
    assert derive(CONFIG, Secrets()).deadline_sources() == "your course sites"
    assert derive(CONFIG, secrets(token=True, piazza=True)).post_sources() == "ELMS and Piazza"
    assert derive(CONFIG, secrets(piazza=True)).post_sources() == "Piazza"
    assert derive(CONFIG, secrets(ical=True)).post_sources() == "your course sites"


def test_sms_help_lists_ok_no_only_with_staff_posts() -> None:
    from argus.reminders.text import SEGMENT, gsm_length
    from argus.sms import help_text

    assert "OK 1, NO 1" in help_text(derive(CONFIG, secrets(piazza=True)))
    assert "OK 1" not in help_text(derive(CONFIG, secrets(ical=True)))
    for caps in (derive(CONFIG, secrets(piazza=True)), derive(CONFIG, secrets(ical=True))):
        assert gsm_length(help_text(caps)) <= SEGMENT  # one segment: [ and ] count twice
