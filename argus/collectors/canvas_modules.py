"""Canvas tier 2, nightly: modules (with items), the syllabus, and the current grade.

Needs the same personal access token as `canvas_api`. Files are linked, never downloaded.
Module items come inline (`include[]=items`) unless the module is large, in which case Canvas
omits them and gives `items_url`, which is followed with Link pagination.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.canvas_api import _dt
from argus.collectors.extract_text import MAX_BYTES, extract, kind_for, page_text
from argus.collectors.text import html_to_text
from argus.domain.models import (
    AuthorRole,
    GradeRecord,
    MaterialRecord,
    ModuleItemRecord,
    ModuleRecord,
    PostRecord,
    Source,
)
from argus.logging import get_logger
from argus.store.repo import SourceState, sha256

log = get_logger(__name__)


def parse_item(i: dict[str, Any]) -> ModuleItemRecord:
    url = i.get("external_url") if i.get("type") == "ExternalUrl" else i.get("html_url")
    return ModuleItemRecord(
        title=str(i.get("title") or "(untitled)"),
        type=str(i.get("type") or "Item"),
        url=url,
        indent=int(i.get("indent") or 0),
        api_url=i.get("url") if i.get("type") in ("File", "Page") else None,
    )


def parse_module(m: dict[str, Any], course_code: str, items: list[dict[str, Any]]) -> ModuleRecord:
    return ModuleRecord(
        course_code=course_code,
        canvas_id=int(m["id"]),
        name=str(m.get("name") or "(untitled module)"),
        position=int(m.get("position") or 0),
        items=tuple(parse_item(i) for i in items),
        unlock_at=_dt(m.get("unlock_at")),
        state=m.get("state"),
    )


def parse_grade(enrollments: Any, course_code: str) -> GradeRecord | None:
    if not isinstance(enrollments, list) or not enrollments:
        return None
    grades = enrollments[0].get("grades") or {}
    score = grades.get("current_score")
    return GradeRecord(
        course_code=course_code,
        score=float(score) if score is not None else None,
        letter=grades.get("current_grade"),
    )


MAX_FILES_PER_COURSE = 60


class CanvasModulesCollector:
    source = Source.CANVAS_MODULES

    def enabled(self, ctx: CollectContext) -> bool:
        return ctx.secrets.canvas_token is not None and any(c.canvas_id for c in ctx.config.courses)

    async def collect(self, ctx: CollectContext) -> CollectResult:
        assert ctx.secrets.canvas_token is not None
        headers = {"Authorization": f"Bearer {ctx.secrets.canvas_token.get_secret_value()}"}
        base = ctx.config.canvas.base_url.rstrip("/")
        modules: dict[str, tuple[ModuleRecord, ...]] = {}
        grades: list[GradeRecord] = []
        posts: list[PostRecord] = []
        materials: list[MaterialRecord] = []
        updates: dict[str, SourceState] = {}
        for course in ctx.config.courses:
            if course.canvas_id is None:
                continue
            api = f"{base}/api/v1/courses/{course.canvas_id}"
            pages = await ctx.fetcher.get_all_pages(
                f"{api}/modules", params={"include[]": "items", "per_page": "50"}, headers=headers
            )
            raw: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
            for page in pages:
                data = page.json()
                if not isinstance(data, list):
                    raise ValueError(f"expected a list of modules for {course.code}")
                for m in data:
                    items = m.get("items")
                    if items is None and m.get("items_url"):
                        item_pages = await ctx.fetcher.get_all_pages(
                            str(m["items_url"]), params={"per_page": "100"}, headers=headers
                        )
                        items = [i for p in item_pages for i in p.json()]
                    raw.append((m, list(items or [])))
            syllabus = (
                await ctx.fetcher.get(api, params={"include[]": "syllabus_body"}, headers=headers)
            ).json()
            enrollment = await ctx.fetcher.get(
                f"{api}/enrollments",
                params={"user_id": "self", "type[]": "StudentEnrollment"},
                headers=headers,
            )
            key = f"course:{course.canvas_id}"
            digest = sha256(
                json.dumps([raw, syllabus.get("syllabus_body"), enrollment.json()], sort_keys=True)
            )
            if digest == ctx.state(self.source, key).payload_hash:
                continue
            modules[course.code] = tuple(parse_module(m, course.code, items) for m, items in raw)
            materials.extend(
                await self._materials(
                    ctx,
                    base=base,
                    headers=headers,
                    course_code=course.code,
                    raw=raw,
                    updates=updates,
                )
            )
            grade = parse_grade(enrollment.json(), course.code)
            if grade is not None:
                grades.append(grade)
            body = html_to_text(syllabus.get("syllabus_body"))
            if body:
                posts.append(
                    PostRecord(
                        platform="canvas",
                        external_id=f"syllabus:{course.canvas_id}",
                        via=Source.CANVAS_MODULES,
                        course_code=course.code,
                        title=f"{course.code} syllabus",
                        body=body,
                        posted_at=ctx.clock.now(),
                        author_role=AuthorRole.INSTRUCTOR,
                        kind="syllabus",
                        url=f"{base}/courses/{course.canvas_id}/assignments/syllabus",
                        raw_hash=sha256(body),
                    )
                )
            updates[key] = SourceState(payload_hash=digest)
        status = RunStatus.OK if updates else RunStatus.UNCHANGED
        return CollectResult(
            self.source,
            status,
            modules=modules,
            grades=tuple(grades),
            posts=tuple(posts),
            materials=tuple(materials),
            state_updates=updates,
        )

    async def _materials(
        self,
        ctx: CollectContext,
        *,
        base: str,
        headers: dict[str, str],
        course_code: str,
        raw: list[tuple[dict[str, Any], list[dict[str, Any]]]],
        updates: dict[str, SourceState],
    ) -> list[MaterialRecord]:
        """Text of the module's files (PDF, PPTX) and pages, for study notes. Files are read in
        memory and discarded; only extracted text is kept. Unchanged files are skipped."""
        key = f"files:{course_code}"
        seen: dict[str, str] = json.loads(ctx.state(self.source, key).data or "{}")
        out: list[MaterialRecord] = []
        host = urlsplit(base).netloc
        fetched = 0
        for module, items in raw:
            for i in items:
                api_url = i.get("url")
                if (
                    i.get("type") not in ("File", "Page")
                    or not api_url
                    or fetched >= MAX_FILES_PER_COURSE
                ):
                    continue
                if urlsplit(str(api_url)).netloc != host:
                    continue  # never send the token anywhere but Canvas
                meta = (await ctx.fetcher.get(str(api_url), headers=headers)).json()
                stamp = str(meta.get("updated_at") or meta.get("modified_at") or "")
                if seen.get(str(api_url)) == stamp and stamp:
                    continue
                fetched += 1
                link = str(i.get("html_url") or api_url)
                try:
                    if i["type"] == "Page":
                        pages = page_text(meta.get("body"))
                        kind = "page"
                    else:
                        kind_ = kind_for(
                            str(meta.get("content-type", "")), str(meta.get("filename", ""))
                        )
                        if (
                            kind_ is None
                            or int(meta.get("size") or 0) > MAX_BYTES
                            or not meta.get("url")
                        ):
                            continue
                        download = str(meta["url"])
                        same_host = urlsplit(download).netloc == host
                        data = (
                            await ctx.fetcher.get(download, headers=headers if same_host else None)
                        ).body
                        pages = extract(kind_, data)
                        kind = kind_
                except (ValueError, KeyError) as e:
                    log.warning(
                        "could not extract material",
                        extra={"title": i.get("title"), "error": str(e)},
                    )
                    continue
                seen[str(api_url)] = stamp
                if pages:
                    out.append(
                        MaterialRecord(
                            course_code=course_code,
                            module_id=int(module["id"]),
                            title=str(i.get("title") or meta.get("display_name") or "Untitled"),
                            kind=kind,
                            url=link,
                            pages=pages,
                            content_hash=sha256(json.dumps([p.text for p in pages])),
                        )
                    )
        updates[key] = SourceState(data=json.dumps(seen, sort_keys=True))
        return out
