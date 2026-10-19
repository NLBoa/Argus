from argus.ai.schemas import ClaimCheck, ConceptDraft, PracticeCheck, VerificationOut
from argus.domain.study import apply_verification, number, slugify, valid_mermaid

SOURCES = {"S1", "S2", "S3"}


def draft(**kw: object) -> ConceptDraft:
    base: dict[str, object] = {
        "name": "The master theorem",
        "idea": [
            {"text": "It solves T(n) = aT(n/b) + f(n).", "sources": ["S1"]},
            {"text": "It has three cases.", "sources": ["S2"]},
        ],
        "diagram": "flowchart LR\n  A[Compare f(n) with n^log_b a] --> B[Case 1, 2, or 3]",
        "worked_example": [{"text": "For T(n) = 2T(n/2) + n, a = 2 and b = 2.", "sources": ["S3"]}],
        "why_it_works": [{"text": "The recursion tree has log_b n levels.", "sources": ["S2"]}],
        "common_mistakes": [
            {"text": "Forgetting the regularity condition in case 3.", "sources": ["S2"]}
        ],
        "practice": [
            {
                "kind": "question",
                "prompt": "Which case applies to T(n) = 2T(n/2) + n?",
                "answer": "Case 2.",
                "sources": ["S3"],
            },
            {
                "kind": "flashcard",
                "prompt": "What does b stand for?",
                "answer": "The shrink factor.",
                "sources": ["S1"],
            },
        ],
        "related": ["Recursion trees"],
    }
    base.update(kw)
    return ConceptDraft.model_validate(base)


def all_ok(d: ConceptDraft) -> VerificationOut:
    n = number(d)
    return VerificationOut(
        claims=[ClaimCheck(id=c[0], supported=True, citation_ok=True, note="") for c in n.claims],
        practice=[PracticeCheck(id=p[0], answer_correct=True, note="") for p in n.practice],
    )


def test_numbering_covers_every_claim_diagram_and_question() -> None:
    n = number(draft())
    assert [c[0] for c in n.claims] == ["C1", "C2", "C3", "C4", "C5", "D1"]
    assert [p[0] for p in n.practice] == ["P1", "P2"]


def test_all_verified() -> None:
    v = apply_verification(draft(), all_ok(draft()), SOURCES)
    assert v is not None
    assert v.slug == "the-master-theorem"
    assert len(v.idea) == 2 and all(c.verified for c in v.idea)
    assert v.diagram and v.diagram.startswith("flowchart")
    assert len(v.practice) == 2 and v.removed_claims == 0
    assert v.all_sources == ("S1", "S2", "S3")


def test_unsupported_removed_miscited_marked_wrong_answers_dropped() -> None:
    d = draft()
    checks = all_ok(d)
    checks.claims[1] = ClaimCheck(
        id="C2", supported=False, citation_ok=False, note="not in sources"
    )
    checks.claims[2] = ClaimCheck(id="C3", supported=True, citation_ok=False, note="it is S2")
    checks.practice[0] = PracticeCheck(id="P1", answer_correct=False, note="should be case 1")
    v = apply_verification(d, checks, SOURCES)
    assert v is not None
    assert [c.text for c in v.idea] == ["It solves T(n) = aT(n/b) + f(n)."]
    assert v.worked_example[0].verified is False
    assert [p.prompt for p in v.practice] == ["What does b stand for?"]
    assert (v.removed_claims, v.dropped_practice) == (1, 1)


def test_missing_verdicts_count_as_unsupported_and_unknown_sources_fail() -> None:
    d = draft()
    checks = all_ok(d)
    checks.claims.pop(0)  # verifier skipped C1
    v = apply_verification(d, checks, {"S2", "S3"})  # S1 does not exist
    assert v is not None
    assert [c.text for c in v.idea] == ["It has three cases."]
    assert [p.prompt for p in v.practice] == ["Which case applies to T(n) = 2T(n/2) + n?"]


def test_concept_without_surviving_idea_is_dropped() -> None:
    d = draft()
    checks = all_ok(d)
    for i in (0, 1):
        checks.claims[i] = ClaimCheck(id=f"C{i + 1}", supported=False, citation_ok=False, note="")
    assert apply_verification(d, checks, SOURCES) is None


def test_bad_diagram_dropped() -> None:
    d = draft(diagram="```mermaid\ngraph TD\nA-->B\n```")
    v = apply_verification(d, all_ok(d), SOURCES)
    assert v is not None and v.diagram is None
    assert valid_mermaid("not a diagram") is None
    assert valid_mermaid(None) is None
    no_diagram = draft(diagram=None)
    assert [c[0] for c in number(no_diagram).claims][-1] == "C5"


def test_slugify() -> None:
    assert slugify("Big-O, Big-Omega & Big-Theta") == "big-o-big-omega-big-theta"
    assert slugify("!!!") == "concept"
