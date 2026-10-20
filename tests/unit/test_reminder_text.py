from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from argus.reminders.text import SEGMENT, fit, to_gsm7, when


def test_gsm7_replaces_smart_punctuation_and_unknowns() -> None:
    assert to_gsm7("It’s “due” – now…") == 'It\'s "due" - now...'
    assert to_gsm7("emoji \U0001f600 and 中") == "emoji ? and ?"
    assert to_gsm7("café über") == "café über"  # in the GSM basic set


def test_fit_keeps_short_text_and_cuts_long_at_word() -> None:
    assert fit("short") == "short"
    long = "word " * 50
    out = fit(long)
    assert len(out) <= SEGMENT
    assert out.endswith("...")
    assert not out[:-3].endswith(" ")


@given(st.text())
def test_fit_always_within_segment_and_gsm7(s: str) -> None:
    out = fit(s)
    assert len(out) <= SEGMENT
    assert to_gsm7(out) == out


def test_when_formats_local_time() -> None:
    assert when(datetime(2026, 10, 23, 3, 30, tzinfo=UTC)) == "Thu 11:30pm"
    assert when(datetime(2026, 10, 19, 13, 0, tzinfo=UTC)) == "Mon 9am"
    assert when(datetime(2026, 10, 19, 16, 5, tzinfo=UTC)) == "Mon 12:05pm"
    assert when(datetime(2026, 10, 20, 4, 0, tzinfo=UTC)) == "Tue 12am"


def test_item_numbers_survive_gsm7_and_count_double() -> None:
    from argus.reminders.text import fit, gsm_length, to_gsm7

    assert to_gsm7("[1] HW 3") == "[1] HW 3"  # brackets are GSM-7 (extension table), not "?"
    assert gsm_length("[1]") == 5
    long = "[1] " + "word " * 40
    out = fit(long)
    assert gsm_length(out) <= 160 and out.endswith("...") and out.startswith("[1]")


def test_hours_short_rounds_to_five_minutes_then_half_hours() -> None:
    from argus.reminders.text import hours_short

    assert [hours_short(h) for h in (0.0, 0.25, 0.75, 0.99, 1.0, 1.26, 4.0)] == [
        "5m",
        "15m",
        "45m",
        "1h",
        "1h",
        "1.5h",
        "4h",
    ]
