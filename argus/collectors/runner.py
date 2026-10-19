"""The sync cycle: run due collectors in isolation, persist results, merge, derive status.

Isolation: each collector runs under its own timeout and exception boundary, and its
results are persisted in its own transaction, so one failing source never blocks the others.
Errors are redacted before they reach the database or logs.

Piazza fallback: when the Piazza API is not configured, or its login (or fetch) fails, the
Gmail-based email collector runs in the same cycle.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from argus.announcements import attach_official
from argus.clock import Clock
from argus.collectors.base import CollectContext, Collector, CollectResult, RunStatus
from argus.collectors.canvas_api import CanvasApiCollector
from argus.collectors.canvas_ical import CanvasIcalCollector
from argus.collectors.canvas_modules import CanvasModulesCollector
from argus.collectors.gradescope import GradescopeCollector
from argus.collectors.http import AuthError, Fetcher
from argus.collectors.piazza import PiazzaCollector
from argus.collectors.piazza_email import PiazzaEmailCollector
from argus.collectors.schedule import POLICIES, is_due
from argus.collectors.umdio import UmdioCollector
from argus.config import AppConfig, Secrets
from argus.domain.models import Source
from argus.logging import get_logger, redactor
from argus.store import repo
from argus.store.db import iso, parse_iso, transaction

log = get_logger(__name__)
VANISH_TRUST_AFTER = timedelta(hours=24)
COLLECTOR_TIMEOUT_S = 180.0
_FAILED = (RunStatus.ERROR, RunStatus.AUTH_ERROR)


def default_collectors() -> list[Collector]:
    return [
        CanvasIcalCollector(),
        CanvasApiCollector(),
        CanvasModulesCollector(),
        GradescopeCollector(),
        PiazzaCollector(),
        PiazzaEmailCollector(),
        UmdioCollector(),
    ]


async def run_collector(
    collector: Collector, ctx: CollectContext, timeout: float = COLLECTOR_TIMEOUT_S
) -> CollectResult:
    """Run one collector. Never raises: failures become ERROR / AUTH_ERROR results."""
    try:
        return await asyncio.wait_for(collector.collect(ctx), timeout)
    except AuthError as e:
        return CollectResult(collector.source, RunStatus.AUTH_ERROR, error=redactor(str(e)))
    except TimeoutError:
        return CollectResult(
            collector.source, RunStatus.ERROR, error=f"timed out after {timeout:.0f}s"
        )
    except Exception as e:
        log.exception("collector failed", extra={"source": collector.source.value})
        return CollectResult(
            collector.source, RunStatus.ERROR, error=redactor(f"{type(e).__name__}: {e}")[:500]
        )


@dataclass
class CycleReport:
    results: list[CollectResult] = field(default_factory=list)
    new_items: list[int] = field(default_factory=list)
    new_posts: list[int] = field(default_factory=list)
    status_changes: int = 0

    def result(self, source: Source) -> CollectResult | None:
        return next((r for r in self.results if r.source == source), None)


class SyncRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: AppConfig,
        secrets: Secrets,
        clock: Clock,
        fetcher: Fetcher,
        *,
        collectors: Sequence[Collector] | None = None,
        timeout: float = COLLECTOR_TIMEOUT_S,
    ) -> None:
        self.conn = conn
        self.config = config
        self.secrets = secrets
        self.clock = clock
        self.fetcher = fetcher
        self.collectors = list(collectors) if collectors is not None else default_collectors()
        self.timeout = timeout

    def _context(self) -> CollectContext:
        return CollectContext(
            config=self.config,
            secrets=self.secrets,
            clock=self.clock,
            fetcher=self.fetcher,
            states=repo.all_source_state(self.conn),
        )

    def _wanted(
        self, collector: Collector, ctx: CollectContext, only: set[Source] | None, force: bool
    ) -> bool:
        if not collector.enabled(ctx):
            return False
        if only is not None:
            return collector.source in only
        if force:
            return True
        health = repo.source_health(self.conn, [collector.source.value])[collector.source.value]
        if collector.source == Source.UMDIO and self._missing_meetings():
            return True  # once per term: whenever a course has no meetings yet
        policy = POLICIES.get(collector.source)
        return policy is not None and is_due(policy, self.clock.now(), health.last_attempt)

    def _missing_meetings(self) -> bool:
        have = {m.course_code for m in repo.list_meetings(self.conn)}
        return any(c.code not in have for c in self.config.courses)

    def _piazza_fallback_needed(self, ctx: CollectContext, piazza: CollectResult | None) -> bool:
        piazza_collector = next((c for c in self.collectors if c.source == Source.PIAZZA), None)
        if piazza_collector is None or not piazza_collector.enabled(ctx):
            return True
        if piazza is not None:
            return piazza.status in _FAILED
        last = repo.source_health(self.conn, [Source.PIAZZA.value])[Source.PIAZZA.value]
        return last.last_status in ("error", "auth_error")

    async def run_cycle(
        self, *, only: set[Source] | None = None, force: bool = False
    ) -> CycleReport:
        with transaction(self.conn):
            ids = repo.sync_courses(self.conn, self.config, self.clock.now())
        ctx = self._context()
        primary = [
            c
            for c in self.collectors
            if c.source != Source.PIAZZA_EMAIL and self._wanted(c, ctx, only, force)
        ]
        started = self.clock.now()
        results = list(
            await asyncio.gather(*(run_collector(c, ctx, self.timeout) for c in primary))
        )

        email = next((c for c in self.collectors if c.source == Source.PIAZZA_EMAIL), None)
        if email is not None and email.enabled(ctx):
            piazza = next((r for r in results if r.source == Source.PIAZZA), None)
            explicit = only is not None and Source.PIAZZA_EMAIL in only
            if explicit or (
                (only is None or Source.PIAZZA in only)
                and self._piazza_fallback_needed(ctx, piazza)
                and (
                    force
                    or only is not None
                    or (piazza is not None and piazza.status in _FAILED)
                    or self._wanted(email, ctx, None, False)
                )
            ):
                results.append(await run_collector(email, ctx, self.timeout))

        report = CycleReport(results=results)
        finished = self.clock.now()
        for result in results:
            report.new_posts += self._persist(result, ids, started, finished)
        with transaction(self.conn):
            attach_official(self.conn)  # official records take over announced items
            report.new_items = repo.apply_merge(self.conn, ids, self.clock.now())
            report.status_changes = repo.refresh_statuses(self.conn, self.clock.now())
            repo.rebuild_search_index(self.conn)
        for r in results:
            log.info(
                "sync",
                extra={
                    "source": r.source.value,
                    "status": r.status.value,
                    "records": r.record_count,
                    "error": r.error,
                },
            )
        return report

    def _mark_absent(
        self,
        result: CollectResult,
        ids: dict[str, int],
        code: str,
        *,
        present: set[str],
        window_start: datetime | None,
        now: datetime,
    ) -> CollectResult:
        """Review R6: a listing that drops all or most upcoming records at once is kept and
        reported as an error (three in a row alert the student). If it is still the case 24
        hours later, it is believed: the records are marked absent."""
        key = f"vanish:{code}"
        state = repo.all_source_state(self.conn).get((result.source.value, key))
        since = parse_iso(state.data) if state and state.data else None
        trust = since is not None and now - since >= VANISH_TRUST_AFTER
        try:
            repo.mark_absent(
                self.conn,
                result.source,
                course_id=ids[code],
                present_external_ids=present,
                now=now,
                not_before=window_start,
                guard=not trust,
            )
        except repo.SuspiciousListingError as e:
            if since is None:
                repo.set_source_state(
                    self.conn, result.source.value, key, repo.SourceState(data=iso(now)), now
                )
            return replace(
                result,
                status=RunStatus.ERROR,
                error=f"{code}: {e}. Kept them for now; check the source (a page or login change?).",
            )
        if state is not None:
            self.conn.execute(
                "DELETE FROM source_state WHERE source = ? AND key = ?", (result.source.value, key)
            )
        return result

    def _persist(
        self,
        result: CollectResult,
        ids: dict[str, int],
        started: datetime,
        finished: datetime,
    ) -> list[int]:
        now = self.clock.now()
        touched: list[int] = []
        with transaction(self.conn):
            # An error result may still carry what did work (umd.io's other courses).
            if result.status in (RunStatus.OK, RunStatus.UNCHANGED, RunStatus.ERROR):
                repo.upsert_source_items(self.conn, result.items, ids, now)
                for code, window_start in result.complete_scopes.items():
                    if code not in ids:
                        continue
                    present = {i.external_id for i in result.items if i.course_code == code}
                    result = self._mark_absent(
                        result, ids, code, present=present, window_start=window_start, now=now
                    )
                for code, meetings in result.meetings.items():
                    if code in ids:
                        repo.replace_meetings(self.conn, ids[code], "umdio", meetings, now)
                for info in result.courses:
                    repo.update_course_info(self.conn, info, now)
                for code, modules in result.modules.items():
                    if code in ids:
                        repo.replace_modules(self.conn, ids[code], modules, now)
                for grade in result.grades:
                    repo.set_grade(self.conn, grade, now)
                repo.upsert_materials(self.conn, result.materials, ids, now)
                touched = repo.upsert_posts(self.conn, result.posts, ids, now)
                if (
                    result.status is not RunStatus.ERROR
                ):  # a suspicious listing is fetched again in full
                    for key, state in result.state_updates.items():
                        repo.set_source_state(self.conn, result.source.value, key, state, now)
            repo.record_sync_run(
                self.conn,
                source=result.source.value,
                started_at=started,
                finished_at=finished,
                status=result.status.value,
                error=result.error,
                records=result.record_count,
            )
        return touched
