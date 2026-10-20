"""The /workload chart's geometry (D065): step lines, the shortfall, the axis, the labels. Pure."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from argus.clock import local_datetime
from argus.planner.workload import CurveDay
from argus.web.viewmodels import workload_chart, workload_days

MON = date(2026, 10, 19)


def days(window: list[float], due: list[float]) -> list[CurveDay]:
    out, wt, dt = [], 0.0, 0.0
    for n, (w, d) in enumerate(zip(window, due, strict=True)):
        wt, dt = wt + w, dt + d
        out.append(CurveDay(local_datetime(MON + timedelta(days=n), 0), w, d, wt, dt))
    return out


def test_day_labels_name_today_and_the_weekday() -> None:
    v = workload_days(days([4, 4, 0], [0, 3, 1]), MON)
    assert [(d.label, d.short_label) for d in v] == [
        ("Today", "Mon"),
        ("Tomorrow", "Tue"),
        ("Wednesday", "Wed"),
    ]
    assert v[2].window_total == 8 and v[2].due_total == 4


def test_step_paths_rise_at_each_day_on_a_clean_axis() -> None:
    c = workload_chart(workload_days(days([4, 4], [0, 9]), MON))
    assert c.top == 10 and [t.text for t in c.ticks] == ["0", "5 h", "10 h"]
    assert c.window_path == "M0 1000 V600 H100 V200 H200"
    assert c.due_path == "M0 1000 V1000 H100 V100 H200"


def test_shortfall_is_shaded_and_labelled_where_it_is_largest() -> None:
    c = workload_chart(workload_days(days([4, 4, 2], [6, 4, 0]), MON))
    assert [(r.x, r.y, r.h) for r in c.short] == [(0, 400, 200), (100, 0, 200)]
    assert c.short_label is not None
    assert c.short_label.text == "2 h short"
    assert c.short_label.left == pytest.approx(100 / 6)  # the first of the two equal days, centred


def test_no_shortfall_no_label() -> None:
    c = workload_chart(workload_days(days([4, 4], [1, 1]), MON))
    assert c.short == [] and c.short_label is None


def test_end_labels_drop_when_the_lines_end_close_together() -> None:
    apart = workload_chart(workload_days(days([8, 8], [1, 1]), MON))
    assert [e.text for e in apart.ends] == ["Window time 16 h", "Work due 2 h"]
    close = workload_chart(workload_days(days([4, 4], [4, 3.5]), MON))
    assert close.ends == []  # the legend and the tooltip carry them


def test_an_empty_fortnight_still_has_an_axis() -> None:
    c = workload_chart(workload_days(days([0] * 14, [0] * 14), MON))
    assert c.top == 4 and c.short == []


def test_tooltip_strings_say_zero_plainly() -> None:
    c = workload_chart(workload_days(days([0, 4], [2, 0]), MON))
    assert c.tips == [["Today", "2 h", "0 h", "2 h"], ["Tomorrow", "2 h", "4 h", ""]]


def test_without_work_windows_there_is_no_window_line_and_no_shortfall() -> None:
    c = workload_chart(workload_days(days([0, 0], [6, 0]), MON), windows_on=False)
    assert c.window_path == "" and c.short == [] and c.short_label is None
    assert [e.text for e in c.ends] == ["Work due 6 h"]
    assert c.tips[0] == ["Today", "6 h", "", ""]
