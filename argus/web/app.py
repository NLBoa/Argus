"""FastAPI application factory.

`create_app` takes every dependency explicitly (config, secrets, clock) so tests and the
e2e server can build an app against fixtures and a fake clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from argus import __version__
from argus.clock import Clock, SystemClock
from argus.config import AppConfig, Secrets

WEB_DIR = Path(__file__).parent


@dataclass(frozen=True)
class AppState:
    config: AppConfig
    secrets: Secrets
    clock: Clock


def create_app(
    config: AppConfig | None = None,
    secrets: Secrets | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    state = AppState(
        config=config or AppConfig(),
        secrets=secrets or Secrets(),
        clock=clock or SystemClock(),
    )
    app = FastAPI(title="Argus", version=__version__, docs_url=None, redoc_url=None)
    app.state.argus = state
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "home.html", {"version": __version__})

    return app
