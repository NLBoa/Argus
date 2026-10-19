from datetime import UTC, datetime, timedelta

import pytest

from argus.ai.schemas import FindingOut
from argus.domain.findings import (
    Action,
    Finding,
    ItemRef,
    PostContext,
    decide,
    has_date_expression,
    link_announced,
    match_item,
    normalize,
    status_for,
)
from argus.domain.models import AuthorRole, ItemKind, Source, SourceItem

POSTED = datetime(2026, 10, 18, 15, tzinfo=UTC)  # Sun Oct 18, 11:00 am EDT
FRI = datetime(2026, 10, 24, 3, 59, tzinfo=UTC)  # Fri Oct 23 23:59 EDT


def post(
    body: str = "Quiz 3 is now on Friday, Oct 23. It covers lectures 8-10.",
    role: AuthorRole = AuthorRole.INSTRUCTOR,
) -> PostContext:
    return PostContext(
        course_code="CMSC351",
        author_role=role,
        pinned=False,
        posted_at=POSTED,
        title="Quiz 3 moved",
        body=body,
    )


def out(**kw: object) -> FindingOut:
    base: dict[str, object] = {
        "kind": "date_change",
        "course": "CMSC351",
        "title": "Quiz 3",
        "date": "2026-10-23",
        "scope": None,
        "quote": "Quiz 3 is now on Friday",
        "confidence": 0.95,
    }
    base.update(kw)
    return FindingOut(**base)  # type: ignore[arg-type]


def finding(**kw: object) -> Finding:
    f = normalize(out(**kw), post())
    assert f is not None
    return f


def item(
    i: int = 1,
    title: str = "Quiz 3",
    kind: ItemKind = ItemKind.QUIZ,
    due: datetime | None = FRI - timedelta(days=2),
    official: bool = True,
) -> ItemRef:
    return ItemRef(
        id=i, course_code="CMSC351", title=title, kind=kind, due_at=due, official=official
    )


# ---------------------------------------------------------------- normalization guards


def test_date_only_finding_is_end_of_local_day() -> None:
    f = finding()
    assert f.date == FRI and f.date_only


def test_datetime_finding_keeps_time() -> None:
    f = normalize(
        out(date="2026-10-23T10:00:00-04:00"), post("Quiz 3 is now on Friday, Oct 23 at 10am.")
    )
    assert f is not None and f.date == datetime(2026, 10, 23, 14, tzinfo=UTC) and not f.date_only


def test_quote_must_appear_in_post() -> None:
    assert normalize(out(quote="Quiz 3 was cancelled"), post()) is None
    # Whitespace, case, and curly quotes are normalized before comparing.
    assert normalize(out(quote="quiz 3  IS now on friday"), post()) is not None


def test_course_must_match_post() -> None:
    assert normalize(out(course="MATH240"), post()) is None


def test_never_invent_a_date() -> None:
    body = "Quiz 3 will cover lectures 8-10."
    f = normalize(
        out(kind="scope", quote="Quiz 3 will cover lectures 8-10", date="2026-10-23"), post(body)
    )
    assert f is not None and f.date is None


def test_date_outside_plausible_window_dropped() -> None:
    f = normalize(out(date="2027-12-01"), post("Quiz 3 is now on Friday, Dec 1."))
    assert f is not None and f.date is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Quiz on Friday", True),
        ("due tomorrow", True),
        ("moved to Nov 5", True),
        ("on 11/5", True),
        ("next week", True),
        ("tonight at 9", True),
        ("in two weeks", True),
        ("on the 23rd", True),
        ("covers chapter 4", False),
        ("bring a calculator", False),
        ("Homework 4 posted", False),
    ],
)
def test_date_expressions(text: str, expected: bool) -> None:
    assert has_date_expression(text) is expected


# ---------------------------------------------------------------- matching


def test_match_by_normalized_title() -> None:
    assert match_item(finding(title="quiz #3"), [item(1, "Quiz 2"), item(2, "Quiz 3")]) == item(
        2, "Quiz 3"
    )


def test_match_by_token_overlap_but_numbers_must_agree() -> None:
    f = finding(title="Midterm Exam 1", kind="new_assessment")
    assert match_item(f, [item(1, "Midterm 1", ItemKind.EXAM)]) is not None
    assert match_item(f, [item(1, "Midterm 2", ItemKind.EXAM)]) is None


def test_no_match_other_course_or_incompatible_kind() -> None:
    other = ItemRef(9, "MATH240", "Quiz 3", ItemKind.QUIZ, FRI, True)
    assert match_item(finding(), [other]) is None
    assert (
        match_item(finding(title="Homework 3"), [item(1, "Homework 3 quiz", ItemKind.EXAM)]) is None
    )


def test_ties_prefer_nearest_due() -> None:
    a = item(1, "Quiz 3", due=FRI - timedelta(days=20))
    b = item(2, "Quiz 3", due=FRI - timedelta(days=1))
    assert match_item(finding(), [a, b]) == b


# ---------------------------------------------------------------- decision table


