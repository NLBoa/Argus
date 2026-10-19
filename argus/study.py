"""Study content: generate concept notes from course materials, verify them, store them.

The generator (standard tier) writes concept notes with a source id on every claim. The
verifier (strong tier) sees only the sources and the note, as a separate call. Code applies
the verdict (domain/study.py). Nothing is published unverified: if verification is
unavailable, the concept waits. The non-AI fallback is the module note listing its
materials, which the vault writes regardless.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from argus.ai import prompts
from argus.ai.client import AIClient, Tier
from argus.ai.schemas import ConceptBatchOut, ConceptDraft, PracticeBatchOut, VerificationOut
from argus.clock import Clock
from argus.config import AppConfig
from argus.domain.models import MaterialRecord
from argus.domain.study import VerifiedConcept, apply_verification, kept_practice, number, slugify
from argus.logging import get_logger
from argus.store import repo
from argus.store.db import iso, transaction

log = get_logger(__name__)
MAX_SOURCE_CHARS = 60_000


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    ref: str
    text: str
    url: str


def sources_for(materials: list[MaterialRecord], limit: int = MAX_SOURCE_CHARS) -> list[Source]:
    out: list[Source] = []
    used = 0
    for m in materials:
        for page in m.pages:
            if used + len(page.text) > limit:
                return out
            out.append(Source(f"S{len(out) + 1}", m.title, page.ref, page.text, m.url))
            used += len(page.text)
    return out


def input_hash(materials: list[MaterialRecord]) -> str:
    gen, ver = prompts.load("generate").id, prompts.load("verify").id
    return hashlib.sha256(
        json.dumps([gen, ver, *[m.content_hash for m in materials]]).encode()
    ).hexdigest()[:24]


async def check(
    ai: AIClient, draft: ConceptDraft, sources: list[Source], *, use_cache: bool = True
) -> VerificationOut | None:
    """The verifier's raw verdicts (strong tier; it sees only the sources and the note)."""
    numbered = number(draft)
    rendered = prompts.load("verify").render(
        sources=sources,
        name=draft.name,
        claims=[
            {"id": cid, "text": text, "sources": list(srcs)}
            for cid, _, text, srcs in numbered.claims
        ],
        practice=[
            {"id": pid, "prompt": p.prompt, "answer": p.answer, "sources": p.sources}
            for pid, p in numbered.practice
        ],
    )
    result = await ai.structured(
        task="study_verify",
        tier=Tier.STRONG,
        prompt=rendered,
        schema=VerificationOut,
        max_tokens=8000,
        effort="high",
        use_cache=use_cache,
    )
    return result.value


async def verify(
    ai: AIClient, draft: ConceptDraft, sources: list[Source]
) -> VerifiedConcept | None:
    checks = await check(ai, draft, sources)
    if checks is None:
        return None
    return apply_verification(draft, checks, {s.id for s in sources})


