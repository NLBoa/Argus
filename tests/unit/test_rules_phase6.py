"""Morning brief, exam countdowns, announced changes, and the snooze guard (pure)."""

from datetime import UTC, date, datetime, time, timedelta

from argus.config import TimeWindow
from argus.domain.models import Item, ItemKind, ItemStatus
from argus.reminders.guards import GuardContext, Verdict, check
from argus.reminders.rules import (
    Announced,
    Brief,
    Countdown,
    Reminder,
    Rule,
    Snapshot,
    announced,
    candidates,
    exam_countdowns,
    morning_brief,
)
from argus.reminders.text import SEGMENT, gsm_length

NOW = datetime(2026, 10, 19, 13, tzinfo=UTC)  # Mon 09:00 EDT
EOD = datetime(2026, 10, 20, 4, tzinfo=UTC)
EXAM = Item(1, "CMSC351", ItemKind.EXAM, "Midterm 1", NOW + timedelta(days=3), ItemStatus.UPCOMING)


def snap(**kw: object) -> Snapshot:
    sent = kw.pop("sent", set())
    return Snapshot(now=NOW, items=[EXAM], sent_keys=frozenset(sent), **kw)  # type: ignore[arg-type]


def test_morning_brief_once_a_day() -> None:
    b = Brief(date(2026, 10, 19), "Due today: [1] CMSC351 HW 3 11:59pm.", (7,), EOD)
    [r] = morning_brief(snap(brief=b))
    assert (r.rule, r.keys, r.title, r.item_ids, r.expires_at) == (
        Rule.MORNING_BRIEF,
        ("brief:2026-10-19",),
        "Today",
        (7,),
        EOD,
    )
    assert r.sms == "Today: Due today: [1] CMSC351 HW 3 11:59pm."
    assert morning_brief(snap(brief=b, sent={"brief:2026-10-19"})) == []
    assert morning_brief(snap()) == []


def test_exam_countdown_names_weakest_concepts() -> None:
    c = Countdown(EXAM, 3, ("Master theorem", "Memoization"), EOD)
    [r] = exam_countdowns(snap(countdowns=[c]))
    assert r.title == "Exam in 3 days"
    assert "Weakest: Master theorem, Memoization." in r.body and r.item_ids == (1,)
    assert r.keys[0].endswith(":3") and r.keys[0].startswith("1:exam_countdown:")
    [tomorrow] = exam_countdowns(snap(countdowns=[Countdown(EXAM, 1, (), EOD)]))
    assert tomorrow.title == "Exam tomorrow" and "Weakest" not in tomorrow.body
    assert exam_countdowns(snap(countdowns=[c], sent={r.keys[0]})) == []
    undated = Item(2, "CMSC351", ItemKind.EXAM, "Final", None, ItemStatus.UPCOMING)
    assert exam_countdowns(snap(countdowns=[Countdown(undated, 7, (), EOD)])) == []
    assert gsm_length(r.sms) <= SEGMENT


def ann(**kw: object) -> Announced:
    base: dict[str, object] = {
        "finding_id": 5,
        "course_code": "CMSC351",
        "kind": "new_assessment",
        "title": "Quiz 3",
        "status": "applied",
        "date": NOW + timedelta(days=4),
        "item_id": 9,
        "official": None,
        "scope": None,
        "until": NOW + timedelta(days=2),
    }
    base.update(kw)
    return Announced(**base)  # type: ignore[arg-type]


def test_announced_changes() -> None:
    [new] = announced(snap(announced=[ann()]))
    assert (
        new.title == "Announced"
        and "not on ELMS yet" in new.body
        and new.keys == ("finding:5:applied",)
    )
    [conflict] = announced(
        snap(
            announced=[ann(kind="date_change", status="conflict", official=NOW + timedelta(days=2))]
        )
    )
    assert (
        conflict.title == "Dates disagree"
        and "Reply OK 1" in conflict.body
        and conflict.priority == 4
    )
    [moved] = announced(snap(announced=[ann(kind="date_change")]))
    assert moved.title == "Date moved"
    [scope] = announced(snap(announced=[ann(kind="scope", scope="Chapters 1-4", date=None)]))
    assert "covers: Chapters 1-4" in scope.body
    [cancel] = announced(snap(announced=[ann(kind="cancellation", status="pending")]))
    assert "Reply OK 1 to hide it" in cancel.body
    [undated] = announced(snap(announced=[ann(date=None, item_id=None)]))
    assert undated.item_ids == () and "," not in undated.body.split("(")[0].replace(
        "CMSC351 Quiz 3", ""
    )
    assert announced(snap(announced=[ann()], sent={"finding:5:applied"})) == []
    for r in (new, conflict, moved, scope, cancel):
        assert gsm_length(r.sms) <= SEGMENT


def test_candidates_include_the_new_rules() -> None:
    b = Brief(date(2026, 10, 19), "Nothing due today.", (), EOD)
    rules = {
        r.rule
        for r in candidates(
            snap(brief=b, countdowns=[Countdown(EXAM, 3, (), EOD)], announced=[ann()])
        )
    }
    assert {Rule.MORNING_BRIEF, Rule.EXAM_COUNTDOWN, Rule.ANNOUNCED} <= rules


QUIET = TimeWindow(start=time(23), end=time(8))


def test_snooze_defers_everything_but_the_three_hour_warning() -> None:
    ctx = GuardContext(QUIET, [], 0, 6, snoozed={1: NOW + timedelta(hours=2)})
    nudge = Reminder(Rule.START_NUDGE, ("k",), "t", "b", item_ids=(1,))
    assert check(nudge, ctx, NOW) == (Verdict.DEFER, "snoozed")
    assert check(nudge, ctx, NOW + timedelta(hours=2))[0] is Verdict.SEND  # snooze over
    warning = Reminder(Rule.DUE_3H, ("k",), "t", "b", item_ids=(1,), bypass_quiet=True)
    assert check(warning, ctx, NOW)[0] is Verdict.SEND
    batch = Reminder(Rule.NEW_ITEM, ("k",), "t", "b", item_ids=(1, 2))
    assert check(batch, ctx, NOW)[0] is Verdict.SEND  # item 2 is not snoozed
    system = Reminder(Rule.SYSTEM_ALERT, ("k",), "t", "b")
    assert check(system, ctx, NOW)[0] is Verdict.SEND
