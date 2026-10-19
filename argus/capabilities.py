"""Capabilities: which features exist, given the configured sources (D054).

One object, derived once from `config.yaml` and the secrets, answers "can Argus show this?"
everywhere: web views and templates, the vault, reminders, and SMS. Features are named by
what the student sees (modules, grade, staff posts), not by collector. A feature that needs a
missing source is hidden, not shown empty (D025, D055).

Each collector's own `enabled()` decides whether its source is configured, so this module
cannot drift from what the sync runner actually does. Per-course flags add the course's own
id (a token does nothing for a course without a `canvas_id`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import cast

from argus.clock import Clock
from argus.collectors.base import CollectContext
from argus.collectors.http import Fetcher
from argus.collectors.runner import default_collectors
from argus.config import AppConfig, Secrets

AI = "ai"  # pseudo-source: an Anthropic key (or an injected transport in tests and the demo)
TRACKING = frozenset({"canvas_api", "gradescope"})  # sources that report submission state


class Feature(StrEnum):
    CALENDAR = "calendar"  # deadlines from the ELMS calendar feed
    SCHEDULE = "schedule"  # class meeting times (umd.io)
    MODULES = "modules"
    GRADE = "grade"
    SYLLABUS = "syllabus"
    CANVAS_ANNOUNCEMENTS = "canvas_announcements"
    GRADESCOPE = "gradescope"
    PIAZZA = "piazza"
    STAFF_POSTS = "staff_posts"  # the Announcements page: any source of staff posts
    SUBMISSION_TRACKING = "submission_tracking"  # Argus can see that work was submitted
    AI = "ai"
    STUDY_GENERATION = "study_generation"  # concept notes and practice from course files


def _any(*options: set[str]) -> tuple[frozenset[str], ...]:
    return tuple(frozenset(o) for o in options)


# Feature -> the source sets that enable it (any one set suffices). Pinned by
# tests/unit/test_capabilities.py and documented in docs/DECISIONS.md (D054).
FEATURES: Mapping[Feature, tuple[frozenset[str], ...]] = {
    Feature.CALENDAR: _any({"canvas_ical"}),
    Feature.SCHEDULE: _any({"umdio"}),
    Feature.MODULES: _any({"canvas_modules"}),
    Feature.GRADE: _any({"canvas_modules"}),
    Feature.SYLLABUS: _any({"canvas_modules"}),
    Feature.CANVAS_ANNOUNCEMENTS: _any({"canvas_api"}),
    Feature.GRADESCOPE: _any({"gradescope"}),
    Feature.PIAZZA: _any({"piazza"}, {"piazza_email"}),
    Feature.STAFF_POSTS: _any({"canvas_api"}, {"piazza"}, {"piazza_email"}),
    Feature.SUBMISSION_TRACKING: _any({"canvas_api"}, {"gradescope"}),
    Feature.AI: _any({AI}),
    Feature.STUDY_GENERATION: _any({"canvas_modules", AI}),
}


@dataclass(frozen=True, slots=True)
class CourseCaps:
    canvas_api: bool = False
    modules: bool = False  # modules, grade, syllabus, and course files
    gradescope: bool = False
    piazza: bool = False


_NO_COURSE = CourseCaps()


@dataclass(frozen=True, slots=True)
class Capabilities:
    sources: Mapping[str, bool]
    courses: Mapping[str, CourseCaps]
    features: frozenset[Feature]
    ai_calls_left: bool = False  # AI is set up and this month's budget is not spent

    def has(self, feature: Feature | str) -> bool:
        """For code and templates: `caps.has('modules')`."""
        return Feature(feature) in self.features

    def course(self, code: str) -> CourseCaps:
        return self.courses.get(code, _NO_COURSE)

    @property
    def tracking_sources(self) -> frozenset[str]:
        """The enabled sources that report submissions (D056)."""
        return frozenset(s for s in TRACKING if self.sources.get(s))

    def with_budget(self, *, spent: float, cap: float) -> Capabilities:
        """The budget hides only affordances that make new AI calls (Re-run AI estimate).
        Content the AI already produced stays visible."""
        return replace(self, ai_calls_left=self.has(Feature.AI) and spent < cap)


def derive(config: AppConfig, secrets: Secrets, *, ai: bool = False) -> Capabilities:
    """`ai` is True when an AI transport is injected (tests, the demo) without a key."""
    # enabled() only reads config and secrets; it never touches the clock or the network.
    ctx = CollectContext(
        config=config, secrets=secrets, clock=cast(Clock, None), fetcher=cast(Fetcher, None)
    )
    sources = {c.source.value: c.enabled(ctx) for c in default_collectors()}
    on = {s for s, enabled in sources.items() if enabled}
    if ai or secrets.anthropic_api_key is not None:
        on.add(AI)
    features = frozenset(f for f, options in FEATURES.items() if any(o <= on for o in options))
    piazza = sources.get("piazza", False) or sources.get("piazza_email", False)
    courses = {
        c.code: CourseCaps(
            canvas_api=sources.get("canvas_api", False) and c.canvas_id is not None,
            modules=sources.get("canvas_modules", False) and c.canvas_id is not None,
            gradescope=sources.get("gradescope", False) and c.gradescope_id is not None,
            piazza=piazza and c.piazza_network_id is not None,
        )
        for c in config.courses
    }
    return Capabilities(
        sources=sources,
        courses=courses,
        features=features,
        ai_calls_left=Feature.AI in features,
    )
