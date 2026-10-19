"""Ownership decisions (pure). Exactly the table in CLAUDE.md:

| Situation                              | Behavior                                              |
|----------------------------------------|-------------------------------------------------------|
| New Argus-created note                 | Write freely                                          |
| Argus region, hash unchanged           | Rewrite                                               |
| Argus region the user edited           | Leave it alone; create a proposal with the diff       |
| User text anywhere else                | Leave it alone; propose only                          |
| Note/folder with `argus-edit: allow`   | Edit freely; log the change in the weekly note        |
| Approved proposal                      | Apply that exact diff once (proposals.py)             |

"Hash unchanged" means the region still hashes to what Argus last wrote there (stored in
`vault_regions`). Argus-owned frontmatter keys are tracked the same way, as region
"frontmatter". All of a note's changes that need approval are batched into one proposal, so
an approved proposal always applies exactly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from argus.vault.proposals import file_hash
from argus.vault.regions import (
    ParsedNote,
    compose,
    parse,
    region_hash,
    render_region,
    replace_region,
    update_frontmatter,
)

FRONTMATTER = "frontmatter"


class Action(StrEnum):
    CREATE = "create"
    REWRITE = "rewrite"
    UNCHANGED = "unchanged"
    PROPOSE = "propose"
    ALLOW_EDIT = "allow_edit"


@dataclass(frozen=True, slots=True)
class DesiredNote:
    frontmatter: Mapping[str, Any]  # Argus-owned keys only
    regions: tuple[tuple[str, str], ...]  # (region id, content), in order
    tail: str = ""  # the student's sections, written only when the note is created


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    region_ids: tuple[str, ...]
    before: str
    after: str
    base_hash: str
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    action: Action
    text: str | None  # the new note to write now (None: write nothing)
    written: Mapping[str, str]  # region id -> hash Argus wrote (or confirmed) there
    proposals: tuple[ProposalDraft, ...] = ()
    log: tuple[str, ...] = ()  # changes made under `argus-edit: allow`, for the weekly note


def frontmatter_hash(frontmatter: Mapping[str, Any] | None, owned: Mapping[str, Any]) -> str:
    values = {k: (frontmatter or {}).get(k) for k in sorted(owned)}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _append(text: str, region_id: str, content: str) -> str:
    return text.rstrip("\n") + "\n\n" + render_region(region_id, content) + "\n"


def plan_note(
    current: str | None, desired: DesiredNote, stored: Mapping[str, str], *, allow: bool
) -> Plan:
    want_fm = frontmatter_hash(desired.frontmatter, desired.frontmatter)
    if current is None:
        text = compose(dict(desired.frontmatter) or None, desired.regions, desired.tail)
        written = {rid: region_hash(c) for rid, c in desired.regions} | {FRONTMATTER: want_fm}
        return Plan(Action.CREATE, text, written)

    if allow:
        return _allow_edit(current, desired, want_fm)

    note = parse(current)
    text, written, pending, changed = _safe_rewrites(current, desired, stored, want_fm)
    proposals = _proposal(text, desired, pending, note) if pending else ()
    if proposals:
        return Plan(Action.PROPOSE, text if changed else None, written, proposals)
    return Plan(Action.REWRITE if changed else Action.UNCHANGED, text if changed else None, written)


def _safe_rewrites(
    current: str, desired: DesiredNote, stored: Mapping[str, str], want_fm: str
) -> tuple[str, dict[str, str], list[tuple[str, str | None]], bool]:
    """Apply every change the table allows without asking; collect the rest as pending."""
    text = current
    written: dict[str, str] = {}
    pending: list[tuple[str, str | None]] = []  # (region id, content; None means frontmatter)
    changed = False
    have_fm = frontmatter_hash(parse(current).frontmatter, desired.frontmatter)
    if have_fm == want_fm:
        written[FRONTMATTER] = want_fm
    elif stored.get(FRONTMATTER) == have_fm:
        text = update_frontmatter(text, desired.frontmatter)
        written[FRONTMATTER] = want_fm
        changed = True
    else:
        pending.append((FRONTMATTER, None))
    for rid, content in desired.regions:
        region = parse(text).regions.get(rid)
        if region is None:
            pending.append((rid, content))  # missing or damaged: never re-inserted without approval
            continue
        have, want = region_hash(region.content), region_hash(content)
        if have == want:
            written[rid] = want
        elif stored.get(rid) == have:
            text = replace_region(text, rid, content)
            written[rid] = want
            changed = True
        else:
            pending.append((rid, content))
    return text, written, pending, changed


def _proposal(
    base: str, desired: DesiredNote, pending: list[tuple[str, str | None]], original: ParsedNote
) -> tuple[ProposalDraft, ...]:
    after = base
    for rid, content in pending:
        if content is None:
            after = update_frontmatter(after, desired.frontmatter)
        elif rid in parse(after).regions:
            after = replace_region(after, rid, content)
        else:
            after = _append(after, rid, content)
    edited = any(rid in original.regions or rid == FRONTMATTER for rid, _ in pending)
    reason = "You edited part of this note" if edited else "Argus wants to add to this note"
    return (ProposalDraft(tuple(rid for rid, _ in pending), base, after, file_hash(base), reason),)


def _allow_edit(current: str, desired: DesiredNote, want_fm: str) -> Plan:
    text = current
    log: list[str] = []
    note = parse(text)
    if frontmatter_hash(note.frontmatter, desired.frontmatter) != want_fm:
        text = update_frontmatter(text, desired.frontmatter)
        log.append("updated frontmatter")
    for rid, content in desired.regions:
        region = parse(text).regions.get(rid)
        if region is None:
            text = _append(text, rid, content)
            log.append(f"added {rid}")
        elif region_hash(region.content) != region_hash(content):
            text = replace_region(text, rid, content)
            log.append(f"rewrote {rid}")
    written = {rid: region_hash(c) for rid, c in desired.regions} | {FRONTMATTER: want_fm}
    if not log:
        return Plan(Action.UNCHANGED, None, written)
    return Plan(Action.ALLOW_EDIT, text, written, log=tuple(log))
