"""Announcement extraction (Haiku): one post in, candidate findings out. Code decides the rest."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from argus.ai import prompts
from argus.ai.client import AIClient, AIResult, Tier
from argus.ai.schemas import ExtractionOut
from argus.clock import to_local
from argus.domain.findings import ItemRef
from argus.domain.models import AuthorRole, PostRecord

MAX_BODY = 6000


def _clock(dt: datetime) -> str:
    local = to_local(dt)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"


_AUTHORS = {
    AuthorRole.INSTRUCTOR: "an instructor",
    AuthorRole.TA: "a teaching assistant",
    AuthorRole.STUDENT: "a student",
    AuthorRole.UNKNOWN: "someone (role unknown)",
}


def render(
    post: PostRecord, courses: Sequence[str], known: Sequence[ItemRef]
) -> prompts.RenderedPrompt:
    local = to_local(post.posted_at)
    return prompts.load("extract").render(
        courses=list(courses),
        course=post.course_code,
        known_items=[
            {
                "title": i.title,
                "kind": i.kind.value,
                "due": f"{to_local(i.due_at):%a %b} {to_local(i.due_at).day} {_clock(i.due_at)}"
                if i.due_at
                else None,
            }
            for i in known
        ],
        author=_AUTHORS[post.author_role],
        posted_local=f"{local:%A, %B} {local.day}, {local.year} at {_clock(post.posted_at)}",
        title=post.title,
        body=post.body[:MAX_BODY],
    )


async def extract(
    ai: AIClient, post: PostRecord, courses: Sequence[str], known: Sequence[ItemRef]
) -> AIResult[ExtractionOut]:
    return await ai.structured(
        task="extract",
        tier=Tier.FAST,
        prompt=render(post, courses, known),
        schema=ExtractionOut,
        max_tokens=4096,
        effort="medium",
    )
