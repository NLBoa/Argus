"""Canvas tier 2: REST API with a personal access token (only if UMD allows tokens).

Every 30 minutes: assignments with the student's submission state, and course
announcements (which become posts). Modules, the syllabus, and grades are nightly
(`canvas_modules.py`). Quirk: pagination uses `Link` headers. Follow `rel="next"`,
never compute page numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.text import html_to_text
from argus.domain.models import AuthorRole, ItemKind, PostRecord, Source, SourceItem
from argus.domain.titles import infer_kind
from argus.store.repo import SourceState, sha256

SUBMITTED_STATES = frozenset({"submitted", "pending_review", "graded"})
NO_SUBMISSION_TYPES = frozenset({"none", "on_paper", "not_graded"})


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Canvas returned a timestamp without zone: {value!r}")
    return dt


def parse_assignment(a: dict[str, Any], course_code: str) -> SourceItem:
    types = set(a.get("submission_types") or [])
    sub = a.get("submission") or {}
    state = sub.get("workflow_state")
    submitted: bool | None
    if (types and types <= NO_SUBMISSION_TYPES) or state is None:
        submitted = None
    else:
        submitted = state in SUBMITTED_STATES
    graded = None if state is None else (state == "graded" and sub.get("score") is not None)
    is_quiz = bool(a.get("is_quiz_assignment")) or "online_quiz" in types
    title = str(a.get("name") or "(untitled)")
    due = _dt(a.get("due_at"))
    lock = _dt(a.get("lock_at"))
    points = a.get("points_possible")
    return SourceItem(
        source=Source.CANVAS_API,
        external_id=str(a["id"]),
        course_code=course_code,
        title=title,
        kind=ItemKind.QUIZ if is_quiz else infer_kind(title, ItemKind.ASSIGNMENT),
        due_at=due,
        release_at=_dt(a.get("unlock_at")),
        late_due_at=lock if (lock and due and lock > due) else None,
        points=float(points) if points is not None else None,
        submitted=submitted,
        graded=graded,
        description=html_to_text(a.get("description")),
        url=a.get("html_url"),
        raw_hash=sha256(json.dumps(a, sort_keys=True)),
    )


ANNOUNCEMENT_LOOKBACK = timedelta(days=60)


def parse_announcement(a: dict[str, Any], course_code: str) -> PostRecord:
    title = str(a.get("title") or "(untitled)")
    body = html_to_text(a.get("message")) or ""
    posted = _dt(a.get("posted_at") or a.get("created_at"))
    if posted is None:
        raise ValueError(f"announcement {a.get('id')} has no posted_at")
    return PostRecord(
        platform="canvas",
        external_id=f"announcement:{a['id']}",
        via=Source.CANVAS_API,
        course_code=course_code,
        title=title,
        body=body,
        posted_at=posted,
        # Only instructors and TAs can post course announcements in Canvas.
        author_role=AuthorRole.INSTRUCTOR,
        kind="announcement",
        url=a.get("html_url"),
        raw_hash=sha256(json.dumps([title, body])),
    )


class CanvasApiCollector:
    source = Source.CANVAS_API

    def enabled(self, ctx: CollectContext) -> bool:
        return ctx.secrets.canvas_token is not None and any(c.canvas_id for c in ctx.config.courses)

    async def collect(self, ctx: CollectContext) -> CollectResult:
        assert ctx.secrets.canvas_token is not None
        headers = {"Authorization": f"Bearer {ctx.secrets.canvas_token.get_secret_value()}"}
        base = ctx.config.canvas.base_url.rstrip("/")
        items: list[SourceItem] = []
        scopes: dict[str, datetime | None] = {}
        updates: dict[str, SourceState] = {}
        for course in ctx.config.courses:
            if course.canvas_id is None:
                continue
            key = f"assignments:{course.canvas_id}"
            pages = await ctx.fetcher.get_all_pages(
                f"{base}/api/v1/courses/{course.canvas_id}/assignments",
                params={"include[]": "submission", "per_page": "100", "order_by": "due_at"},
                headers=headers,
            )
            digest = sha256(b"".join(p.body for p in pages))
            if digest == ctx.state(self.source, key).payload_hash:
                continue
            for page in pages:
                data = page.json()
                if not isinstance(data, list):
                    raise ValueError(f"expected a list of assignments for {course.code}")
                items.extend(parse_assignment(a, course.code) for a in data)
            scopes[course.code] = None
            updates[key] = SourceState(payload_hash=digest)
        posts = await self._announcements(ctx, base, headers, updates)
        status = RunStatus.OK if scopes or posts else RunStatus.UNCHANGED
        return CollectResult(
            self.source,
            status,
            items=tuple(items),
            posts=tuple(posts),
            complete_scopes=scopes,
            state_updates=updates,
        )

    async def _announcements(
        self,
        ctx: CollectContext,
        base: str,
        headers: dict[str, str],
        updates: dict[str, SourceState],
    ) -> list[PostRecord]:
        by_context = {f"course_{c.canvas_id}": c.code for c in ctx.config.courses if c.canvas_id}
        if not by_context:
            return []
        now = ctx.clock.now()
        pages = await ctx.fetcher.get_all_pages(
            f"{base}/api/v1/announcements",
            params={
                "context_codes[]": sorted(by_context),
                "start_date": (now - ANNOUNCEMENT_LOOKBACK).date().isoformat(),
                "end_date": (now + timedelta(days=1)).date().isoformat(),
                "per_page": "50",
            },
            headers=headers,
        )
        digest = sha256(b"".join(p.body for p in pages))
        if digest == ctx.state(self.source, "announcements").payload_hash:
            return []
        posts = []
        for page in pages:
            data = page.json()
            if not isinstance(data, list):
                raise ValueError("expected a list of announcements")
            for a in data:
                code = by_context.get(str(a.get("context_code")))
                if code:
                    posts.append(parse_announcement(a, code))
        updates["announcements"] = SourceState(payload_hash=digest)
        return posts
