"""Today's view models: the queue cards and hero (D058, D064), the confirm line (D060), the day
strip, and the kiosk rail's lanes and real hours (D063). Pure."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from argus.clock import local_datetime
from argus.demo import DEMO_CONFIG
from argus.domain.models import ItemKind, ItemStatus
from argus.planner.freetime import Busy
from argus.planner.risk import RiskLevel
from argus.web.viewmodels import (
    CourseView,
    ItemView,
    QueueEntryFacts,
    confirm_choices,
    confirm_when,
    queue_view,
    strip,
    timeline,
)

MON = date(2026, 10, 19)
NOW = local_datetime(MON, 9)
COURSE = CourseView("CMSC351", "teal")


def view(item_id: int, title: str, due: datetime | None = None) -> ItemView:
    return ItemView(
        id=item_id,
        title=title,
        course=COURSE,
        kind=ItemKind.ASSIGNMENT,
        kind_label="Assignment",
        status=ItemStatus.UPCOMING,
        status_label="Not started",
        risk=RiskLevel.OK,
        due_at=due,
        due_text=None,
        due_relative=None,
        sources=(),
        announced=False,
        conflict=(),
    )


def fact(item_id: int, **kw: object) -> QueueEntryFacts:
    base: dict[str, object] = {
        "entry_id": f"item:{item_id}",
        "kind": "work",
        "item_id": item_id,
        "remaining": 2.0,
        "capacity": 4.0,
        "demand": 2.0,
        "level": RiskLevel.OK,
        "pinned": False,
        "moved": False,
        "skipped": False,
        "tonight_hours": None,
    }
    base.update(kw)
    return QueueEntryFacts(**base)  # type: ignore[arg-type]


VIEWS = {i: view(i, f"Item {i}") for i in range(1, 6)}


def q(facts: list[QueueEntryFacts], **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "windows_on": True,
        "window_text": "Tonight 7-11pm",
        "can_skip": True,
        "timer_item_id": None,
    }
    args.update(kw)
    return queue_view(facts, VIEWS, **args)  # type: ignore[arg-type]


def test_the_hero_is_first_and_the_rest_keep_their_numbers_and_neighbors() -> None:
    v = q([fact(1), fact(2), fact(3, skipped=True), fact(4)])
    assert v.hero is not None and v.hero.entry_id == "item:1" and v.hero.position == 1
    assert [(c.entry_id, c.position) for c in v.cards] == [("item:2", 2), ("item:4", 3)]
    assert v.hero.up_before is None and v.hero.down_before == "item:2"
    assert v.cards[-1].down_before is None and v.cards[-1].up_before == "item:2"
    assert [c.entry_id for c in v.skipped] == ["item:3"] and v.skipped[0].position is None


def test_while_a_timer_runs_its_card_is_the_hero() -> None:
    v = q([fact(1), fact(2), fact(3)], timer_item_id=3)
    assert v.hero is not None and v.hero.entry_id == "item:3" and v.hero.position == 3
    assert [c.entry_id for c in v.cards] == ["item:1", "item:2"]
    elsewhere = q([fact(1), fact(2)], timer_item_id=9)  # timing something not in the queue
    assert elsewhere.hero is None and len(elsewhere.cards) == 2


def test_focus_names_the_container_the_control_ends_up_in() -> None:
    assert q([fact(1), fact(2)], focus=("item:2", "up")).focus == "q-item-2:up"
    assert q([fact(1), fact(2)], focus=("item:1", "down")).focus == "hero:down"
    assert q([fact(1)], focus=("confirm", "confirm")).focus == "confirm:confirm"


def test_reasons_and_labels() -> None:
    risky = q([fact(1, level=RiskLevel.AT_RISK, capacity=2.0, demand=5.5)]).hero
    assert risky is not None and risky.marker is RiskLevel.AT_RISK
    assert risky.reason == "Needs 3.5 h more than your windows have before it's due"
    ok = q([fact(1, capacity=6.0)]).hero
    assert (
        ok is not None and ok.reason == "6 h of window time before it's due" and ok.marker is None
    )
    assert q([fact(1, capacity=0.0)]).hero.reason == "No work window before it's due"  # type: ignore[union-attr]
    assert q([fact(1)], windows_on=False).hero.reason is None  # type: ignore[union-attr]
    tiny = q([fact(1, remaining=0.05)]).hero
    assert tiny is not None and tiny.left_text == "~15 min left"  # shown floor, display only
    done = q([fact(1, remaining=0.0)]).hero
    assert (
        done is not None
        and done.left_text is None
        and done.reason == "Nothing left by your estimate"
    )
    study = q([fact(2, kind="study", entry_id="study:2", remaining=0.75, shortfall=1.5)]).hero
    assert study is not None and study.title == "Study for Item 2"
    assert study.left_text == "~45 min set aside" and not study.can_skip
    assert study.reason == "Study time short by 1.5 h before the exam"
    pinned = q([fact(1, pinned=True, moved=True)]).hero
    assert pinned is not None and pinned.pinned and not pinned.moved
    assert not q([fact(1)], can_skip=False).hero.can_skip  # type: ignore[union-attr]


def test_the_confirm_line_words() -> None:
    assert (
        confirm_when(local_datetime(MON, 19), local_datetime(MON + timedelta(days=1), 8))
        == "Last night"
    )
    assert (
        confirm_when(local_datetime(MON, 13), local_datetime(MON + timedelta(days=1), 8))
        == "Yesterday"
    )
    assert confirm_when(local_datetime(MON, 13), local_datetime(MON, 19)) == "Earlier today"
    assert (
        confirm_when(local_datetime(MON, 19), local_datetime(MON + timedelta(days=2), 8))
        == "On Monday"
    )
    assert confirm_choices(4.0) == (4.0, 2.0, 0.0)
    assert confirm_choices(0.3) == (1.0, 0.5, 0.0)
    assert confirm_choices(1.4) == (1.5, 1.0, 0.0)


def busy(h1: int, m1: int, h2: int, m2: int, title: str = "CMSC351 lecture") -> Busy:
    return Busy(local_datetime(MON, h1, m1), local_datetime(MON, h2, m2), "class", title, "CMSC351")


def test_the_strip_covers_the_busy_part_of_the_day_with_overlaps_in_lanes() -> None:
    s = strip(
        MON,
        [busy(10, 0, 11, 0), busy(10, 30, 11, 30, "MATH240 lab")],
        NOW,
        courses={"CMSC351": COURSE},
        due_items=[view(1, "Homework 3", local_datetime(MON, 23, 30))],
        windows=[(local_datetime(MON, 19), local_datetime(MON, 23))],
    )
    assert s.lanes == 2 and sorted(b.lane for b in s.bars) == [0, 1]
    first = s.ticks[0]
    assert first[0] == "9 am"  # 8:30 am (now 9 am - 30 min) rounds up to the first whole hour
    assert s.dues[0].text == "11:30 pm" and s.dues[0].left_pct < 100  # the range runs to midnight
    assert s.now_pct is not None and s.now_pct > 0
    assert len(s.windows) == 1 and not s.empty


def test_an_empty_day_has_an_empty_strip() -> None:
    assert strip(MON, [], NOW, courses={}).empty


def test_the_kiosk_rail_puts_overlaps_in_columns_and_takes_in_late_deadlines() -> None:
    cfg = DEMO_CONFIG.model_copy(
        update={
            "schedule": DEMO_CONFIG.schedule.model_copy(
                update={
                    "waking_window": DEMO_CONFIG.schedule.waking_window.model_copy(
                        update={"start": time(8), "end": time(22)}
                    )
                }
            )
        }
    )
    tl = timeline(
        MON,
        [busy(10, 0, 11, 0), busy(10, 30, 11, 30, "MATH240 lab")],
        NOW,
        config=cfg,
        courses={"CMSC351": COURSE},
        due_items=[view(1, "Homework 3", local_datetime(MON, 23, 59))],
    )
    assert {(b.lane, b.lanes) for b in tl.blocks} == {(0, 2), (1, 2)}
    assert tl.end_hour == 24 and tl.span_hours == 16  # stretched from 10 pm to midnight
    assert [m.text for m in tl.due_marks] == ["11:59 pm"]


def test_the_rail_labels_real_hours_on_the_fall_back_day() -> None:
    cfg = DEMO_CONFIG.model_copy(
        update={
            "schedule": DEMO_CONFIG.schedule.model_copy(
                update={
                    "waking_window": DEMO_CONFIG.schedule.waking_window.model_copy(
                        update={"start": time(0), "end": time(23, 59)}
                    )
                }
            )
        }
    )
    tl = timeline(
        date(2026, 11, 1), [], local_datetime(date(2026, 11, 1), 9), config=cfg, courses={}
    )
    assert tl.span_hours == 25
    assert [h for h, _ in tl.hours[:4]] == ["12 am", "1 am", "1 am", "2 am"]