@pytest.mark.parametrize(
    ("kind", "role", "conf", "target", "expected"),
    [
        # new assessments
        ("new_assessment", AuthorRole.INSTRUCTOR, 0.95, None, Action.APPLY_NEW),
        ("new_assessment", AuthorRole.TA, 0.95, None, Action.APPLY_NEW),
        ("new_assessment", AuthorRole.STUDENT, 0.95, None, Action.HOLD),
        ("new_assessment", AuthorRole.INSTRUCTOR, 0.6, None, Action.HOLD),
        ("new_assessment", AuthorRole.INSTRUCTOR, 0.95, "same", Action.LINK),
        ("new_assessment", AuthorRole.INSTRUCTOR, 0.95, "official", Action.CONFLICT),
        ("new_assessment", AuthorRole.INSTRUCTOR, 0.95, "unofficial", Action.APPLY_DATE),
        # date changes: anything changing an official date is held
        ("date_change", AuthorRole.INSTRUCTOR, 0.99, "official", Action.CONFLICT),
        ("date_change", AuthorRole.INSTRUCTOR, 0.95, "unofficial", Action.APPLY_DATE),
        ("date_change", AuthorRole.INSTRUCTOR, 0.6, "unofficial", Action.HOLD),
        ("date_change", AuthorRole.INSTRUCTOR, 0.95, "same", Action.LINK),
        ("date_change", AuthorRole.INSTRUCTOR, 0.95, None, Action.APPLY_NEW),
        # cancellations always need confirmation
        ("cancellation", AuthorRole.INSTRUCTOR, 0.99, "official", Action.HOLD),
        ("cancellation", AuthorRole.INSTRUCTOR, 0.99, None, Action.STORE),
        # scope / logistics change no dates
        ("scope", AuthorRole.INSTRUCTOR, 0.9, "official", Action.APPLY_INFO),
        ("scope", AuthorRole.STUDENT, 0.9, "official", Action.HOLD),
        ("logistics", AuthorRole.TA, 0.9, "official", Action.APPLY_INFO),
        ("logistics", AuthorRole.TA, 0.9, None, Action.STORE),
        ("other", AuthorRole.INSTRUCTOR, 0.99, "official", Action.STORE),
    ],
)
def test_decision_table(
    kind: str, role: AuthorRole, conf: float, target: str | None, expected: Action
) -> None:
    f = finding(kind=kind, confidence=conf)
    targets = {
        None: None,
        "same": item(due=FRI),
        "official": item(due=FRI - timedelta(days=2), official=True),
        "unofficial": item(due=FRI - timedelta(days=2), official=False),
    }
    p = PostContext("CMSC351", role, False, POSTED, "t", "b")
    assert decide(f, p, targets[target]).action == expected


def test_undated_finding_on_existing_item_links() -> None:
    f = normalize(out(kind="new_assessment", date=None), post())
    assert f is not None
    assert decide(f, post(), item()).action == Action.LINK


def test_date_only_same_day_counts_as_same() -> None:
    f = finding(kind="date_change")
    same_day_earlier = item(due=FRI - timedelta(hours=5))  # Fri 6:59 pm EDT, same local day
    assert decide(f, post(), same_day_earlier).action == Action.LINK


def test_decisions_carry_item_and_reason() -> None:
    d = decide(finding(), post(), item(7))
    assert d.item_id == 7 and "ELMS" not in d.reason and d.reason


def test_status_mapping() -> None:
    assert [status_for(a) for a in Action] == [
        "applied",
        "applied",
        "applied",
        "linked",
        "pending",
        "conflict",
        "stored",
    ]


# ---------------------------------------------------------------- official records take over announced items


def rec(
    ext: str,
    title: str,
    due: datetime | None,
    course: str = "CMSC351",
    kind: ItemKind = ItemKind.QUIZ,
) -> SourceItem:
    return SourceItem(Source.CANVAS_ICAL, ext, course, title, kind, due, "h")


def test_official_record_links_to_announced_item() -> None:
    announced = [item(5, "Quiz 3", official=False, due=FRI)]
    named = rec("700", "Quiz 3: graphs", FRI + timedelta(hours=10))  # overlap 2/3: same quiz
    assert link_announced([named], announced) == {named.key: 5}
    other = rec("701", "Quiz 4: graphs", FRI)  # numbers differ: never the same
    assert link_announced([other], announced) == {}


def test_link_rules() -> None:
    announced = [item(5, "Midterm 1", ItemKind.EXAM, official=False, due=FRI)]
    far = rec("1", "Midterm 1", FRI + timedelta(days=30), kind=ItemKind.EXAM)
    other_course = rec("2", "Midterm 1", FRI, course="MATH240", kind=ItemKind.EXAM)
    undated = rec("3", "Midterm 1", None, kind=ItemKind.EXAM)
    assert link_announced([far, other_course], announced) == {}
    assert link_announced([undated], announced) == {undated.key: 5}


def test_each_item_and_record_used_once() -> None:
    announced = [
        item(5, "Quiz 3", official=False, due=FRI),
        item(6, "Quiz 3", official=False, due=FRI + timedelta(days=3)),
    ]
    a, b = rec("1", "Quiz 3", FRI), rec("2", "Quiz 3", FRI + timedelta(days=3))
    assert link_announced([b, a], announced) == {a.key: 5, b.key: 6}
