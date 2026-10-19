"""Collector contract: every source module implements `Collector` and returns a `CollectResult`.

A collector does no database I/O. It reads previous source state from `ctx.state`, fetches,
parses, and returns typed records plus state updates. The runner persists everything and
records health. Raising is allowed (the runner converts it); returning is preferred.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from argus.clock import Clock
from argus.collectors.http import Fetcher
from argus.config import AppConfig, Secrets
from argus.domain.models import CourseInfo, MeetingRecord, PostRecord, Source, SourceItem
from argus.store.repo import SourceState


class RunStatus(StrEnum):
    OK = "ok"
    UNCHANGED = "unchanged"
    ERROR = "error"
    AUTH_ERROR = "auth_error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CollectContext:
    config: AppConfig
    secrets: Secrets
    clock: Clock
    fetcher: Fetcher
    states: Mapping[tuple[str, str], SourceState] = field(default_factory=dict)

    def state(self, source: Source, key: str) -> SourceState:
        return self.states.get((source.value, key), SourceState())


@dataclass(frozen=True)
class CollectResult:
    source: Source
    status: RunStatus
    items: tuple[SourceItem, ...] = ()
    posts: tuple[PostRecord, ...] = ()
    courses: tuple[CourseInfo, ...] = ()
    # Complete meeting lists per course code (replaces what is stored for this source).
    meetings: Mapping[str, tuple[MeetingRecord, ...]] = field(default_factory=dict)
    # Course codes whose item listing is complete. Records missing from it are marked absent,
    # unless their due date is before the window start (the feed's lookback limit).
    complete_scopes: Mapping[str, datetime | None] = field(default_factory=dict)
    state_updates: Mapping[str, SourceState] = field(default_factory=dict)
    error: str | None = None

    @property
    def record_count(self) -> int:
        return len(self.items) + len(self.posts) + sum(len(m) for m in self.meetings.values())


class Collector(Protocol):
    source: Source

    def enabled(self, ctx: CollectContext) -> bool:
        """False when credentials or configuration for this source are missing."""
        ...

    async def collect(self, ctx: CollectContext) -> CollectResult: ...
