"""Tests for the rental timing calculator PoC (math-only; no data I/O)."""

from __future__ import annotations

import datetime as dt

import pytest

from rental_timing_calculator.calculate import (
    adjusted_rent,
    candidate_starts,
    find_best_date,
    first_year_cost,
    month_delta,
    monthly_starts,
    overlap_days,
)

LEASE_END = dt.date(2027, 4, 30)
BASE = 4000.0


def test_overlap_days_clamps_at_zero_after_lease_end() -> None:
    assert overlap_days(dt.date(2027, 4, 30), LEASE_END) == 0
    assert overlap_days(dt.date(2027, 5, 15), LEASE_END) == 0
    assert overlap_days(dt.date(2027, 3, 31), LEASE_END) == 30


def test_month_delta_scales_by_haircut() -> None:
    jan = dt.date(2027, 1, 15)
    assert month_delta(jan, 1.0) == pytest.approx(-0.019)
    assert month_delta(jan, 0.5) == pytest.approx(-0.0095)


def test_adjusted_rent_computes_r_times_one_plus_delta() -> None:
    dec_delta = month_delta(dt.date(2026, 12, 1), 1.0)  # -0.020
    assert adjusted_rent(BASE, dec_delta) == pytest.approx(3920.0)


def test_first_year_cost_matches_readme_formula() -> None:
    # Apr 15 start: δ = +0.3%, overlap 15d, C = 12·R_m + 15·R_m/30.
    d = dt.date(2027, 4, 15)
    rm = BASE * (1 + 0.003)
    expected = 12 * rm + 15 * rm / 30
    assert first_year_cost(d, BASE, 1.0, LEASE_END) == pytest.approx(expected)


def test_no_overlap_baseline_wins_given_readme_delta_scale() -> None:
    # With overlap at this rent (~$134/day), even a ≤90-day winter overlap is
    # not enough: the late-April baseline minimizes cost (matches README's
    # "default safe path" guidance in step 4).
    dates = candidate_starts(dt.date(2026, 12, 1), dt.date(2027, 6, 30))
    result = find_best_date(dates, BASE, 1.0, LEASE_END, max_overlap=90)
    assert result.ideal_date == LEASE_END
    assert result.ideal_cost == pytest.approx(result.baseline_cost)


def test_find_best_date_respects_max_overlap_cap() -> None:
    dates = candidate_starts(dt.date(2026, 12, 1), dt.date(2027, 6, 30))
    # Cap of 15 days: only starts on/after Apr 15 are feasible.
    result = find_best_date(dates, BASE, 1.0, LEASE_END, max_overlap=15)
    assert overlap_days(result.ideal_date, LEASE_END) <= 15
    assert result.ideal_date >= dt.date(2027, 4, 15)  # feasible window boundary


def test_find_best_date_raises_when_scan_has_no_feasible_dates() -> None:
    dates = candidate_starts(dt.date(2026, 12, 1), dt.date(2027, 2, 1))
    with pytest.raises(ValueError, match="No start date"):
        find_best_date(dates, BASE, 1.0, LEASE_END, max_overlap=10)


def test_candidate_starts_inclusive_and_daily() -> None:
    dates = candidate_starts(dt.date(2027, 1, 1), dt.date(2027, 1, 31))
    assert len(dates) == 31
    assert dates[0] == dt.date(2027, 1, 1)
    assert dates[-1] == dt.date(2027, 1, 31)


def test_monthly_starts_lists_first_of_each_month() -> None:
    months = monthly_starts(dt.date(2026, 12, 15), dt.date(2027, 3, 20))
    assert months == [dt.date(2027, 1, 1), dt.date(2027, 2, 1), dt.date(2027, 3, 1)]
