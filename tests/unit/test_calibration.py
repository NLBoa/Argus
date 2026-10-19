import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.domain.models import ItemKind
from argus.planner.calibration import (
    DEFAULT_HOURS,
    Effective,
    Finished,
    K,
    calibrate,
    factor,
    median_actual,
)

A = ItemKind.ASSIGNMENT


def fin(actual: float, p50: float, course: str = "CMSC351", kind: ItemKind = A) -> Finished:
    return Finished(course, kind, p50, actual)


def test_no_history_means_no_adjustment() -> None:
    f = factor([], "CMSC351", A)
    assert (f.m, f.n) == (1.0, 0)


def test_spec_formula() -> None:
    # Two items took 2x their p50: m = (2*2 + 3) / (2 + 3) = 1.4
    f = factor([fin(4, 2), fin(6, 3)], "CMSC351", A)
    assert f.n == 2 and f.r_bar == 2.0 and f.m == pytest.approx(1.4)
    assert K == 3


def test_only_same_course_and_kind_counts() -> None:
    f = factor(
        [
            fin(4, 2),
            fin(10, 1, course="MATH240"),
            fin(10, 1, kind=ItemKind.QUIZ),
            fin(0, 2),
            fin(3, 0),
        ],
        "CMSC351",
        A,
    )
    assert f.n == 1  # zero actuals and zero estimates are ignored too


@given(st.lists(st.tuples(st.floats(0.25, 40), st.floats(0.25, 40)), max_size=30))
def test_factor_is_between_one_and_mean_ratio(pairs: list[tuple[float, float]]) -> None:
    hist = [fin(a, p) for a, p in pairs]
    f = factor(hist, "CMSC351", A)
    lo, hi = sorted((1.0, f.r_bar if f.n else 1.0))
    assert lo - 1e-9 <= f.m <= hi + 1e-9


def test_calibrate_and_override_wins() -> None:
    f = factor([fin(4, 2), fin(6, 3)], "CMSC351", A)
    e = calibrate(raw_p50=3.0, raw_p80=4.0, f=f, override=None)
    assert e == Effective(
        p50=pytest.approx(4.2),
        p80=pytest.approx(5.6),
        raw_p50=3.0,
        raw_p80=4.0,
        overridden=False,
        factor=pytest.approx(1.4),
    )
    o = calibrate(raw_p50=3.0, raw_p80=4.0, f=f, override=7.5)
    assert (o.p50, o.p80, o.overridden) == (7.5, 7.5, True)


def test_median_actual_fallback() -> None:
    assert (
        median_actual([fin(3, 2), fin(5, 2), fin(9, 2), fin(1, 1, course="X")], "CMSC351", A) == 5
    )
    assert median_actual([fin(3, 2), fin(5, 2)], "CMSC351", A) == 4
    assert median_actual([], "CMSC351", A) is None
    assert DEFAULT_HOURS[ItemKind.QUIZ] < DEFAULT_HOURS[A] < DEFAULT_HOURS[ItemKind.EXAM]
