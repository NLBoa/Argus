"""Shared rendering: theme cookie, the status line (D067), boot data, HTMX toasts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from argus.ai.budget import spent_this_month
from argus.ai.client import AIClient, build_transport
from argus.capabilities import Capabilities, Feature
from argus.logging import get_logger
from argus.vault.service import health as vault_health
from argus.web.data import source_statuses
from argus.web.deps import THEME_COOKIE, AppState
from argus.web.viewmodels import StatusLine, status_line

log = get_logger(__name__)
LOAD_ERROR = (
    "Couldn't load this page: the database did not respond. Argus keeps syncing in the background."
)


def templates(request: Request) -> Jinja2Templates:
    t: Jinja2Templates = request.app.state.templates
    return t


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
    status = StatusLine([], [])
    proposals = 0
    if conn is not None:
        try:
            vault = vault_health(conn)
            proposals = vault.open_proposals
            status = status_line(
                source_statuses(conn, state.caps.sources, now),
                proposals=proposals,
                vault_failures=vault.failures,
                vault_conflict=vault.last_status == "conflict",
                page=active,
            )
        except sqlite3.Error:
            log.exception("could not load source health for the status line")
    ctx = {
        "caps": page_caps(state, conn, now),
        "page_title": title,
        "active": active,
        "theme": request.cookies.get(THEME_COOKIE, "system"),
        "status": status,
        "open_proposals": proposals,
        "boot": {"now": now.isoformat(), "page": active, "pages": hidden_pages(state.caps)},
        **context,
    }
    return templates(request).TemplateResponse(request, template, ctx, status_code=status_code)


def page_caps(state: AppState, conn: sqlite3.Connection | None, now: datetime) -> Capabilities:
    """The app's capabilities plus this month's AI budget (it hides only new-call buttons)."""
    if conn is None:
        return state.caps.with_budget(spent=0.0, cap=0.0)
    try:
        spent = spent_this_month(conn, now)
    except sqlite3.Error:
        log.exception("could not read AI spend")
        spent = state.config.ai.monthly_budget_usd
    return state.caps.with_budget(spent=spent, cap=state.config.ai.monthly_budget_usd)


def hidden_pages(caps: Capabilities) -> list[str]:
    """Pages the setup cannot support, so the palette and shortcuts skip them (D055)."""
    return [] if caps.has(Feature.STAFF_POSTS) else ["/announcements"]


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
