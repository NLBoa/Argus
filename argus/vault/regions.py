"""Argus regions in Markdown notes, and frontmatter.

Argus writes only between `%% argus:begin <id> %%` and `%% argus:end <id> %%` (Obsidian
comments, invisible in reading view). Everything else in a note belongs to the student.
Parsing is strict: broken, duplicated, or nested markers are reported as problems and the
affected region is treated as user-owned, so a damaged note is never overwritten by guesswork.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

_MARKER = re.compile(r"^%% argus:(begin|end) ([a-z0-9-]+) %%[ \t]*$", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\n(.*?\n)?---\n", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Region:
    id: str
    content: str
    start: int  # index of the first content character (after the begin line)
    end: int  # index of the end marker line


@dataclass
class ParsedNote:
    frontmatter: dict[str, Any] | None
    regions: dict[str, Region] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _normalize(content: str) -> str:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


_TOOL_ANNOTATION = re.compile(r"^<!--ID: \d+-->$", re.MULTILINE)


def region_hash(content: str) -> str:
    """Stable across line endings, trailing spaces, and blank lines at the edges. Anki IDs
    that the Obsidian_to_Anki plugin writes into flashcard blocks (`<!--ID: 123-->`) are
    tool annotations, not student edits, so they are ignored."""
    cleaned = "\n".join(
        line
        for line in content.replace("\r\n", "\n").split("\n")
        if not _TOOL_ANNOTATION.match(line)
    )
    return hashlib.sha256(_normalize(cleaned).encode()).hexdigest()


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str]:
    """(parsed frontmatter, raw frontmatter block, body). Unparseable frontmatter is body."""
    m = _FRONTMATTER.match(text)
    if not m:
        return None, "", text
    try:
        data = yaml.safe_load(m.group(1) or "") or {}
    except yaml.YAMLError:
        return None, "", text
    if not isinstance(data, dict):
        return None, "", text
    return data, m.group(0), text[m.end() :]


def parse(text: str) -> ParsedNote:
    fm, _, _ = split_frontmatter(text)
    note = ParsedNote(frontmatter=fm)
    open_id: str | None = None
    open_at = 0
    seen: set[str] = set()
    for m in _MARKER.finditer(text):
        kind, rid = m.group(1), m.group(2)
        if kind == "begin":
            if open_id is not None:
                note.problems.append(f"region '{rid}' starts inside region '{open_id}'")
                open_id = None
                continue
            open_id, open_at = rid, m.end() + 1
        else:
            if open_id is None or rid != open_id:
                note.problems.append(f"end marker for '{rid}' without a begin")
                open_id = None
                continue
            if rid in seen:
                note.problems.append(f"region '{rid}' appears more than once")
                note.regions.pop(rid, None)
            else:
                body = text[open_at : m.start()]
                note.regions[rid] = Region(rid, body.rstrip("\n"), open_at, m.start())
                seen.add(rid)
            open_id = None
    if open_id is not None:
        note.problems.append(f"region '{open_id}' has no end marker")
    return note


def render_region(region_id: str, content: str) -> str:
    inner = _normalize(content)
    return f"%% argus:begin {region_id} %%\n{inner + chr(10) if inner else ''}%% argus:end {region_id} %%"


def replace_region(text: str, region_id: str, content: str) -> str:
    note = parse(text)
    if region_id not in note.regions:
        raise KeyError(region_id)
    r = note.regions[region_id]
    inner = _normalize(content)
    return text[: r.start] + (inner + "\n" if inner else "") + text[r.end :]


def _dump(data: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        dict(data), sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000
    )


def compose(
    frontmatter: Mapping[str, Any] | None, regions: Iterable[tuple[str, str]], tail: str = ""
) -> str:
    """A brand-new note: frontmatter, Argus regions, then the student's sections."""
    head = f"---\n{_dump(frontmatter)}---\n" if frontmatter else ""
    body = "\n\n".join(render_region(rid, content) for rid, content in regions)
    return head + body + "\n" + tail


class FrontmatterEditError(ValueError):
    """The owned keys can't be changed without touching the student's bytes."""


_TOP_KEY = re.compile(r"""^(?:"([^"]+)"|'([^']+)'|([^\s#'"\-][^:]*?)):(?:\s|$)""")


def _key_of(line: str) -> str | None:
    m = _TOP_KEY.match(line)
    return next((g for g in m.groups() if g is not None), None) if m else None


def update_frontmatter(text: str, updates: Mapping[str, Any], *, strict: bool = True) -> str:
    """Set Argus-owned keys by replacing only their own lines (review R3): the student's keys,
    comments, order, and formatting stay byte for byte. If that can't be done safely (and the
    result re-checked), raise `FrontmatterEditError` (or, with `strict=False`, re-serialize:
    used only inside proposals, which the student reviews)."""
    fm, raw, body = split_frontmatter(text)
    if fm is None:
        if strict and _FRONTMATTER.match(text):
            raise FrontmatterEditError("the frontmatter is not plain YAML")
        return f"---\n{_dump(dict(updates))}---\n" + text
    lines = raw.split("\n")
    inner = lines[1:-2]  # between the --- lines (raw ends with "---\n")
    for key, value in updates.items():
        new = _dump({key: value}).rstrip("\n").split("\n")
        start = next((i for i, line in enumerate(inner) if _key_of(line) == key), None)
        if start is None:
            inner += new
            continue
        end = start + 1
        while (
            end < len(inner)
            and inner[end]
            and (inner[end][0] in " \t" or inner[end].startswith("- "))
        ):
            end += 1
        inner[start:end] = new
    out = "---\n" + "\n".join(inner) + ("\n" if inner else "") + "---\n" + body
    expected = dict(fm)
    expected.update(updates)
    if split_frontmatter(out)[0] != expected:
        if strict:
            raise FrontmatterEditError("could not edit the owned keys in place")
        return f"---\n{_dump(expected)}---\n" + body
    return out


def allows_edit(frontmatter: Mapping[str, Any] | None) -> bool:
    return (
        bool(frontmatter)
        and str((frontmatter or {}).get("argus-edit", "")).strip().lower() == "allow"
    )
