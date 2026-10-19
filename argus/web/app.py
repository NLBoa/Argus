"""FastAPI application factory.

`create_app` takes every dependency explicitly (config, secrets, clock, database path), so tests
and the e2e server build an app against fixtures and a frozen clock.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from argus import __version__
from argus.clock import Clock, SystemClock
from argus.collectors.http import Fetcher, make_client
from argus.config import AppConfig, Secrets
from argus.logging import get_logger
from argus.store.db import connect, migrate
from argus.web.deps import STATIC_DIR, AppState, Runner, make_templates
from argus.web.routes import actions, kiosk, pages, pwa

log = get_logger(__name__)
# State-changing routes that may be called cross-origin (they authenticate another way).
CROSS_ORIGIN_ALLOWED = ("/webhooks/",)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    host = request.headers.get("host", "")
    return urlsplit(origin).netloc == host


def create_app(
    config: AppConfig | None = None,
    secrets: Secrets | None = None,
    clock: Clock | None = None,
    *,
    db_path: Path | None = None,
    runner_factory: Callable[[sqlite3.Connection, Fetcher], Runner] | None = None,
    kiosk_interval: float = 30.0,
    ai_transport: Any = None,
) -> FastAPI:
    cfg = config or AppConfig()
    state = AppState(
        config=cfg,
        secrets=secrets or Secrets(),
        clock=clock or SystemClock(),
        db_path=db_path or cfg.paths.db_path,
        runner_factory=runner_factory,
        kiosk_interval=kiosk_interval,
        ai_transport=ai_transport,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        conn = connect(state.db_path)
        migrate(conn, state.clock)
        conn.close()
        async with make_client() as client:
            state.fetcher = Fetcher(client)
            yield
            for task in list(state.tasks):
                task.cancel()

    app = FastAPI(
        title="Argus",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.argus = state
    app.state.templates = make_templates()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def guard_and_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        exempt = request.url.path.startswith(CROSS_ORIGIN_ALLOWED)
        if unsafe and not exempt and not _same_origin(request):
            return PlainTextResponse("Cross-origin request refused.", status_code=403)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/static/") and "v=" in request.url.query:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    app.include_router(pages.router)
    app.include_router(actions.router)
    app.include_router(kiosk.router)
    app.include_router(pwa.router)
    return app
