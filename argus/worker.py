"""The always-on worker: every minute, run due collectors, then the reminder engine.

Each step is isolated: a crash in one is logged and the loop continues. Deadline reminders
depend only on stored data, so they keep working when every source is down.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable

import httpx

from argus.clock import Clock
from argus.collectors.http import Fetcher
from argus.collectors.runner import SyncRunner
from argus.config import AppConfig, Secrets
from argus.logging import get_logger
from argus.notify.ntfy import NtfyChannel
from argus.notify.router import Router
from argus.reminders.engine import ReminderEngine

log = get_logger(__name__)
TICK_SECONDS = 60.0


def build_router(config: AppConfig, secrets: Secrets, fetcher: Fetcher) -> Router:
    topic = secrets.ntfy_topic.get_secret_value() if secrets.ntfy_topic else None
    return Router([NtfyChannel(fetcher, config.notifications.ntfy_server, topic)])


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
    ) -> None:
        fetcher = Fetcher(client)
        self.runner = (
            runner_factory(fetcher)
            if runner_factory
            else SyncRunner(conn, config, secrets, clock, fetcher)
        )
        self.engine = ReminderEngine(conn, config, clock, build_router(config, secrets, fetcher))

    async def tick(self) -> None:
        try:
            await self.runner.run_cycle()
        except Exception:
            log.exception("sync cycle crashed")
        try:
            await self.engine.tick()
        except Exception:
            log.exception("reminder tick crashed")

    async def run(self, stop: asyncio.Event, tick_seconds: float = TICK_SECONDS) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
            except TimeoutError:
                continue
