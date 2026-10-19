"""Canvas tier 2: REST API with a personal access token (only if UMD allows tokens).

Phase 1 covers assignments with the student's submission state. Modules, announcements,
and files arrive in Phase 3. Quirk: pagination uses `Link` headers. Follow `rel="next"`,
never compute page numbers.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.text import html_to_text
from argus.domain.models import ItemKind, Source, SourceItem
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
        status = RunStatus.OK if scopes else RunStatus.UNCHANGED
        return CollectResult(
            self.source, status, items=tuple(items), complete_scopes=scopes, state_updates=updates
        )