def _module_ids(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:
    rows = conn.execute(
        """SELECT DISTINCT c.code, m.module_id, COALESCE(md.name, 'Course materials') AS name
           FROM materials m JOIN courses c ON c.id = m.course_id
           LEFT JOIN modules md ON md.course_id = m.course_id AND md.canvas_id = m.module_id
           WHERE c.active = 1 AND m.module_id IS NOT NULL AND m.kind != 'inbox'
           ORDER BY c.code, m.module_id"""
    ).fetchall()
    return [(r["code"], r["module_id"], r["name"]) for r in rows]


def module_hash(conn: sqlite3.Connection, course_code: str, module_id: int) -> str | None:
    row = conn.execute(
        """SELECT k.input_hash FROM concepts k JOIN courses c ON c.id = k.course_id
           WHERE c.code = ? AND k.module_id = ? AND k.input_hash IS NOT NULL LIMIT 1""",
        (course_code, module_id),
    ).fetchone()
    return str(row[0]) if row else None


async def generate_module(
    conn: sqlite3.Connection,
    ai: AIClient,
    *,
    clock: Clock,
    course_code: str,
    module_id: int,
    module_name: str,
) -> int:
    """Generate and verify concept notes for one module. Returns how many were published."""
    materials = repo.list_materials(conn, course_code, module_id)
    sources = sources_for(materials)
    if not sources:
        return 0
    gen = await ai.structured(
        task="study_generate",
        tier=Tier.STANDARD,
        prompt=prompts.load("generate").render(
            course=course_code, module=module_name, sources=sources
        ),
        schema=ConceptBatchOut,
        max_tokens=16000,
        effort="medium",
    )
    if gen.value is None:
        return 0
    h = input_hash(materials)
    by_id = {s.id: s for s in sources}
    published = 0
    for draft in gen.value.concepts:
        verified = await verify(ai, draft, sources)
        if verified is None:
            log.info("concept not published", extra={"concept": draft.name})
            continue
        store_concept(
            conn,
            clock,
            course_code=course_code,
            module_id=module_id,
            v=verified,
            sources=by_id,
            input_hash_=h,
            model=gen.model,
        )
        published += 1
    return published


def store_concept(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    course_code: str,
    module_id: int | None,
    v: VerifiedConcept,
    sources: dict[str, Source],
    input_hash_: str | None,
    model: str | None,
) -> int:
    now = iso(clock.now())

    def cite(ids: tuple[str, ...]) -> list[str]:
        return [f"{sources[i].title}, {sources[i].ref}" for i in ids if i in sources]

    def claims(cs: tuple[object, ...]) -> list[dict[str, object]]:
        return [{"text": c.text, "cites": cite(c.sources), "verified": c.verified} for c in cs]  # type: ignore[attr-defined]

    body = {
        "idea": claims(v.idea),
        "diagram": v.diagram,
        "worked_example": claims(v.worked_example),
        "why_it_works": claims(v.why_it_works),
        "common_mistakes": claims(v.common_mistakes),
    }
    source_list = [
        {"ref": f"{sources[i].title}, {sources[i].ref}", "url": sources[i].url}
        for i in v.all_sources
        if i in sources
    ]
    with transaction(conn):
        course_id = repo.course_ids(conn)[course_code]
        conn.execute(
            """INSERT INTO concepts (course_id, module_id, slug, name, summary, related, sources, body, input_hash,
                                     model, prompt_version, generated_at, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (course_id, slug) DO UPDATE SET module_id = excluded.module_id, name = excluded.name,
                 summary = excluded.summary, related = excluded.related, sources = excluded.sources,
                 body = excluded.body, input_hash = excluded.input_hash, model = excluded.model,
                 prompt_version = excluded.prompt_version, generated_at = excluded.generated_at,
                 verified_at = excluded.verified_at""",
            (
                course_id,
                module_id,
                v.slug,
                v.name,
                v.idea[0].text,
                json.dumps([slugify(r) for r in v.related]),
                json.dumps(source_list),
                json.dumps(body),
                input_hash_,
                model,
                prompts.load("generate").id,
                now,
                now,
            ),
        )
        concept_id = int(
            conn.execute(
                "SELECT id FROM concepts WHERE course_id = ? AND slug = ?", (course_id, v.slug)
            ).fetchone()[0]
        )
        add_practice(conn, concept_id, v, sources, origin="generated", now=now, replace=True)
    return concept_id


def add_practice(
    conn: sqlite3.Connection,
    concept_id: int,
    v: VerifiedConcept,
    sources: dict[str, Source],
    *,
    origin: str,
    now: str,
    replace: bool,
) -> int:
    """Store verified practice items. With `replace`, generated items no longer produced are
    deactivated (attempt history is kept)."""
    prompts_now = {p.prompt for p in v.practice}
    if replace:
        for r in conn.execute(
            "SELECT id, prompt FROM practice_items WHERE concept_id = ? AND origin = 'generated'",
            (concept_id,),
        ).fetchall():
            if r["prompt"] not in prompts_now:
                conn.execute("UPDATE practice_items SET active = 0 WHERE id = ?", (r["id"],))
    added = 0
    for p in v.practice:
        ref = "; ".join(f"{sources[i].title}, {sources[i].ref}" for i in p.sources if i in sources)
        cur = conn.execute(
            """INSERT INTO practice_items (concept_id, kind, prompt, answer, source_ref, origin, verified, active,
                                          created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)
               ON CONFLICT (concept_id, prompt) DO UPDATE SET answer = excluded.answer,
                 source_ref = excluded.source_ref, kind = excluded.kind, verified = 1, active = 1""",
            (concept_id, p.kind, p.prompt, p.answer, ref, origin, now),
        )
        added += cur.rowcount
    return added


async def ensure_study(
    conn: sqlite3.Connection, ai: AIClient, config: AppConfig, clock: Clock, *, limit: int = 1
) -> int:
    """Generate notes for up to `limit` modules whose materials changed. Returns modules done."""
    if not ai.available:
        return 0
    done = 0
    for code, module_id, name in _module_ids(conn):
        if done >= limit:
            break
        if config.course(code) is None:
            continue
        materials = repo.list_materials(conn, code, module_id)
        if module_hash(conn, code, module_id) == input_hash(materials):
            continue
        await generate_module(
            conn, ai, clock=clock, course_code=code, module_id=module_id, module_name=name
        )
        done += 1
    return done


async def targeted_practice(
    conn: sqlite3.Connection, ai: AIClient, clock: Clock, concept_id: int, confusion: str
) -> int:
    """Up to three verified practice items aimed at a confusion. Returns how many were added."""
    row = conn.execute(
        "SELECT k.*, c.code FROM concepts k JOIN courses c ON c.id = k.course_id WHERE k.id = ?",
        (concept_id,),
    ).fetchone()
    if row is None or not ai.available:
        return 0
    materials = (
        repo.list_materials(conn, row["code"], row["module_id"])
        if row["module_id"]
        else repo.list_materials(conn, row["code"])
    )
    sources = sources_for(materials, limit=30_000)
    if not sources:
        return 0
    result = await ai.structured(
        task="study_practice",
        tier=Tier.STANDARD,
        prompt=prompts.load("practice").render(
            concept=row["name"], confusion=confusion, sources=sources
        ),
        schema=PracticeBatchOut,
        max_tokens=4000,
        effort="medium",
    )
    if result.value is None:
        return 0
    draft = ConceptDraft(
        name=row["name"],
        idea=[{"text": row["summary"] or row["name"], "sources": [sources[0].id]}],
        diagram=None,
        worked_example=[],
        why_it_works=[],
        common_mistakes=[],
        practice=result.value.practice,
        related=[],
    )
    numbered = number(draft)
    rendered = prompts.load("verify").render(
        sources=sources,
        name=draft.name,
        claims=[],
        practice=[
            {"id": pid, "prompt": p.prompt, "answer": p.answer, "sources": p.sources}
            for pid, p in numbered.practice
        ],
    )
    checks = await ai.structured(
        task="study_verify",
        tier=Tier.STRONG,
        prompt=rendered,
        schema=VerificationOut,
        max_tokens=4000,
        effort="high",
    )
    if checks.value is None:
        return 0
    kept, _ = kept_practice(draft, checks.value, {s.id for s in sources})
    if not kept:
        return 0
    stub = VerifiedConcept(row["name"], row["slug"], (), None, (), (), (), tuple(kept), (), 0, 0)
    with transaction(conn):
        return add_practice(
            conn,
            concept_id,
            stub,
            {s.id: s for s in sources},
            origin="inbox",
            now=iso(clock.now()),
            replace=False,
        )
