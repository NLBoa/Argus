"""The always-on worker: every minute, run due collectors, then the reminder engine.

Each step is isolated: a crash in one is logged and the loop continues. Deadline reminders
depend only on stored data, so they keep working when every source is down.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import httpx
from pydantic import SecretStr

from argus.ai.client import AIClient, Transport, build_transport
from argus.announcements import scan_posts
from argus.clock import Clock
from argus.collectors.http import AuthError, Fetcher, FetchError
from argus.collectors.runner import SyncRunner
from argus.config import AppConfig, Secrets
from argus.estimates import ensure_estimates
from argus.exams import sync_exams
from argus.logging import get_logger
from argus.notify.ntfy import NtfyChannel
from argus.notify.router import Router
from argus.notify.twilio_sms import TwilioChannel
from argus.planning import explain, replan
from argus.reminders.engine import ReminderEngine
from argus.store import repo
from argus.study import ensure_study, targeted_practice
from argus.vault import service as vault

log = get_logger(__name__)
TICK_SECONDS = 60.0
PING_EVERY = timedelta(minutes=5)
SYNC_BUDGET = 240.0  # seconds; each collector is bounded at 180 s and they run concurrently
AI_BUDGET = 90.0


def build_router(config: AppConfig, secrets: Secrets, fetcher: Fetcher) -> Router:
    """SMS first once Twilio is set up; ntfy is the backup (and the only channel until then)."""

    def plain(value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value else None

    sms = TwilioChannel(
        fetcher.client,
        plain(secrets.twilio_account_sid),
        plain(secrets.twilio_auth_token),
        plain(secrets.twilio_from_number),
        plain(secrets.user_phone_number),
    )
    ntfy = NtfyChannel(fetcher, config.notifications.ntfy_server, plain(secrets.ntfy_topic))
    return Router([sms, ntfy])


class Worker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: AppConfig,
        secrets: Secrets,
        clock: Clock,
        client: httpx.AsyncClient,
        *,
        runner_factory: Callable[[Fetcher], SyncRunner] | None = None,
        transport: Transport | None = None,
    ) -> None:
        fetcher = Fetcher(client)
        self.conn = conn
        self.config = config
        self.clock = clock
        key = secrets.anthropic_api_key.get_secret_value() if secrets.anthropic_api_key else None
        self.ai = AIClient(
            conn, config.ai, clock, transport if transport is not None else build_transport(key)
        )
        self.runner = (
            runner_factory(fetcher)
            if runner_factory
            else SyncRunner(conn, config, secrets, clock, fetcher)
        )
        self.engine = ReminderEngine(conn, config, clock, build_router(config, secrets, fetcher))
        self._ping_url = (
            secrets.healthchecks_url.get_secret_value() if secrets.healthchecks_url else None
        )
        self._pinger = Fetcher(client, max_attempts=1)
        self._last_ping: datetime | None = None

    async def tick(self) -> None:
        """One minute of work. Each step is isolated; reminders run even if all else fails."""
        # Review R5: reminders run first, so nothing (a slow API, a hung source) delays them, and
        # again after planning, so new sessions and items are picked up in the same minute. Every
        # other step has a time budget; a step that runs out is cancelled and retried next tick.
        steps: list[tuple[str, Callable[[], Awaitable[object]], float | None]] = [
            ("reminders", self.engine.tick, None),
            ("sync", self.runner.run_cycle, SYNC_BUDGET),
            (
                "announcements",
                lambda: scan_posts(self.conn, self.ai, self.config, self.clock),
                AI_BUDGET,
            ),
            (
                "estimates",
                lambda: ensure_estimates(self.conn, self.ai, self.config, self.clock),
                AI_BUDGET,
            ),
            ("study", self._study, AI_BUDGET),
            ("plan", self._plan, AI_BUDGET),
            ("reminders", self.engine.tick, None),
            (
                "vault",
                self._vault,
                None,
            ),  # last: reminders never wait on git (each git call has a timeout)
        ]
        crashed = []
        for name, step, budget in steps:
            try:
                if budget is None:
                    await step()
                else:
                    await asyncio.wait_for(step(), timeout=budget)
            except TimeoutError:
                log.warning("worker step ran out of time", extra={"step": name, "budget_s": budget})
            except Exception:
                crashed.append(name)
                log.exception("worker step crashed", extra={"step": name})
        broken = (
            "reminders" in crashed or repo.failing_deadline_sends(self.conn, self.clock.now()) > 0
        )
        await self._heartbeat(failed=broken)

    async def _heartbeat(self, *, failed: bool) -> None:
        """Dead-man's switch (healthchecks.io style): ping every 5 minutes while reminders work,
        `/fail` when they crash or deadline reminders can't be delivered (review R4). If the Pi,
        the worker, or the network dies, the pings stop and the external service alerts the
        student by email or text."""
        now = self.clock.now()
        if not self._ping_url:
            return
        if not failed and self._last_ping is not None and now - self._last_ping < PING_EVERY:
            return
        try:
            await self._pinger.get(self._ping_url.rstrip("/") + ("/fail" if failed else ""))
        except (AuthError, FetchError) as e:
            log.warning("heartbeat ping failed", extra={"error": str(e)})
            return
        self._last_ping = now

    async def _plan(self) -> None:
        if replan(self.conn, self.config, self.clock) and self.ai.available:
            await explain(self.conn, self.ai, self.config, self.clock)

    async def _study(self) -> None:
        now = self.clock.now()
        sync_exams(self.conn, now)
        await ensure_study(self.conn, self.ai, self.config, self.clock)
        for entry_id, concept_id, text in vault.pending_practice(self.conn):
            if not self.ai.available:
                break
            await targeted_practice(self.conn, self.ai, self.clock, concept_id, text)
            vault.mark_practiced(self.conn, entry_id, self.clock.now())

    async def _vault(self) -> None:
        # Blocking git calls (each with a timeout) every 15 minutes: the loop is single-user.
        if vault.due(self.conn, self.clock.now()):
            vault.run_cycle(self.conn, self.config, self.clock)

    async def run(self, stop: asyncio.Event, tick_seconds: float = TICK_SECONDS) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
            except TimeoutError:
                continue
