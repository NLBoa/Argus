"""Runna feed parsing (D078): planned runs become RUN assignments; completions mark them done."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from argus.collectors.runna_ical import (
    RunnaFeed,
    StoredRun,
    display_title,
    match_completed,
    normalize_title,
    parse_duration_text,
    parse_runna_feed,
    run_due,
)
from argus.config import RunnaConfig
from argus.domain.models import CompletedRun, ItemKind, Source, SourceItem
from tests.conftest import FIXTURES

FIXTURE = (FIXTURES / "runna" / "feed.ics").read_bytes()
FIXTURE_REPLACED = (FIXTURES / "runna" / "feed_replaced.ics").read_bytes()

TEMPO_ID = "uid-tempo"
TEMPO_DUE = datetime(2026, 11, 11, 4, 59, tzinfo=UTC)  # 23:59 EST Tue Nov 10
TEMPO_START = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)


def parse(body: bytes = FIXTURE, runna: RunnaConfig | None = None) -> RunnaFeed:
    return parse_runna_feed(body, runna or RunnaConfig())


def by_title(title: str, runna: RunnaConfig | None = None) -> SourceItem:
    return next(i for i in parse(FIXTURE, runna).runs if i.title == title)


def stored(
    external_id: str = TEMPO_ID,
    title: str = "Tempo Run",
    *,
    open_: bool = True,
    raw_hash: str = "stored-hash",
) -> StoredRun:
    return StoredRun(
        external_id=external_id,
        title=title,
        due_at=TEMPO_DUE,
        description=None,
        duration_minutes=45,
        preferred_start=TEMPO_START,
        open=open_,
        raw_hash=raw_hash,
    )


def done(external_id: str, title_key: str, raw_hash: str = "done-hash") -> CompletedRun:
    return CompletedRun(external_id, date(2026, 11, 10), title_key, raw_hash)


# ---------------------------------------------------------------- duration text


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("45 min", 45),
        ("Easy · 50 mins", 50),
        ("1h 5m", 65),
        ("1 hr 5 min", 65),
        ("1:05:00", 65),
        ("65 min", 65),
        ("2 hours", 120),
        ("45m", None),  # bare m is ambiguous with metres
        ("65:00", None),  # mm:ss is not a duration
        ("8 km", None),
        ("5 x 400m", None),
        ("Zone 2", None),
        ("", None),
        ("Warm up 10 min · 4 x 3 min @ 5:10/km · cool down 10 min", None),
        ("5:30/km", None),
        ("8:45 min/mi", None),
        ("6:30-7:00/km", None),
        ("Easy Run • 4mi • 35m - 40m", None),  # Runna's range line: bare m only
        (
            "Estimated duration: 42 min · Warm up 10 min · 4 x 3 min @ 5:10/km · cool down 10 min",
            42,
        ),
        ("Estimated duration 1h 5m · easy 6:30/km", 65),
        ("Duration: 45m", 45),  # bare m inside a labeled total means minutes
        ("Easy 45m · strides", None),  # bare m elsewhere is not a duration
    ],
)
def test_parse_duration_text(text: str, minutes: int | None) -> None:
    assert parse_duration_text(text) == minutes


# ---------------------------------------------------------------- titles


@pytest.mark.parametrize(
    ("summary", "key"),
    [
        ("🏃 4mi Easy Run • 4mi", "4mi easy run"),
        ("🏃 4mi Easy Run", "4mi easy run"),
        ("🏃 Drop Set • 3.6mi", "drop set"),
        ("🏃  Tempo   Run ", "tempo run"),
        ("🏃 5km Time Trial • 3.9mi", "5km time trial"),
    ],
)
def test_normalize_title(summary: str, key: str) -> None:
    assert normalize_title(summary) == key


@pytest.mark.parametrize(
    ("summary", "title"),
    [
        ("🏃 4mi Easy Run • 4mi", "4mi Easy Run"),
        ("🏃 Drop Set • 3.6mi", "Drop Set • 3.6mi"),
        ("🏃 Easy Run", "Easy Run"),
        ("🏃", "Run"),
        ("", "Run"),
    ],
)
def test_display_title(summary: str, title: str) -> None:
    assert display_title(summary) == title


# ---------------------------------------------------------------- the feed


def test_items_from_fixture() -> None:
    feed = parse()
    items = {i.title: i for i in feed.runs}
    assert set(items) == {
        "Tempo Run",
        "4mi Easy Run",
        "Steady Run • 5mi",
        "Night Run",
        "6.5mi Long Run",
        "Recovery Run",
    }
    a = items["Tempo Run"]
    assert a.due_at == TEMPO_DUE
    assert a.duration_minutes == 45  # DTEND - DTSTART of a timed run
    assert a.preferred_start == TEMPO_START
    assert a.submitted is True  # completed twin (f) in the same fetch
    b = items["4mi Easy Run"]
    assert b.duration_minutes == 50  # X-WORKOUT-ESTIMATED-DURATION 3000 s
    assert b.preferred_start == datetime(2026, 11, 11, 21, 0, tzinfo=UTC)  # 16:00 EST
    assert items["Steady Run • 5mi"].duration_minutes is None
    assert items["Steady Run • 5mi"].submitted is None
    assert items["Recovery Run"].duration_minutes == 30  # from the text
    assert items["6.5mi Long Run"].duration_minutes == 65
    # (g) has no planned twin; (f) was a twin; (k) is not a plan workout and is ignored.
    assert [(c.local_day, c.title_key) for c in feed.completed] == [
        (date(2026, 11, 5), "hill repeats")
    ]


def test_rest_days_and_strength_sessions_are_skipped() -> None:
    titles = {i.title for i in parse().runs}
    assert "Rest day" not in titles and "Strength & mobility" not in titles


def test_every_item_is_a_run_assignment() -> None:
    runs = parse().runs
    assert runs
    for i in runs:
        assert i.source is Source.RUNNA_ICAL
        assert i.course_code == "RUN" and i.kind is ItemKind.ASSIGNMENT and i.all_day
        assert i.external_id.startswith("uid-") and len(i.external_id) == 20


def test_late_timed_run_is_due_on_its_local_day() -> None:  # Review Focus 1
    d = by_title("Night Run")
    assert d.due_at == datetime(2026, 11, 14, 4, 59, tzinfo=UTC)  # 23:59 EST Fri Nov 13
    assert d.duration_minutes == 40


def test_fall_back_day_due_is_est() -> None:  # Review Focus 2
    assert by_title("6.5mi Long Run").due_at == datetime(2026, 11, 2, 4, 59, tzinfo=UTC)
    assert run_due(date(2026, 11, 1)) == datetime(2026, 11, 2, 4, 59, tzinfo=UTC)
    assert run_due(date(2026, 10, 31)) == datetime(2026, 11, 1, 3, 59, tzinfo=UTC)  # EDT


def test_custom_preferred_time() -> None:
    b = by_title("4mi Easy Run", RunnaConfig(preferred_time=time(7, 30)))
    assert b.preferred_start == datetime(2026, 11, 11, 12, 30, tzinfo=UTC)


def test_raw_hash_ignores_dtstamp() -> None:
    restamped = FIXTURE.replace(b"DTSTAMP:20261109T120000Z", b"DTSTAMP:20261110T080000Z")
    assert restamped != FIXTURE
    assert [i.raw_hash for i in parse(restamped).runs] == [i.raw_hash for i in parse().runs]


def test_malformed_feed_raises() -> None:
    with pytest.raises(ValueError, match=r"iCal|VCALENDAR"):
        parse(b"not ical")


def test_replaced_run_is_a_completed_orphan() -> None:
    feed = parse(FIXTURE_REPLACED)
    assert "Tempo Run" not in {i.title for i in feed.runs}
    assert (date(2026, 11, 10), "tempo run") in {(c.local_day, c.title_key) for c in feed.completed}


# ---------------------------------------------------------------- matching completions


def test_match_completed_by_same_id() -> None:
    [hit] = match_completed([done(TEMPO_ID, "renamed")], [stored()])
    assert hit.external_id == TEMPO_ID and hit.submitted is True


def test_match_completed_by_day_and_title() -> None:
    [hit] = match_completed([done("uid-new", "tempo run", "h1")], [stored()])
    assert hit.external_id == TEMPO_ID and hit.submitted is True
    assert hit.duration_minutes == 45 and hit.preferred_start == TEMPO_START
    assert hit.due_at == TEMPO_DUE and hit.title == "Tempo Run"
    assert hit.raw_hash == "h1"  # the completion's hash, so the next fetch can find it again
    assert hit.course_code == "RUN" and hit.kind is ItemKind.ASSIGNMENT and hit.all_day


def test_match_completed_only_open_run_that_day() -> None:  # case (c), renamed
    [hit] = match_completed([done("uid-new", "morning run")], [stored()])
    assert hit.external_id == TEMPO_ID and hit.submitted is True


def test_match_completed_two_runs_that_day_is_no_match() -> None:
    two = [stored(), stored("uid-x", "Shakeout")]
    assert match_completed([done("uid-new", "morning run")], two) == []


def test_match_completed_ignores_done_runs_for_the_day_fallback() -> None:
    assert match_completed([done("uid-new", "morning run")], [stored(open_=False)]) == []


def test_match_completed_finds_its_earlier_match_by_hash() -> None:
    # A renamed completion matched last fetch (the run is now done): it must keep matching.
    run = stored(open_=False, raw_hash="done-hash")
    [hit] = match_completed([done("uid-new", "morning run", "done-hash")], [run])
    assert hit.external_id == TEMPO_ID


def test_unmatched_completed_is_dropped() -> None:  # fixture (g)
    g = CompletedRun("uid-g", date(2026, 11, 5), "hill repeats", "h")
    assert match_completed([g], [stored()]) == []
