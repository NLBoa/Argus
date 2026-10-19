"""Announcements pipeline: scan new or edited posts, extract findings, decide, apply.

AI is optional. Without it (no key, budget reached, API down), posts stay unscanned and the
Announcements view lists them as "Not analyzed yet": the student still sees every staff post.
Applying a finding never touches an official date; those wait for the student (confirm/dismiss).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from argus.ai.client import AIClient
from argus.ai.extract import extract
from argus.clock import Clock, local_datetime, to_local
from argus.config import AppConfig
from argus.domain.findings import (
    Action,
    Finding,
    PostContext,
    decide,
    link_announced,
    match_item,
    matches_keywords,
    normalize,
    status_for,
)
from argus.domain.models import ItemKind, Source
from argus.domain.titles import infer_kind
from argus.logging import get_logger
from argus.reminders.text import when
from argus.store import repo
from argus.store.db import transaction

if TYPE_CHECKING:  # capabilities imports the sync runner, which imports this module
    from argus.capabilities import Capabilities

log = get_logger(__name__)
RETRY_LATER = frozenset({"unavailable", "budget", "error"})


@dataclass
class ScanReport:
    scanned: int = 0
    analyzed: int = 0
    findings: int = 0
    applied: int = 0
    waiting: int = 0  # pending + conflict
    deferred: int = 0  # AI not available now; retried later


SCAN_LOOKAHEAD = 500  # unscanned posts considered per pass, before hidden ones are dropped


def post_visible(conn: sqlite3.Connection, caps: Capabilities, post_id: int) -> bool:
    """Whether a stored post's source is still set up for its course (D055)."""
    stored = repo.get_post(conn, post_id)
    return stored is not None and caps.shows_post(
        platform=stored.record.platform, kind=stored.record.kind, course=stored.record.course_code
    )


def worth_analyzing(post: repo.StoredPost) -> bool:
    p = post.record
    return p.author_role.is_staff or p.pinned or matches_keywords(f"{p.title}\n{p.body}")


def _context(post: repo.StoredPost) -> PostContext:
    p = post.record
    return PostContext(p.course_code, p.author_role, p.pinned, p.posted_at, p.title, p.body)


def _apply(
    conn: sqlite3.Connection,
    *,
    finding_id: int,
    f: Finding,
    action: Action,
    item_id: int | None,
    url: str | None,
    now: datetime,
) -> None:
    course_id = repo.course_ids(conn)[f.course_code]
    kind = infer_kind(f.title, ItemKind.ASSIGNMENT)
    if action in (Action.APPLY_NEW, Action.APPLY_DATE):
        repo.add_finding_source(
            conn,
            finding_id=finding_id,
            course_id=course_id,
            title=f.title,
            kind=kind,
            due_at=f.date,
            date_only=f.date_only,
            url=url,
            item_id=item_id if action == Action.APPLY_DATE else None,
            now=now,
        )
    elif action == Action.APPLY_INFO and item_id is not None:
        text = f.scope or f.quote
        repo.set_item_info(
            conn,
            item_id,
            scope=text if f.kind == "scope" else None,
            logistics=text if f.kind == "logistics" else None,
        )


async def scan_posts(
    conn: sqlite3.Connection,
    ai: AIClient,
    config: AppConfig,
    clock: Clock,
    *,
    caps: Capabilities,
    limit: int = 20,
) -> ScanReport:
    """Posts from a source that is no longer set up are left unscanned (D055): nothing they
    say may change a date. They are scanned if the source comes back."""
    report = ScanReport()
    tracking_sources = caps.tracking_sources
    courses = [c.code for c in config.courses]
    visible = [
        p
        for p in repo.posts_to_scan(conn, SCAN_LOOKAHEAD)
        if caps.shows_post(
            platform=p.record.platform, kind=p.record.kind, course=p.record.course_code
        )
    ]
    for post in visible[:limit]:
        report.scanned += 1
        if not worth_analyzing(post):
            with transaction(conn):
                repo.mark_scanned(conn, post.id)
            continue
        known = repo.item_refs(conn, post.record.course_code)
        result = await extract(ai, post.record, courses, known)
        if result.status in RETRY_LATER:
            report.deferred += 1
            continue
        report.analyzed += 1
        now = clock.now()
        ctx = _context(post)
        with transaction(conn):
            repo.supersede_open_findings(conn, post.id, now)
            for out in result.value.findings if result.value else []:
                f = normalize(out, ctx)
                if f is None:
                    log.info("finding discarded by guards", extra={"post": post.id})
                    continue
                target = match_item(f, known)
                decision = decide(f, ctx, target)
                fid = repo.insert_finding(
                    conn,
                    post_id=post.id,
                    course_id=repo.course_ids(conn)[f.course_code],
                    kind=f.kind,
                    title=f.title,
                    date=f.date,
                    date_only=f.date_only,
                    scope=f.scope,
                    quote=f.quote,
                    confidence=f.confidence,
                    item_id=decision.item_id,
                    status=status_for(decision.action),
                    reason=decision.reason,
                    now=now,
                )
                _apply(
                    conn,
                    finding_id=fid,
                    f=f,
                    action=decision.action,
                    item_id=decision.item_id,
                    url=post.record.url,
                    now=now,
                )
                report.findings += 1
                report.applied += decision.action in (
                    Action.APPLY_NEW,
                    Action.APPLY_DATE,
                    Action.APPLY_INFO,
                )
                report.waiting += decision.action in (Action.HOLD, Action.CONFLICT)
            repo.mark_scanned(conn, post.id)
            ids = repo.course_ids(conn)
            repo.apply_merge(conn, ids, now)
            _link_finding_items(conn)
            repo.refresh_statuses(conn, now, tracking_sources=tracking_sources)
    return report


