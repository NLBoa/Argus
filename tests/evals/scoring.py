"""Pure scoring for the extraction eval, so the scorer itself is unit-tested in CI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from argus.clock import to_local
from argus.domain.findings import Action, Finding, title_similarity

ASSESSMENT_KINDS = ("new_assessment", "date_change")


@dataclass
class PostResult:
    post: dict[str, Any]
    findings: list[Finding]
    actions: list[Action]


@dataclass
class Report:
    true_assessments: int = 0
    found: int = 0
    invented: list[str] = field(default_factory=list)
    date_change_failures: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    outputs: int = 0
    matched_outputs: int = 0
    wrong_dates: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.found / self.true_assessments if self.true_assessments else 1.0

    @property
    def precision(self) -> float:
        return self.matched_outputs / self.outputs if self.outputs else 1.0

    def gates(self) -> dict[str, bool]:
        return {
            "zero invented dates": not self.invented,
            "recall >= 95% on true assessments": self.recall >= 0.95,
            "every date change handled": not self.date_change_failures,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "recall": round(self.recall, 3),
            "precision": round(self.precision, 3),
            "true_assessments": self.true_assessments,
            "found": self.found,
            "invented": self.invented,
            "date_change_failures": self.date_change_failures,
            "missed": self.missed,
            "wrong_dates": self.wrong_dates,
            "gates": self.gates(),
        }


def _matches(expected: dict[str, Any], f: Finding) -> bool:
    return title_similarity(expected["title"], f.title) >= 0.6


def _local_date(f: Finding) -> str | None:
    return to_local(f.date).date().isoformat() if f.date else None


def score(results: list[PostResult]) -> Report:
    r = Report()
    for res in results:
        post, expected = res.post, res.post["expect"]
        r.outputs += len(res.findings)
        for f in res.findings:
            if any(_matches(e, f) for e in expected):
                r.matched_outputs += 1
            if f.date is None:
                continue
            dated = [e for e in expected if _matches(e, f) and e["dates"]]
            if not dated:
                r.invented.append(f"{post['id']}: {f.title} -> {_local_date(f)}")
            elif _local_date(f) not in {d for e in dated for d in e["dates"]}:
                r.wrong_dates.append(f"{post['id']}: {f.title} -> {_local_date(f)}")
        for e in expected:
            if e["kind"] not in ASSESSMENT_KINDS:
                continue
            r.true_assessments += 1
            hits = [
                (f, a)
                for f, a in zip(res.findings, res.actions, strict=True)
                if f.kind in ASSESSMENT_KINDS and _matches(e, f)
            ]
            if hits:
                r.found += 1
            else:
                r.missed.append(f"{post['id']}: {e['title']}")
            if e["kind"] == "date_change":
                ok = any(_local_date(f) in e["dates"] and a == Action.CONFLICT for f, a in hits)
                if not ok:
                    got = [(f.kind, _local_date(f), a.value) for f, a in hits]
                    r.date_change_failures.append(
                        f"{post['id']}: {e['title']} expected {e['dates']} as a conflict, got {got}"
                    )
    return r
