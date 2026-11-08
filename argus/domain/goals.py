"""Goal and week files (D079): the student's "get good at X" goals, read from the vault.

Each file is ordinary Markdown with exactly one fenced YAML block, ```argus-goal (one goal:
its aim, track, next steps, and test topics) or ```argus-week (one ISO week's plan: the one
change, each goal's focus and target, and the weekend test scores). Argus only reads them.

Parsing never raises: a file gives a model, `None` (no block), or one `DocError` that names the
vault line and the field in plain English. Pure: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

GOAL_ACCENTS: Final = ("indigo", "teal", "plum", "ochre", "rose", "sky", "olive", "slate")

_FENCE = re.compile(r"^```[ \t]*argus-(goal|week)[ \t]*$")
_CLOSE = re.compile(r"^```[ \t]*$")
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")

Tag = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9&-]{0,23}$")]
Id = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]
Title = Annotated[str, Field(min_length=1, max_length=80)]
Line = Annotated[str, Field(min_length=1, max_length=200)]
Short = Annotated[str, Field(min_length=1, max_length=140)]


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, coerce_numbers_to_str=True)


class Milestone(_Doc):
    id: Id
    title: Title
    status: Literal["done", "current", "next"]
    done_when: Line | None = None


class NextStep(_Doc):
    what: Short
    done_when: Line | None = None


class Topic(_Doc):
    id: Id
    title: Title
    after: tuple[Id, ...] = ()


class GoalDoc(_Doc):
    version: Literal[1]
    tag: Tag
    aim: Line
    why: Line | None = None
    status: Literal["active", "paused", "done"] = "active"
    accent: Literal["indigo", "teal", "plum", "ochre", "rose", "sky", "olive", "slate"] | None = (
        None
    )
    since: date | None = None
    track: Annotated[tuple[Milestone, ...], Field(min_length=1, max_length=20)]
    next: Annotated[tuple[NextStep, ...], Field(max_length=3)] = ()
    topics: Annotated[tuple[Topic, ...], Field(max_length=60)] = ()

    @property
    def current(self) -> Milestone | None:
        return next((m for m in self.track if m.status == "current"), None)

    @property
    def next_milestone(self) -> Milestone | None:
        return next((m for m in self.track if m.status == "next"), None)


class Change(_Doc):
    kind: Literal["none", "pace", "method", "length"]
    what: Short
    why: Annotated[str, Field(min_length=1, max_length=300)]


class WeekGoal(_Doc):
    focus: Short | None = None
    target_minutes: Annotated[int, Field(ge=0, le=1500)]
    sessions: Annotated[int, Field(ge=0, le=20)] | None = None


class TestResult(_Doc):
    goal: Tag
    topic: Id
    date: date
    score: Annotated[int, Field(ge=0, le=100)]
    method: Annotated[str, Field(min_length=1, max_length=80)] | None = None


class WeekDoc(_Doc):
    version: Literal[1]
    week: str
    written: date | None = None
    change: Change | None = None
    goals: dict[Tag, WeekGoal] = {}
    tests: Annotated[tuple[TestResult, ...], Field(max_length=100)] = ()

    @field_validator("week")
    @classmethod
    def _iso_week(cls, v: str) -> str:
        week_monday(v)  # raises ValueError for a malformed or impossible week
        return v

    @property
    def monday(self) -> date:
        return week_monday(self.week)


def week_monday(key: str) -> date:
    """The Monday of an ISO week key like "2026-W46". Raises ValueError."""
    m = _WEEK.match(key)
    if not m:
        raise ValueError("must look like 2026-W46")
    year, week = int(m.group(1)), int(m.group(2))
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError:
        raise ValueError(f"{year} has no week {week}") from None


def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True, slots=True)
class DocError:
    """One problem in a goal or week file. `line` is the vault file's line (1-based)."""

    line: int | None
    path: str  # the field, e.g. "track[1].status"; "" for the file as a whole
    message: str


Kind = Literal["goal", "week"]


def extract_block(text: str) -> tuple[Kind, str, int] | DocError | None:
    """The file's one argus block: (kind, YAML text, the YAML's first file line)."""
    lines = text.splitlines()
    opens = [i for i, line in enumerate(lines) if _FENCE.match(line)]
    if not opens:
        return None
    if len(opens) > 1:
        return DocError(opens[1] + 1, "", "A file can hold only one argus-goal or argus-week block")
    start = opens[0]
    end = next((i for i in range(start + 1, len(lines)) if _CLOSE.match(lines[i])), None)
    if end is None:
        return DocError(start + 1, "", "The argus block is never closed with ```")
    m = _FENCE.match(lines[start])
    assert m is not None
    kind: Kind = "goal" if m.group(1) == "goal" else "week"
    return kind, "\n".join(lines[start + 1 : end]) + "\n", start + 2


