"""Shared rendering: theme cookie, stale banners, boot data, HTMX toasts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from argus.ai.client import AIClient, build_transport
from argus.logging import get_logger
from argus.web.data import configured_sources, notices
from argus.web.deps import THEME_COOKIE, AppState
from argus.web.study_data import open_proposal_count

log = get_logger(__name__)
LOAD_ERROR = (
    "Couldn't load this page: the database did not respond. Argus keeps syncing in the background."
)


def templates(request: Request) -> Jinja2Templates:
    t: Jinja2Templates = request.app.state.templates
    return t


def configured(state: AppState) -> dict[str, bool]:
    assert state.fetcher is not None
    return configured_sources(state.config, state.secrets, state.fetcher, state.clock)


def render(
    request: Request,
    state: AppState,
    conn: sqlite3.Connection | None,
    template: str,
    context: dict[str, Any],
    *,
    active: str,
    title: str,
    status_code: int = 200,
) -> HTMLResponse:
    now = state.clock.now()
    stale = []
    proposals = 0
    if conn is not None:
        try:
            stale = notices(conn, configured(state), now)
            proposals = open_proposal_count(conn)
        except sqlite3.Error:
            log.exception("could not load source health for banners")
    ctx = {
        "page_title": title,
        "active": active,
        "theme": request.cookies.get(THEME_COOKIE, "system"),
        "stale": stale,
        "open_proposals": proposals,
        "boot": {"now": now.isoformat(), "page": active},
        **context,
    }
    return templates(request).TemplateResponse(request, template, ctx, status_code=status_code)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def toast(response: Response, message: str, kind: str = "ok") -> Response:
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": message, "kind": kind}})
    return response


def back(request: Request, fallback: str = "/today") -> RedirectResponse:
    """303 to the referring page on this host (no-JS form posts)."""
    from urllib.parse import urlsplit

    ref = request.headers.get("referer")
    target = fallback
    if ref:
        parts = urlsplit(ref)
        if parts.netloc == request.headers.get("host") and parts.path.startswith("/"):
            target = parts.path + (f"?{parts.query}" if parts.query else "")
    return RedirectResponse(target, status_code=303)


def ai_client(state: AppState, conn: sqlite3.Connection) -> AIClient:
    transport = state.ai_transport
    if transport is None and state.secrets.anthropic_api_key is not None:
        transport = build_transport(state.secrets.anthropic_api_key.get_secret_value())
    return AIClient(conn, state.config.ai, state.clock, transport)
