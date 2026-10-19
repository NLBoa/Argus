"""Versioned prompt files: `<name>.v<N>.md` with front matter and `# system` / `# user` sections.

Bump the version (a new file) whenever wording changes: the version is part of the AI
cache key and of every recorded estimate, so results stay traceable to their prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

PROMPTS_DIR = Path(__file__).parent
_FILE = re.compile(r"^(?P<name>[a-z_]+)\.v(?P<version>\d+)\.md$")
_ENV = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)  # noqa: S701 (plain text, not HTML)


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: int
    system: str
    user: str

    @property
    def id(self) -> str:
        return f"{self.name}.v{self.version}"

    def render(self, **values: Any) -> RenderedPrompt:
        return RenderedPrompt(
            id=self.id,
            system=_ENV.from_string(self.system).render(**values).strip(),
            user=_ENV.from_string(self.user).render(**values).strip(),
        )


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    id: str
    system: str
    user: str


def _parse(path: Path) -> Prompt:
    m = _FILE.match(path.name)
    if not m:
        raise ValueError(f"bad prompt file name: {path.name}")
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"^# (system|user)\s*$", text, flags=re.MULTILINE)
    sections = dict(zip(parts[1::2], parts[2::2], strict=True))
    if set(sections) != {"system", "user"}:
        raise ValueError(f"{path.name}: needs exactly '# system' and '# user' sections")
    return Prompt(
        m["name"], int(m["version"]), sections["system"].strip(), sections["user"].strip()
    )


@cache
def load(name: str) -> Prompt:
    """The latest version of a prompt."""
    versions = [p for p in PROMPTS_DIR.glob(f"{name}.v*.md") if _FILE.match(p.name)]
    if not versions:
        raise FileNotFoundError(f"no prompt named {name}")
    latest = max(versions, key=lambda p: int(_FILE.match(p.name)["version"]))  # type: ignore[index]
    return _parse(latest)


def all_prompts() -> list[Prompt]:
    return [_parse(p) for p in sorted(PROMPTS_DIR.glob("*.md"))]
