"""The SMS command parser: every command, the forms people actually type, and what it refuses."""

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.reminders.commands import Command, Verb, is_reserved, parse


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DONE 1", Command(Verb.DONE, 1)),
        ("done 2", Command(Verb.DONE, 2)),
        ("  Done   3 ", Command(Verb.DONE, 3)),
        ("DONE #4", Command(Verb.DONE, 4)),
        ("done 1.", Command(Verb.DONE, 1)),
        ("LOG 1 2h", Command(Verb.LOG, 1, hours=2.0)),
        ("log 1 2", Command(Verb.LOG, 1, hours=2.0)),
        ("LOG 2 1.5 h", Command(Verb.LOG, 2, hours=1.5)),
        ("log 2 90m", Command(Verb.LOG, 2, hours=1.5)),
        ("log 2 90 min", Command(Verb.LOG, 2, hours=1.5)),
        ("LOG 1 1h30m", Command(Verb.LOG, 1, hours=1.5)),
        ("log 1 2 hrs", Command(Verb.LOG, 1, hours=2.0)),
        ("SNOOZE 1 2h", Command(Verb.SNOOZE, 1, duration=timedelta(hours=2))),
        ("snooze 3 30m", Command(Verb.SNOOZE, 3, duration=timedelta(minutes=30))),
        ("snooze 3", Command(Verb.SNOOZE, 3, duration=timedelta(hours=1))),  # default
        ("EST 1 3h", Command(Verb.EST, 1, hours=3.0)),
        ("est 1 4.5", Command(Verb.EST, 1, hours=4.5)),
        ("OK 1", Command(Verb.YES, 1)),
        ("ok", Command(Verb.YES, None)),
        ("Y 2", Command(Verb.YES, 2)),
        ("yes 2", Command(Verb.YES, 2)),
        ("NO 1", Command(Verb.NO, 1)),
        ("n 1", Command(Verb.NO, 1)),
        ("N", Command(Verb.NO, None)),
        ("TODAY", Command(Verb.TODAY)),
        ("today?", Command(Verb.TODAY)),
        ("COMMANDS", Command(Verb.HELP)),
        ("?", Command(Verb.HELP)),
        ("DONE", Command(Verb.DONE, None)),  # number filled from context when only one item is open
        ("SKIP 1", Command(Verb.SKIP, 1)),
        ("skip 2", Command(Verb.SKIP, 2)),
        ("FIRST 3", Command(Verb.FIRST, 3)),
        ("first #1", Command(Verb.FIRST, 1)),
        ("LEFT 1 2h", Command(Verb.LEFT, 1, hours=2.0)),
        ("left 2 90m", Command(Verb.LEFT, 2, hours=1.5)),
        ("LEFT 1 150", Command(Verb.LEFT, 1, hours=150.0)),
    ],
)
def test_parses(text: str, expected: Command) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "finished the homework",
        "DONE one",
        "DONE 0",
        "DONE -1",
        "LOG 1",  # hours are required to log
        "LOG 1 0",
        "LOG 1 25h",  # more than a day in one log is a typo
        "EST 1 0",
        "EST 1 500",
        "SNOOZE 1 0m",
        "SNOOZE 1 8d",
        "DONE 1 2 3",
        "LOG 1 2h extra words",
        "DONE 99999999999999999999",
        "SKIP",  # each queue verb needs a number, so chatter falls through to the AI
        "skip it tonight",
        "skip 1 tonight",
        "FIRST",
        "first thing tomorrow",
        "LEFT 1",
        "LEFT 1 0",
        "LEFT 1 201h",
        "left 2 to go",
        "STOP",  # Twilio handles opt-out and help keywords itself
        "HELP",
        "start",
    ],
)
def test_refuses(text: str) -> None:
    assert parse(text) is None


@given(st.text(max_size=60))
def test_never_raises(text: str) -> None:
    result = parse(text)
    assert result is None or isinstance(result, Command)


@given(st.integers(min_value=1, max_value=99), st.integers(min_value=1, max_value=600))
def test_log_minutes_round_trip(n: int, minutes: int) -> None:
    cmd = parse(f"LOG {n} {minutes}m")
    if minutes > 24 * 60:
        assert cmd is None
    else:
        assert cmd == Command(Verb.LOG, n, hours=minutes / 60)


def test_describe() -> None:
    assert Command(Verb.LOG, 1, hours=1.5).describe() == "LOG 1 1.5h"
    assert Command(Verb.SNOOZE, 2, duration=timedelta(minutes=90)).describe() == "SNOOZE 2 90m"
    assert Command(Verb.TODAY).describe() == "TODAY"
    assert Command(Verb.YES, None).describe() == "OK"


def test_reserved_keywords_belong_to_twilio() -> None:
    for word in (
        "STOP",
        "stop",
        " Unsubscribe ",
        "START",
        "HELP",
        "info",
        "UNSTOP",
        "CANCEL",
        "END",
        "QUIT",
    ):
        assert is_reserved(word)
    assert not is_reserved("DONE 1") and not is_reserved("stop reminding me about hw 3")
