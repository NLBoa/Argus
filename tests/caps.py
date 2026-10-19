"""Capabilities for tests that predate D056 and assume every item reports submissions."""

from __future__ import annotations

from pydantic import SecretStr

from argus.capabilities import Capabilities, derive
from argus.config import AppConfig, Secrets
from argus.domain.models import Source

# refresh_statuses(..., tracking_sources=EVERY_SOURCE): any present source record counts as
# tracking, which is the pre-D056 behavior (unknown submission state = not submitted).
EVERY_SOURCE = frozenset(s.value for s in Source)


def tracked_caps(config: AppConfig) -> Capabilities:
    """Every collector enabled (ELMS API and Gradescope report submissions)."""
    return derive(
        config,
        Secrets(
            canvas_ical_url=SecretStr("https://example.test/feed.ics"),
            canvas_token=SecretStr("tok"),
            gradescope_email="s@example.test",
            gradescope_password=SecretStr("pw"),
            piazza_email="s@example.test",
            piazza_password=SecretStr("pw"),
        ),
    )
