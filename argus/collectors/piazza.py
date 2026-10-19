"""Piazza via the unofficial `piazza-api` library (pinned; it can break without warning).

One cycle per network: one feed request, then full fetches only for staff-relevant
posts (pinned, instructor notes, posts with an instructor answer, or keyword matches) whose
`updated` stamp changed since last time. At most `MAX_POSTS_PER_CYCLE` per network per cycle;
the rest follow next cycle. If login fails, the runner switches to the email fallback.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Protocol

from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.http import AuthError, FetchError
from argus.collectors.text import html_to_text
from argus.domain.findings import matches_keywords
from argus.domain.models import AuthorRole, PostRecord, Source
from argus.store.repo import SourceState, sha256

MAX_POSTS_PER_CYCLE = 25
FEED_LIMIT = 100
_ROLES = {
    "instructor": AuthorRole.INSTRUCTOR,
    "professor": AuthorRole.INSTRUCTOR,
    "ta": AuthorRole.TA,
    "student": AuthorRole.STUDENT,
}


class PiazzaClient(Protocol):
    def login(self, email: str, password: str) -> None: ...
    def feed(self, nid: str, limit: int) -> list[dict[str, Any]]: ...
    def post(self, nid: str, cid: str) -> dict[str, Any]: ...
    def users(self, nid: str, uids: list[str]) -> list[dict[str, Any]]: ...


class LibPiazzaClient:
    """Adapter over piazza-api. Isolates the unofficial API to this class."""

    def __init__(self) -> None:
        from piazza_api import Piazza

        self._p = Piazza()

    def login(self, email: str, password: str) -> None:
        from piazza_api.exceptions import AuthenticationError

        try:
            self._p.user_login(email=email, password=password)
        except AuthenticationError as e:
            raise AuthError("Piazza rejected the credentials") from e

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        from piazza_api.exceptions import RequestError

        try:
            return fn(*args, **kwargs)
        except RequestError as e:
            raise FetchError(f"Piazza request failed: {type(e).__name__}") from e

    def feed(self, nid: str, limit: int) -> list[dict[str, Any]]:
        result = self._call(self._p.network(nid).get_feed, limit=limit, offset=0)
        if not isinstance(result, dict) or not isinstance(result.get("feed"), list):
            raise FetchError("Piazza feed format changed")
        return list(result["feed"])

    def post(self, nid: str, cid: str) -> dict[str, Any]:
        post = self._call(self._p.network(nid).get_post, cid)
        if not isinstance(post, dict):
            raise FetchError("Piazza post format changed")
        return post

    def users(self, nid: str, uids: list[str]) -> list[dict[str, Any]]:
        if not uids:
            return []
        return list(self._call(self._p.network(nid).get_users, uids) or [])


def is_candidate(entry: dict[str, Any]) -> bool:
    tags = entry.get("tags") or []
    if entry.get("pin") or "instructor-note" in tags or entry.get("has_i"):
        return True
    return matches_keywords(f"{entry.get('subject', '')} {entry.get('content_snipet', '')}")


def _ts(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Piazza timestamp without zone: {value!r}")
    return dt


def parse_post(
    post: dict[str, Any],
    *,
    nid: str,
    course_code: str,
    pinned: bool,
    roles: dict[str, AuthorRole],
) -> list[PostRecord]:
    """The post itself, plus a separate record for an instructor answer if there is one."""
    history = post.get("history") or []
    if not history:
        raise ValueError("Piazza post without history")
    latest = history[0]
    nr = post["nr"]
    tags = post.get("tags") or []
    change_log = post.get("change_log") or [{}]
    author_uid = change_log[0].get("uid") or latest.get("uid")
    if "instructor-note" in tags:
        role = AuthorRole.INSTRUCTOR
    else:
        role = roles.get(str(author_uid), AuthorRole.UNKNOWN) if author_uid else AuthorRole.UNKNOWN
    created = _ts(post.get("created") or latest["created"])
    edited = _ts(latest["created"]) if len(history) > 1 else None
    url = f"https://piazza.com/class/{nid}/post/{nr}"
    title = str(latest.get("subject") or "(untitled)")
    body = html_to_text(latest.get("content")) or ""
    records = [
        PostRecord(
            platform="piazza",
            external_id=f"{nid}:{nr}",
            via=Source.PIAZZA,
            course_code=course_code,
            title=title,
            body=body,
            posted_at=created,
            edited_at=edited,
            author_role=role,
            kind="note" if post.get("type") == "note" else "question",
            url=url,
            pinned=pinned,
            raw_hash=sha256(json.dumps([title, body, role.value, pinned, str(edited)])),
        )
    ]
    for child in post.get("children") or []:
        if child.get("type") != "i_answer" or not child.get("history"):
            continue
        answer = child["history"][0]
        text = html_to_text(answer.get("content")) or ""
        records.append(
            PostRecord(
                platform="piazza",
                external_id=f"{nid}:{nr}:i_answer",
                via=Source.PIAZZA,
                course_code=course_code,
                title=title,
                body=text,
                posted_at=_ts(child.get("created") or answer["created"]),
                edited_at=_ts(answer["created"]) if len(child["history"]) > 1 else None,
                author_role=AuthorRole.INSTRUCTOR,
                kind="instructor_answer",
                url=url,
                pinned=pinned,
                raw_hash=sha256(json.dumps([title, text])),
            )
        )
    return records


class PiazzaCollector:
    source = Source.PIAZZA

    def __init__(self, client_factory: type[PiazzaClient] | None = None) -> None:
        self._factory = client_factory

    def enabled(self, ctx: CollectContext) -> bool:
        s = ctx.secrets
        return bool(s.piazza_email and s.piazza_password) and any(
            c.piazza_network_id for c in ctx.config.courses
        )

    def _fetch(self, ctx: CollectContext) -> tuple[list[PostRecord], dict[str, SourceState]]:
        assert ctx.secrets.piazza_email
        assert ctx.secrets.piazza_password
        client: PiazzaClient = (self._factory or LibPiazzaClient)()
        client.login(ctx.secrets.piazza_email, ctx.secrets.piazza_password.get_secret_value())
        posts: list[PostRecord] = []
        updates: dict[str, SourceState] = {}
        for course in ctx.config.courses:
            nid = course.piazza_network_id
            if not nid:
                continue
            key = f"versions:{nid}"
            known: dict[str, str] = json.loads(ctx.state(self.source, key).data or "{}")
            stale = [
                e
                for e in client.feed(nid, FEED_LIMIT)
                if is_candidate(e) and known.get(str(e["id"])) != str(e.get("updated"))
            ]
            stale.sort(key=lambda e: str(e.get("updated")), reverse=True)
            batch = stale[:MAX_POSTS_PER_CYCLE]
            if not batch:
                continue
            fetched = [(e, client.post(nid, str(e["id"]))) for e in batch]
            uids = sorted(
                {
                    str(uid)
                    for _, p in fetched
                    if (uid := ((p.get("change_log") or [{}])[0].get("uid")))
                }
            )
            roles = {
                str(u["id"]): _ROLES.get(str(u.get("role", "")).lower(), AuthorRole.UNKNOWN)
                for u in client.users(nid, uids)
                if "id" in u
            }
            for entry, post in fetched:
                posts.extend(
                    parse_post(
                        post,
                        nid=nid,
                        course_code=course.code,
                        pinned=bool(entry.get("pin")),
                        roles=roles,
                    )
                )
                known[str(entry["id"])] = str(entry.get("updated"))
            updates[key] = SourceState(data=json.dumps(known, sort_keys=True))
        return posts, updates

    async def collect(self, ctx: CollectContext) -> CollectResult:
        posts, updates = await asyncio.to_thread(self._fetch, ctx)
        status = RunStatus.OK if updates else RunStatus.UNCHANGED
        return CollectResult(self.source, status, posts=tuple(posts), state_updates=updates)
