"""The Claude connector: a small, read-only MCP server over the Argus database (D073).

Run it with `argus serve-mcp` on its own port and put only that port behind Tailscale Funnel
(docs/runbook.md). It never fetches from Canvas or Gradescope; it reads what the worker stored,
through a `mode=ro` SQLite connection, so it cannot write.

Auth is a long random secret in the URL path (`/mcp/<secret>`, from the `mcp_path_secret`
credential). Every other path is a 404. The secret is registered with the log redactor and the
access log is off, so it never reaches the journal.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette

from argus.clock import Clock
from argus.config import AppConfig, Secrets
from argus.feed import MAX_DAYS, Changes, CourseList, UpcomingWork, parse_since
from argus.feed import changes as feed_changes
from argus.feed import courses as feed_courses
from argus.feed import upcoming as feed_upcoming
from argus.store.db import connect_readonly

SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,}$")
_LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
# claude.ai calls server-to-server (no Origin); the Inspector's proxy runs on localhost.
_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "https://claude.ai"]
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
INSTRUCTIONS = """\
Argus is one UMD student's course hub. These tools read the deadlines it has already collected \
from Canvas (ELMS), Gradescope, and the student's Runna running plan. They are read-only. Times are ISO 8601 with the \
America/New_York offset. Item ids are stable, so use them to match items across calls \
(for example, to the tasks you keep in a planner). get_changes returns `as_of`; pass it as \
`since` next time. Deadlines and links only: Argus never shares grades or submissions. \
Runs (type run) are due by the end of their day and are flexible: schedule one near \
preferred_start if that time is free, otherwise any free time that day, for estimate_minutes. \
A run drops out once its day ends; don't reschedule it."""

T = TypeVar("T")


def path_secret(secrets: Secrets) -> str:
    """The URL path secret, or ValueError when it is missing or too weak."""
    value = secrets.mcp_path_secret.get_secret_value() if secrets.mcp_path_secret else ""
    if not value:
        raise ValueError("mcp_path_secret is not set (ARGUS_MCP_PATH_SECRET or a credential file)")
    if not SECRET_PATTERN.fullmatch(value):
        raise ValueError(
            "mcp_path_secret must be at least 32 URL-safe characters (A-Z a-z 0-9 _ -); "
            "make one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return value


def build_server(cfg: AppConfig, secrets: Secrets, clock: Clock, db_path: Path) -> FastMCP:
    secret = path_secret(secrets)
    hosts = [*_LOCAL_HOSTS]
    for host in cfg.mcp.allowed_hosts:
        hosts += [host, f"{host}:*"]
    server = FastMCP(
        "argus",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path=f"/mcp/{secret}",
        log_level="WARNING",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=_ORIGINS
        ),
    )

    def read(query: Callable[[sqlite3.Connection], T]) -> T:
        conn = connect_readonly(db_path)
        try:
            return query(conn)
        finally:
            conn.close()

    @server.tool(title="List courses", annotations=_READ_ONLY)
    def list_courses() -> CourseList:
        """The student's current courses: code, name, and weekly meeting times
        (local America/New_York), when Argus has them."""
        return read(feed_courses)

    @server.tool(title="Get upcoming work", annotations=_READ_ONLY)
    def get_upcoming_work(
        days: Annotated[
            int, Field(ge=1, le=MAX_DAYS, description="How many days ahead to look (1-60).")
        ] = 14,
    ) -> UpcomingWork:
        """Assignments, quizzes, exams, projects, and course events due in the next `days`
        days, soonest first. Also includes work up to 7 days past due that isn't done
        (`overdue: true`), so late work isn't dropped. `done` is true once it was submitted
        or marked done in Argus. Runs are listed only until their day ends."""
        return read(lambda conn: feed_upcoming(conn, clock.now(), days))

    @server.tool(title="Get changes", annotations=_READ_ONLY)
    def get_changes(
        since: Annotated[
            str,
            Field(
                description="ISO 8601 instant with an offset, e.g. 2026-11-06T07:00:00-05:00. "
                "Use the `as_of` from your previous call."
            ),
        ],
    ) -> Changes:
        """Items that are new, had their due date changed, or were removed after `since`.
        Each has `status` (new, changed, removed), and `old_due` when changed."""
        start = parse_since(since)
        return read(lambda conn: feed_changes(conn, clock.now(), start))

    return server


def create_mcp_app(
    cfg: AppConfig, secrets: Secrets, clock: Clock, db_path: Path | None = None
) -> Starlette:
    """The ASGI app: one route, `/mcp/<secret>`; everything else is Starlette's 404."""
    return build_server(cfg, secrets, clock, db_path or cfg.paths.db_path).streamable_http_app()