def parse_doc(rel: str, text: str) -> GoalDoc | WeekDoc | DocError | None:
    """Parse a vault file. `None` when it has no argus block (plain notes are fine)."""
    found = extract_block(text)
    if found is None or isinstance(found, DocError):
        return found
    kind, body, first = found
    try:
        node = yaml.compose(body)
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        problem = getattr(e, "problem", None) or "the YAML can't be read"
        line = first + mark.line if mark is not None else first
        return DocError(line, "", f"YAML syntax: {problem}")
    if not isinstance(data, dict):
        return DocError(first, "", "The block must be a list of fields (name: value)")
    model: type[GoalDoc] | type[WeekDoc] = GoalDoc if kind == "goal" else WeekDoc
    try:
        doc = model.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        loc = tuple(err["loc"])
        return DocError(_line(node, loc, first), _path(loc), _message(err, loc))
    problems = _goal_problems(doc) if isinstance(doc, GoalDoc) else _week_problems(doc, rel)
    if problems:
        loc, message = problems[0]
        return DocError(_line(node, loc, first), _path(loc), message)
    return doc


# --- semantic checks -------------------------------------------------------------------------

Loc = tuple[str | int, ...]


def _goal_problems(doc: GoalDoc) -> list[tuple[Loc, str]]:
    out: list[tuple[Loc, str]] = []
    seen: set[str] = set()
    for i, m in enumerate(doc.track):
        if m.id in seen:
            out.append((("track", i, "id"), f"Another milestone already has the id {m.id}"))
        seen.add(m.id)
    order = {"done": 0, "current": 1, "next": 2}
    for i in range(1, len(doc.track)):
        prev, cur = doc.track[i - 1].status, doc.track[i].status
        if cur == "current" and prev == "current":
            out.append((("track", i, "status"), "Only one milestone can be current"))
        elif order[cur] < order[prev]:
            out.append(
                (("track", i, "status"), "Milestones go in order: done, then current, then next")
            )
    all_done = all(m.status == "done" for m in doc.track)
    if doc.status == "active" and not all_done and doc.current is None:
        out.append((("track",), "An active goal needs one current milestone"))
    ids = [t.id for t in doc.topics]
    for i, t in enumerate(doc.topics):
        if t.id in ids[:i]:
            out.append((("topics", i, "id"), f"Another topic already has the id {t.id}"))
        missing = [a for a in t.after if a not in ids]
        if missing:
            out.append((("topics", i, "after"), f"No topic has the id {', '.join(missing)}"))
    cycle = _cycle(doc.topics)
    if cycle is not None:
        out.append((("topics", cycle, "after"), "These prerequisites go in a cycle"))
    return sorted(out, key=lambda p: _order(p[0]))


def _cycle(topics: Sequence[Topic]) -> int | None:
    """The index of a topic on a prerequisite cycle, if any."""
    after = {t.id: t.after for t in topics}
    index = {t.id: i for i, t in enumerate(topics)}
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(tid: str) -> str | None:
        state[tid] = 1
        for nxt in after.get(tid, ()):
            if nxt not in after:
                continue
            if state.get(nxt) == 1:
                return nxt
            if nxt not in state:
                hit = visit(nxt)
                if hit is not None:
                    return hit
        state[tid] = 2
        return None

    for t in topics:
        if t.id not in state:
            hit = visit(t.id)
            if hit is not None:
                return index[hit]
    return None


def _week_problems(doc: WeekDoc, rel: str) -> list[tuple[Loc, str]]:
    out: list[tuple[Loc, str]] = []
    stem = PurePosixPath(rel).stem
    if stem != doc.week:
        out.append((("week",), f"week is {doc.week}, but the file is named {stem}"))
    monday = doc.monday
    sunday = monday + timedelta(days=6)
    for i, t in enumerate(doc.tests):
        if not monday <= t.date <= sunday:
            out.append(
                (
                    ("tests", i, "date"),
                    f"A test in {doc.week} must be dated {_day(monday)}\N{EN DASH}{_day(sunday)}",
                )
            )
    return out


