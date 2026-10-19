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


# ---------------------------------------------------------------- verifier eval

from argus.ai.schemas import ClaimCheck, ConceptDraft, PracticeCheck, VerificationOut  # noqa: E402
from tests.evals.datasets.verification import CASES  # noqa: E402
from tests.evals.scoring import ERROR_KINDS, element_ids, score_verification  # noqa: E402


def verdicts(case: dict, *, perfect: bool, rubber_stamp: bool = False) -> VerificationOut:  # type: ignore[type-arg]
    claims, practice = element_ids(case["note"])
    errors = {} if rubber_stamp else case["errors"]
    return VerificationOut(
        claims=[
            ClaimCheck(
                id=c,
                supported=errors.get(c) not in ("unsupported",) if perfect else True,
                citation_ok=errors.get(c) != "miscited",
                note="",
            )
            for c in claims
        ],
        practice=[PracticeCheck(id=p, answer_correct=p not in errors, note="") for p in practice],
    )


def test_verifier_dataset_shape() -> None:
    assert len(CASES) >= 20
    assert len({c["id"] for c in CASES}) == len(CASES)
    kinds = set()
    for c in CASES:
        ConceptDraft.model_validate(c["note"])  # the generator's exact output shape
        claims, practice = element_ids(c["note"])
        assert c["errors"], f"{c['id']} has no seeded error"
        for eid, kind in c["errors"].items():
            assert kind in ERROR_KINDS
            assert eid in (practice if kind == "wrong_answer" else claims), f"{c['id']}: {eid}"
            kinds.add(kind)
        ids = {s["id"] for s in c["sources"]}
        cited = {
            s
            for part in ("idea", "worked_example", "why_it_works", "common_mistakes")
            for x in c["note"][part]
            for s in x["sources"]
        }
        cited |= {s for p in c["note"]["practice"] for s in p["sources"]}
        assert cited <= ids, f"{c['id']} cites a missing source"
    assert kinds == set(ERROR_KINDS)
    assert sum(1 for c in CASES if "D1" in c["errors"]) >= 2  # diagrams are checked too


def test_verifier_scorer() -> None:
    perfect = score_verification([(c, verdicts(c, perfect=True)) for c in CASES])
    assert perfect.catch_rate == 1.0 and perfect.false_reject_rate == 0.0
    assert all(perfect.gates().values())
    assert perfect.seeded >= 30 and perfect.clean >= 60
    stamp = score_verification([(c, verdicts(c, perfect=False, rubber_stamp=True)) for c in CASES])
    assert stamp.catch_rate == 0.0 and not stamp.gates()["catches >= 90% of seeded errors"]
    failed = score_verification([(c, None) for c in CASES])  # a dead verifier rejects everything
    assert failed.catch_rate == 1.0 and failed.false_reject_rate == 1.0
    assert not failed.gates()["false rejects <= 10%"]
    one = CASES[0]
    miscite_only = verdicts(one, perfect=True)
    flagged = score_verification([(one, miscite_only)])
    assert flagged.caught == flagged.seeded  # miscited counts as caught when citation_ok is false
    clean_flag = VerificationOut(
        claims=[
            ClaimCheck(id=c, supported=True, citation_ok=False, note="")
            for c in element_ids(one["note"])[0]
        ],
        practice=[],
    )
    soft = score_verification([(one, clean_flag)])
    assert soft.citation_flags_on_clean  # flagged, not rejected
    assert all(":P" in x for x in soft.false_rejects)  # only the practice it gave no verdict on
    assert soft.summary()["gates"] == soft.gates()
