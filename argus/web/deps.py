"""Shared web state and request dependencies (separate module to keep imports acyclic)."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from argus.clock import Clock
from argus.collectors.http import Fetcher
from argus.config import AppConfig, Secrets
from argus.store.db import connect
from argus.web import format as fmt

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
THEME_COOKIE = "argus_theme"


class Runner(Protocol):
    async def run_cycle(self, *, only: Any = None, force: bool = False) -> Any: ...


class OfflineRunner:
    """A runner that never touches the network (demo mode and browser tests)."""

    async def run_cycle(self, *, only: Any = None, force: bool = False) -> None:
        return None


@dataclass
class AppState:
    config: AppConfig
    secrets: Secrets
    clock: Clock
    db_path: Path
    runner_factory: Callable[[sqlite3.Connection, Fetcher], Runner] | None = None
    kiosk_interval: float = 30.0
    ai_transport: Any = None  # argus.ai.client.Transport; None = build from the API key
    fetcher: Fetcher | None = None
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class Assets:
    """Cache-busting static URLs: /static/<path>?v=<content hash>."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, tuple[float, str]] = {}

    def __call__(self, path: str) -> str:
        f = self.root / path
        try:
            mtime = f.stat().st_mtime
        except FileNotFoundError:
            return f"/static/{path}"
        cached = self._cache.get(path)
        if cached is None or cached[0] != mtime:
            cached = (mtime, hashlib.sha256(f.read_bytes()).hexdigest()[:10])
            self._cache[path] = cached
        return f"/static/{path}?v={cached[1]}"


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.loader = ChoiceLoader(
        [FileSystemLoader(WEB_DIR / "templates"), FileSystemLoader(WEB_DIR)]
    )
    templates.env.globals["asset"] = Assets(STATIC_DIR)
    templates.env.filters["hours"] = fmt.hours
    templates.env.filters["elapsed"] = fmt.elapsed
    templates.env.trim_blocks = True
    templates.env.lstrip_blocks = True
    return templates


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.argus
    return state


def get_conn(state: AppState = Depends(get_state)) -> Iterator[sqlite3.Connection]:  # noqa: B008
    conn = connect(state.db_path)
    try:
        yield conn
    finally:
        conn.close()
