"""Piazza fallback: real-time Piazza notification emails in TerpMail, read through the Gmail API.

Used only when the Piazza API login fails (or no Piazza password is configured). The token file
(`ARGUS_GMAIL_TOKEN_PATH`) holds an OAuth refresh token with the `gmail.readonly` scope:
`{"client_id", "client_secret", "refresh_token", "token_uri"}`.

Quirk: the email format is not documented. The parser keys on the post link
(`piazza.com/class/<nid>/post/<nr>` or `?cid=<nr>`) and is tolerant of the rest.
Verify it against the first real emails (docs/runbook.md).
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.http import AuthError
from argus.config import AppConfig
from argus.domain.models import AuthorRole, PostRecord, Source
from argus.store.repo import SourceState, sha256

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
QUERY = "from:(no-reply@piazza.com OR notifications@piazza.com) newer_than:14d"
MAX_MESSAGES_PER_CYCLE = 25
SEEN_LIMIT = 500
STATE_KEY = "gmail"

_LINK = re.compile(r"piazza\.com/class/([a-z0-9]+)(?:/post/|\?cid=)(\d+)", re.IGNORECASE)
_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)?(?:re:\s*)?"
    r"(?:(?:new\s+)?(?:instr(?:uctor)?\.?\s+)?(?:post|note|question|announcement)"
    r"(?:\s+in\s+[\w\s]+?)?\s*[:\-\u2013]\s*)?",  # \u2013 is an en dash (re escape)
    re.IGNORECASE,
)
_STAFF = [
    (
        re.compile(r"\b(?:instructor|professor|instr\.?\s+note)\b", re.IGNORECASE),
        AuthorRole.INSTRUCTOR,
    ),
    (re.compile(r"\b(?:TA|teaching assistant)\b"), AuthorRole.TA),
]
_FOOTERS = (
    "Go to https://piazza.com",
    "Search or link to this",
    "You are receiving this",
    "Click here to",
    "\n-- \n",
)


def _header(msg: dict[str, Any], name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _plain_body(part: dict[str, Any]) -> str:
    if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
        data = part["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    for sub in part.get("parts") or []:
        text = _plain_body(sub)
        if text:
            return text
    return ""


def _trim(body: str) -> str:
    cut = len(body)
    for marker in _FOOTERS:
        i = body.find(marker)
        if i != -1:
            cut = min(cut, i)
    lines = (" ".join(line.split()) for line in body[:cut].replace("\r\n", "\n").split("\n"))
    return "\n".join(line for line in lines if line)[:4000]


def parse_message(msg: dict[str, Any], config: AppConfig) -> PostRecord | None:
    """A Piazza notification email -> PostRecord, or None if it is not one we can place."""
    raw_body = _plain_body(msg.get("payload", {}))
    link = _LINK.search(raw_body)
    by_nid = {c.piazza_network_id: c.code for c in config.courses if c.piazza_network_id}
    if not link or link.group(1) not in by_nid:
        return None
    nid, nr = link.group(1), link.group(2)
    subject = _header(msg, "Subject")
    title = _SUBJECT_PREFIX.sub("", subject, count=1).strip() or subject.strip() or "(untitled)"
    body = _trim(raw_body)
    head = f"{subject}\n{raw_body[:300]}"
    role = next((r for pattern, r in _STAFF if pattern.search(head)), AuthorRole.UNKNOWN)
    date_header = _header(msg, "Date")
    if date_header:
        posted = parsedate_to_datetime(date_header).astimezone(UTC)
    else:
        posted = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=UTC)
    return PostRecord(
        platform="piazza",
        external_id=f"{nid}:{nr}",
        via=Source.PIAZZA_EMAIL,
        course_code=by_nid[nid],
        title=title,
        body=body,
        posted_at=posted,
        author_role=role,
        kind="note",
        url=f"https://piazza.com/class/{nid}/post/{nr}",
        raw_hash=sha256(json.dumps([title, body, role.value])),
    )


class PiazzaEmailCollector:
    source = Source.PIAZZA_EMAIL

    def enabled(self, ctx: CollectContext) -> bool:
        path = ctx.secrets.gmail_token_path
        return (
            path is not None
            and Path(path).is_file()
            and any(c.piazza_network_id for c in ctx.config.courses)
        )

    async def _access_token(self, ctx: CollectContext) -> str:
        assert ctx.secrets.gmail_token_path is not None
        creds = json.loads(Path(ctx.secrets.gmail_token_path).read_text(encoding="utf-8"))
        resp = await ctx.fetcher.request(
            "POST",
            creds.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "grant_type": "refresh_token",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
            },
            auth_statuses=frozenset({400, 401, 403}),  # invalid_grant comes back as 400
        )
        token = resp.json().get("access_token")
        if not token:
            raise AuthError("Google token endpoint returned no access token")
        return str(token)

    async def collect(self, ctx: CollectContext) -> CollectResult:
        headers = {"Authorization": f"Bearer {await self._access_token(ctx)}"}
        listing = await ctx.fetcher.get(
            GMAIL, params={"q": QUERY, "maxResults": "50"}, headers=headers
        )
        ids = [str(m["id"]) for m in listing.json().get("messages", [])]
        seen: list[str] = json.loads(ctx.state(self.source, STATE_KEY).data or "[]")
        new_ids = [i for i in ids if i not in set(seen)][:MAX_MESSAGES_PER_CYCLE]
        if not new_ids:
            return CollectResult(self.source, RunStatus.UNCHANGED)
        posts: list[PostRecord] = []
        for mid in new_ids:
            msg = await ctx.fetcher.get(
                f"{GMAIL}/{mid}", params={"format": "full"}, headers=headers
            )
            post = parse_message(msg.json(), ctx.config)
            if post is not None:
                posts.append(post)
        # Keep only the most recent ids: the query looks back 14 days anyway.
        seen = (seen + new_ids)[-SEEN_LIMIT:]
        return CollectResult(
            self.source,
            RunStatus.OK,
            posts=tuple(posts),
            state_updates={STATE_KEY: SourceState(data=json.dumps(seen))},
        )
