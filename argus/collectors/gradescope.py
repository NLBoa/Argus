"""Gradescope via the unofficial `gradescopeapi` library (pinned; it can break without warning).

Quirks: SSO users may still need a Gradescope password (set one at gradescope.com). The
library POSTs credentials as query parameters to /login, and it is synchronous (`requests`),
so it runs in a worker thread. Student-view timestamps carry an offset ("-0400"). Rows
without a submit link have no assignment id, so a stable id is derived from the name.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from argus.clock import LOCAL_TZ
from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.http import AuthError, FetchError
from argus.domain.models import ItemKind, Source, SourceItem
from argus.domain.titles import infer_kind
from argus.store.repo import SourceState, sha256

BASE_URL = "https://www.gradescope.com"
# What the scraper raises when Gradescope's HTML no longer matches its selectors.
_LAYOUT_ERRORS = (TypeError, KeyError, AttributeError, IndexError)
_LAYOUT_MESSAGE = "Gradescope page format changed; gradescopeapi may need an update"


@dataclass(frozen=True, slots=True)
class GSAssignment:
    assignment_id: str | None
    name: str
    release: datetime | None
    due: datetime | None
    late_due: datetime | None
    status: str | None
    grade: float | None
    max_grade: float | None


class GradescopeClient(Protocol):
    def login(self, email: str, password: str) -> None: ...
    def assignments(self, course_id: str) -> list[GSAssignment]: ...


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class LibGradescopeClient:
    """Adapter over gradescopeapi. Isolates the unofficial API to this class."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        from gradescopeapi.classes.connection import GSConnection

        self._conn = GSConnection(gradescope_base_url=base_url)

    def login(self, email: str, password: str) -> None:
        try:
            self._conn.login(email, password)
        except ValueError as e:  # the library's "Invalid credentials."
            raise AuthError("Gradescope rejected the credentials") from e
        except _LAYOUT_ERRORS as e:
            raise FetchError(_LAYOUT_MESSAGE) from e

    def assignments(self, course_id: str) -> list[GSAssignment]:
        if self._conn.account is None:
            raise AuthError("not logged in to Gradescope")
        try:
            raw = self._conn.account.get_assignments(course_id)
        except _LAYOUT_ERRORS as e:
            raise FetchError(_LAYOUT_MESSAGE) from e
        out = []
        for a in raw:
            out.append(
                GSAssignment(
                    assignment_id=str(a.assignment_id) if a.assignment_id else None,
                    name=str(a.name).strip(),
                    release=a.release_date,
                    due=a.due_date,
                    late_due=a.late_due_date,
                    status=str(a.submissions_status).strip() if a.submissions_status else None,
                    grade=_float(a.grade),
                    max_grade=_float(a.max_grade),
                )
            )
        return out


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(UTC)


def to_source_item(a: GSAssignment, course_code: str, gs_course_id: str) -> SourceItem:
    if a.grade is not None:
        submitted, graded = True, True
    elif a.status == "Submitted":
        submitted, graded = True, False
    elif a.status == "No Submission":
        submitted, graded = False, False
    else:
        submitted, graded = None, None
    ext = a.assignment_id or f"name-{sha256(gs_course_id + ':' + a.name)[:16]}"
    url = (
        f"{BASE_URL}/courses/{gs_course_id}/assignments/{a.assignment_id}"
        if a.assignment_id
        else f"{BASE_URL}/courses/{gs_course_id}"
    )
    due = _aware(a.due)
    late = _aware(a.late_due)
    fingerprint = json.dumps(
        [ext, a.name, str(a.release), str(a.due), str(a.late_due), a.status, a.grade, a.max_grade]
    )
    return SourceItem(
        source=Source.GRADESCOPE,
        external_id=ext,
        course_code=course_code,
        title=a.name or "(untitled)",
        kind=infer_kind(a.name, ItemKind.ASSIGNMENT),
        due_at=due,
        release_at=_aware(a.release),
        late_due_at=late if (late and due and late > due) else None,
        points=a.max_grade,
        submitted=submitted,
        graded=graded,
        url=url,
        raw_hash=sha256(fingerprint),
    )


class GradescopeCollector:
    source = Source.GRADESCOPE

    def __init__(self, client_factory: type[GradescopeClient] | None = None) -> None:
        self._factory = client_factory

    def enabled(self, ctx: CollectContext) -> bool:
        s = ctx.secrets
        return bool(s.gradescope_email and s.gradescope_password) and any(
            c.gradescope_id for c in ctx.config.courses
        )

    def _fetch(self, ctx: CollectContext) -> dict[str, list[GSAssignment]]:
        assert ctx.secrets.gradescope_email
        assert ctx.secrets.gradescope_password
        client: GradescopeClient = (self._factory or LibGradescopeClient)()
        client.login(
            ctx.secrets.gradescope_email, ctx.secrets.gradescope_password.get_secret_value()
        )
        return {
            c.code: client.assignments(c.gradescope_id)
            for c in ctx.config.courses
            if c.gradescope_id
        }

    async def collect(self, ctx: CollectContext) -> CollectResult:
        fetched = await asyncio.to_thread(self._fetch, ctx)
        items: list[SourceItem] = []
        scopes: dict[str, datetime | None] = {}
        updates: dict[str, SourceState] = {}
        for code, assignments in fetched.items():
            course = ctx.config.course(code)
            assert course is not None
            assert course.gradescope_id is not None
            converted = [to_source_item(a, code, course.gradescope_id) for a in assignments]
            key = f"course:{course.gradescope_id}"
            digest = sha256("".join(i.raw_hash for i in converted))
            if digest == ctx.state(self.source, key).payload_hash:
                continue
            items.extend(converted)
            scopes[code] = None
            updates[key] = SourceState(payload_hash=digest)
        status = RunStatus.OK if scopes else RunStatus.UNCHANGED
        return CollectResult(
            self.source, status, items=tuple(items), complete_scopes=scopes, state_updates=updates
        )