def _day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _order(loc: Loc) -> tuple[Any, ...]:
    return tuple((0, p) if isinstance(p, int) else (1, p) for p in loc)


def check_goal_set(docs: Mapping[str, GoalDoc]) -> dict[str, DocError]:
    """Problems across goal files: a tag used twice flags every file after the first (by path)."""
    first: dict[str, str] = {}
    out: dict[str, DocError] = {}
    for path in sorted(docs):
        tag = docs[path].tag
        if tag in first:
            out[path] = DocError(None, "tag", f"{first[tag]} already uses the tag {tag}")
        else:
            first[tag] = path
    return out


def week_problems(week: WeekDoc, goals: Mapping[str, GoalDoc]) -> list[DocError]:
    """A week file's references to goals and topics that no goal file has. These are checked when
    the page builds, not at parse, so goal files may arrive after the week file."""
    out: list[DocError] = []
    for tag in week.goals:
        if tag not in goals:
            out.append(DocError(None, f"goals.{tag}", f"No goal file has the tag {tag}"))
    for i, t in enumerate(week.tests):
        goal = goals.get(t.goal)
        if goal is None:
            out.append(DocError(None, f"tests[{i}].goal", f"No goal file has the tag {t.goal}"))
        elif t.topic not in {topic.id for topic in goal.topics}:
            out.append(DocError(None, f"tests[{i}].topic", f"{t.goal} has no topic {t.topic}"))
    return out


# --- error locations and wording ------------------------------------------------------------


def _path(loc: Loc) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _line(node: yaml.Node | None, loc: Loc, first: int) -> int:
    """The vault line of the node at `loc`, or of the deepest node on the way there."""
    if node is None:
        return first
    line = node.start_mark.line
    for part in loc:
        child: yaml.Node | None = None
        if isinstance(node, yaml.MappingNode) and isinstance(part, str):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value == part:
                    child = value
                    break
        elif (
            isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value)
        ):
            child = node.value[part]
        if child is None:
            break
        node = child
        line = node.start_mark.line
    return first + line


_PATTERN_HELP = {
    "tag": "must be one word of letters, digits, & or - (up to 24), like Projects",
    "goal": "must be a goal tag, like Projects",
    "id": "must be lowercase letters, digits and dashes, like ts-react",
    "topic": "must be a topic id: lowercase letters, digits and dashes",
    "after": "must list topic ids: lowercase letters, digits and dashes",
}


# Plain wording per pydantic error type; `{field}` and the error's ctx keys are filled in.
_MESSAGES: Final = {
    "missing": "{field} is missing",
    "extra_forbidden": "{field} isn't a known field",
    "string_type": "must be text (put it in quotes)",
    "string_too_short": "is empty",
    "string_too_long": "is longer than {max_length} characters",
    "too_long": "has more than {max_length} entries",
    "too_short": "needs at least {min_length} entry",
    "int_type": "must be a whole number",
    "int_parsing": "must be a whole number",
    "int_from_float": "must be a whole number",
    "dict_type": "must be a group of fields (name: value)",
    "model_type": "must be a group of fields (name: value)",
    "model_attributes_type": "must be a group of fields (name: value)",
    "tuple_type": "must be a list",
    "list_type": "must be a list",
    "datetime_type": "must be a date like 2026-11-14",
}

_RANGES: Final = {
    "score": "0\N{EN DASH}100",
    "target_minutes": "0\N{EN DASH}1500",
    "sessions": "0\N{EN DASH}20",
}


def _message(err: Any, loc: Loc) -> str:
    kind = str(err["type"])
    ctx = err.get("ctx") or {}
    field = next((str(p) for p in reversed(loc) if isinstance(p, str)), "the block")
    if kind in _MESSAGES:
        return _MESSAGES[kind].format(field=field, **ctx)
    if kind == "literal_error":
        return "must be " + str(ctx.get("expected", "")).replace("'", "")
    if kind == "string_pattern_mismatch":
        return _PATTERN_HELP.get(field, "has characters that aren't allowed")
    if kind in ("greater_than_equal", "less_than_equal"):
        span = _RANGES.get(field)
        return f"is out of range ({span})" if span else "is out of range"
    if kind.startswith("date"):
        return "must be a date like 2026-11-14"
    if kind == "value_error":
        return str(ctx.get("error", err.get("msg", "is not valid")))
    return str(err.get("msg", "is not valid"))
