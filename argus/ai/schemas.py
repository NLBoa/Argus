"""Pydantic schemas for every AI output, and their conversion to strict JSON Schema.

Validation constraints (ranges, lengths) live here and are enforced by Pydantic after
the response arrives. The JSON Schema sent to the API is the structural part only:
keywords structured outputs may not support are stripped, every object is closed
(`additionalProperties: false`), and every property is required (nullable where optional).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DROP = {
    "title",
    "default",
    "examples",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
}


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- extraction

FindingKind = Literal[
    "new_assessment", "scope", "date_change", "cancellation", "logistics", "other"
]


class FindingOut(_Out):
    kind: FindingKind
    course: str = Field(description="Course code exactly as listed, e.g. CMSC351")
    title: str = Field(min_length=1, max_length=120, description="Assessment name, e.g. 'Quiz 3'")
    date: str | None = Field(
        description="ISO 8601 local date (YYYY-MM-DD) or datetime with offset, resolved against "
        "the post's timestamp; null if the post gives no date"
    )
    scope: str | None = Field(max_length=400, description="What it covers, if stated")
    quote: str = Field(min_length=1, max_length=300, description="Short exact quote from the post")
    confidence: float = Field(ge=0, le=1)

    @field_validator("date")
    @classmethod
    def _iso(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        try:
            if len(v) == 10:
                date.fromisoformat(v)
            else:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise ValueError("datetime needs an offset")
        except ValueError as e:
            raise ValueError(f"not ISO 8601: {v!r}") from e
        return v


class ExtractionOut(_Out):
    findings: list[FindingOut] = Field(max_length=10)


# ---------------------------------------------------------------- estimates


class Step(_Out):
    step: str = Field(min_length=1, max_length=120)
    hours: float = Field(ge=0, le=100)


class EstimateOut(_Out):
    p50_hours: float = Field(gt=0, le=200)
    p80_hours: float = Field(gt=0, le=300)
    confidence: Literal["low", "medium", "high"]
    breakdown: list[Step] = Field(min_length=1, max_length=12)
    assumptions: list[str] = Field(min_length=1, max_length=8)
    reasoning: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def _ordered(self) -> EstimateOut:
        if self.p80_hours < self.p50_hours:
            raise ValueError("p80_hours must be >= p50_hours")
        return self


# ---------------------------------------------------------------- plan note


class PlanNoteOut(_Out):
    line: str = Field(min_length=1, max_length=160)


# ---------------------------------------------------------------- schema conversion


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            out = {k: walk(v) for k, v in node.items() if k not in _DROP}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(raw)
    assert isinstance(result, dict)
    return result


# ---------------------------------------------------------------- study notes


class Claim(_Out):
    text: str = Field(min_length=1, max_length=700)
    sources: list[str] = Field(min_length=1, max_length=4, description="Source ids like S3")


class PracticeDraft(_Out):
    kind: Literal["question", "flashcard"]
    prompt: str = Field(min_length=1, max_length=600)
    answer: str = Field(min_length=1, max_length=900)
    sources: list[str] = Field(min_length=1, max_length=3)


class ConceptDraft(_Out):
    name: str = Field(min_length=1, max_length=80)
    idea: list[Claim] = Field(min_length=1, max_length=3)
    diagram: str | None = Field(max_length=1500, description="Mermaid diagram source, or null")
    worked_example: list[Claim] = Field(max_length=6)
    why_it_works: list[Claim] = Field(max_length=4)
    common_mistakes: list[Claim] = Field(max_length=5)
    practice: list[PracticeDraft] = Field(min_length=1, max_length=6)
    related: list[str] = Field(max_length=6, description="Names of other concepts in this module")


class ConceptBatchOut(_Out):
    concepts: list[ConceptDraft] = Field(max_length=8)


class PracticeBatchOut(_Out):
    practice: list[PracticeDraft] = Field(min_length=1, max_length=4)


class ClaimCheck(_Out):
    id: str
    supported: bool
    citation_ok: bool
    note: str = Field(max_length=300)


class PracticeCheck(_Out):
    id: str
    answer_correct: bool
    note: str = Field(max_length=300)


class VerificationOut(_Out):
    claims: list[ClaimCheck]
    practice: list[PracticeCheck]


# ---------------------------------------------------------------- SMS replies


class CommandOut(_Out):
    """What a free-text reply most likely means, as one command. Code validates and acts."""

    command: Literal[
        "DONE", "LOG", "SNOOZE", "EST", "LEFT", "SKIP", "FIRST", "OK", "NO", "TODAY", "UNCLEAR"
    ]
    number: int | None = Field(description="The [n] item number the student means, or null")
    hours: float | None = Field(description="Hours for LOG, EST, or LEFT, or null")
    minutes: int | None = Field(description="Snooze length in minutes, or null")
    confidence: float = Field(ge=0, le=1)