def _link_finding_items(conn: sqlite3.Connection) -> None:
    """Record which item each applied new-assessment finding created."""
    for f in repo.list_findings(conn, ["applied"]):
        if f.item_id is None:
            created = repo.item_for_source(conn, Source.FINDING, f"finding:{f.id}")
            if created is not None:
                conn.execute("UPDATE findings SET item_id = ? WHERE id = ?", (created, f.id))


def attach_official(conn: sqlite3.Connection) -> int:
    """Link newly listed official records to announced-only items (before merging)."""
    links = link_announced(repo.unlinked_official_records(conn), repo.announced_only_items(conn))
    for key, item_id in links.items():
        repo.link_source(conn, key, item_id)
    return len(links)


def confirm(
    conn: sqlite3.Connection, finding_id: int, now: datetime, *, tracking_sources: frozenset[str]
) -> str:
    """The student says the announcement is right. Returns a message for the toast."""
    f = repo.get_finding(conn, finding_id)
    if f is None or f.status not in ("pending", "conflict"):
        raise LookupError("This finding is no longer waiting for you")
    finding = Finding(
        f.kind, f.course_code, f.title, f.date, f.date_only, f.scope, f.quote, f.confidence
    )
    with transaction(conn):
        if f.kind == "cancellation" and f.item_id is not None:
            repo.set_override(
                conn,
                f.item_id,
                due_at=None,
                cancelled=True,
                reason=f"Confirmed: {f.quote}",
                now=now,
            )
            message = f"{f.title} marked cancelled"
        elif f.status == "conflict" and f.item_id is not None and f.date is not None:
            due = _keep_time_of_day(conn, f.item_id, f.date) if f.date_only else f.date
            repo.set_override(
                conn,
                f.item_id,
                due_at=due,
                cancelled=False,
                reason=f"Confirmed: {f.quote}",
                now=now,
            )
            message = f"Using the announced date for {f.title}: {when(due)}"
        elif f.kind in ("scope", "logistics") and f.item_id is not None:
            _apply(
                conn,
                finding_id=f.id,
                f=finding,
                action=Action.APPLY_INFO,
                item_id=f.item_id,
                url=None,
                now=now,
            )
            message = f"Added to {f.title}"
        elif f.kind in ("new_assessment", "date_change"):
            action = Action.APPLY_DATE if f.item_id is not None else Action.APPLY_NEW
            _apply(
                conn,
                finding_id=f.id,
                f=finding,
                action=action,
                item_id=f.item_id,
                url=None,
                now=now,
            )
            message = f"Added {f.title}" if action == Action.APPLY_NEW else f"Updated {f.title}"
        else:
            message = "Noted"
        repo.set_finding_status(conn, f.id, "confirmed", now)
        ids = repo.course_ids(conn)
        repo.apply_merge(conn, ids, now)
        _link_finding_items(conn)
        repo.refresh_statuses(conn, now, tracking_sources=tracking_sources)
        repo.rebuild_search_index(conn)
    return message


def _keep_time_of_day(conn: sqlite3.Connection, item_id: int, new_day: datetime) -> datetime:
    """A date-only announcement moves the day; the official time of day stays."""
    item = repo.get_item(conn, item_id)
    if item is None or item.due_at is None:
        return new_day
    old = to_local(item.due_at)
    return local_datetime(to_local(new_day).date(), old.hour, old.minute)


def dismiss(conn: sqlite3.Connection, finding_id: int, now: datetime) -> str:
    f = repo.get_finding(conn, finding_id)
    if f is None or f.status not in ("pending", "conflict"):
        raise LookupError("This finding is no longer waiting for you")
    with transaction(conn):
        repo.set_finding_status(conn, f.id, "dismissed", now)
    return f"Kept the official date for {f.title}" if f.status == "conflict" else "Dismissed"
