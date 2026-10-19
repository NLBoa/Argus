"""The eval scorer and datasets are checked in CI, so `make evals` measures the right thing."""

from datetime import datetime

from argus.clock import local_datetime
from argus.domain.findings import Action, Finding, has_date_expression
from tests.evals.datasets import estimates, extraction, integrity
from tests.evals.scoring import PostResult, score


def finding(kind: str, title: str, day: str | None) -> Finding:
    when = local_datetime(datetime.fromisoformat(day).date(), 23, 59) if day else None
    return Finding(kind, "CMSC351", title, when, bool(day), None, "q", 0.9)


def perfect(post: dict) -> PostResult:  # type: ignore[type-arg]
    fs, acts = [], []
    for e in post["expect"]:
        fs.append(finding(e["kind"], e["title"], e["dates"][0] if e["dates"] else None))
        acts.append(Action.CONFLICT if e["kind"] == "date_change" else Action.APPLY_NEW)
    return PostResult(post, fs, acts)


def test_dataset_shape() -> None:
    posts = extraction.POSTS
    assert len(posts) >= 40
    assert len({p["id"] for p in posts}) == len(posts)
    tags = {t for p in posts for t in p["tags"]}
    assert {
        "announcement",
        "date_change",
        "cancellation",
        "scope",
        "noise",
        "ambiguous",
        "relative",
    } <= tags
    for p in posts:
        datetime.fromisoformat(p["posted"].replace("Z", "+00:00"))
        for e in p["expect"]:
            assert e["kind"] in (
                "new_assessment",
                "scope",
                "date_change",
                "cancellation",
                "logistics",
                "other",
            )
            for d in e["dates"]:
                datetime.fromisoformat(d)
            if e["dates"]:  # a labeled date must be findable by the code-side guard
                assert has_date_expression(p["title"] + "\n" + p["body"]), p["id"]
    assert len(estimates.ITEMS) >= 6
    assert integrity.ESTIMATE_ATTEMPTS and integrity.EXTRACT_ATTEMPTS


def test_perfect_outputs_pass_all_gates() -> None:
    report = score([perfect(p) for p in extraction.POSTS])
    assert report.gates() == dict.fromkeys(report.gates(), True), report.summary()
    assert report.recall == 1.0


def test_invented_date_fails_gate() -> None:
    noise = next(p for p in extraction.POSTS if p["id"] == "n01")
    scope = next(p for p in extraction.POSTS if p["id"] == "s01")
    report = score(
        [
            PostResult(
                noise, [finding("new_assessment", "Office hours", "2026-10-19")], [Action.APPLY_NEW]
            ),
            PostResult(scope, [finding("scope", "Midterm 1", "2026-10-28")], [Action.APPLY_INFO]),
        ]
    )
    assert not report.gates()["zero invented dates"]
    assert len(report.invented) == 2


def test_missed_assessments_fail_recall() -> None:
    posts = [p for p in extraction.POSTS if p["expect"]]
    report = score([PostResult(p, [], []) for p in posts])
    assert report.recall == 0.0
    assert not report.gates()["recall >= 95% on true assessments"]


def test_auto_applied_date_change_fails() -> None:
    d01 = next(p for p in extraction.POSTS if p["id"] == "d01")
    applied = PostResult(
        d01, [finding("date_change", "Midterm 1", "2026-10-29")], [Action.APPLY_DATE]
    )
    wrong_day = PostResult(
        d01, [finding("date_change", "Midterm 1", "2026-10-30")], [Action.CONFLICT]
    )
    assert score([applied]).date_change_failures
    r = score([wrong_day])
    assert r.date_change_failures and r.wrong_dates


def test_ambiguous_dates_accept_either_reading() -> None:
    r01 = next(p for p in extraction.POSTS if p["id"] == "r01")
    for day in ("2026-10-23", "2026-10-30"):
        assert (
            score(
                [PostResult(r01, [finding("new_assessment", "Quiz 3", day)], [Action.APPLY_NEW])]
            ).wrong_dates
            == []
        )
