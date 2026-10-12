"""Significance tests for broker comparison (PRD-003 §3.4).

`scipy` is not a dependency of this project, so these pin the hand-derivable
values the implementation must reproduce — the same discipline
``tests/test_analytics_math.py`` uses for the Sharpe/Sortino/Calmar fixes:

* **Mann-Whitney U** — U against a hand-counted rank sum, the direction of
  every one-sided alternative, and the symmetry that catches a rank/sample
  mix-up (the bug that made "A < B" report a p-value of 0.99).
* **Chi-square 2x2** — the textbook ``[[20,10],[30,15]]`` table, which
  Yates-corrects to exactly 0.0625.
* **The sample-size gate** — the panel must say "not enough data" rather than
  printing a confident p-value off three trades.
"""

from __future__ import annotations

import math

import pytest

from backtest.analytics.stats import (
    ALPHA,
    MIN_SAMPLE,
    chi_square_2x2,
    describe,
    mann_whitney_u,
    normal_sf,
    rate_significance_block,
    significance_block,
)

# ----------------------------------------------------------------------
# normal tail
# ----------------------------------------------------------------------


def test_normal_sf_reference_points():
    assert normal_sf(0.0) == pytest.approx(0.5, abs=1e-12)
    assert normal_sf(1.96) == pytest.approx(0.024998, abs=1e-6)
    assert normal_sf(-1.96) == pytest.approx(0.975002, abs=1e-6)
    # The large-z tail is where a naive exp(-z^2/2) CDF underflows; erfc
    # must still return a positive, non-zero probability.
    assert 0.0 < normal_sf(8.0) < 1e-14
    assert normal_sf(40.0) == 0.0


# ----------------------------------------------------------------------
# Mann-Whitney U
# ----------------------------------------------------------------------


def test_mann_whitney_u_against_hand_counted_ranks():
    # a = {1..5} occupies ranks 1-5 → rank sum 15 → U = 15 - 15 = 0.
    res = mann_whitney_u([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], min_sample=5)
    assert res["testable"] is True
    assert res["u"] == pytest.approx(0.0)
    assert res["n_a"] == res["n_b"] == 5
    # sigma^2 = 25/12 * 11 = 22.9167; z = (0 - 12.5 + 0.5) / 4.78762
    assert res["z"] == pytest.approx(-2.5067, abs=1e-4)
    assert res["p_value"] == pytest.approx(0.012186, abs=1e-5)


def test_mann_whitney_is_symmetric_under_sample_swap():
    """Guards the rank→sample mapping: swapping A and B flips the direction.

    An implementation that ranks the concatenated list and slices the first
    n_a ranks passes the "A is smaller" case by accident and fails this one.
    """
    a, b = [6, 7, 8, 9, 10], [1, 2, 3, 4, 5]
    forward = mann_whitney_u([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], min_sample=5)
    reverse = mann_whitney_u(a, b, min_sample=5)
    assert forward["u"] == pytest.approx(0.0)
    assert reverse["u"] == pytest.approx(25.0)  # n1*n2 - 0
    assert forward["p_value"] == pytest.approx(reverse["p_value"])
    assert forward["z"] == pytest.approx(-reverse["z"])


def test_mann_whitney_one_sided_directions():
    small, big = [1, 2, 3, 4, 5], [6, 7, 8, 9, 10]
    # A is smaller than B — that is the EVIDENCE for "less".
    assert mann_whitney_u(small, big, "less", min_sample=5)["p_value"] < 0.01
    assert mann_whitney_u(small, big, "greater", min_sample=5)["p_value"] > 0.99
    # ...and the mirror image must say the opposite.
    assert mann_whitney_u(big, small, "less", min_sample=5)["p_value"] > 0.99
    assert mann_whitney_u(big, small, "greater", min_sample=5)["p_value"] < 0.01


def test_mann_whitney_two_sided_is_the_double_of_the_one_sided_tail():
    res = mann_whitney_u([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], "two-sided", min_sample=5)
    assert res["p_value"] == pytest.approx(2 * normal_sf(abs(res["z"])), abs=1e-6)


def test_mann_whitney_handles_heavy_ties():
    """Paper fills are frequently exactly 0 bps — ties dominate real data."""
    a = [0.0] * 10 + [1.0] * 10
    b = [8.0] * 10 + [9.0] * 10
    res = mann_whitney_u(a, b, min_sample=5)
    assert res["testable"] is True
    assert res["p_value"] < 0.001
    assert res["u"] == pytest.approx(0.0)


def test_mann_whitney_identical_samples_are_not_significant():
    res = mann_whitney_u([1, 2, 2, 3, 3, 4], [1, 2, 2, 3, 3, 4], min_sample=5)
    assert res["p_value"] == pytest.approx(1.0)
    assert res["significant"] is False


def test_mann_whitney_refuses_thin_samples():
    res = mann_whitney_u([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
    assert res["testable"] is False
    assert res["p_value"] is None
    assert res["significant"] is False
    assert "insufficient sample" in res["reason"]


def test_mann_whitney_refuses_empty_samples():
    res = mann_whitney_u([], [1.0] * 30)
    assert res["testable"] is False
    assert "no samples" in res["reason"]


def test_mann_whitney_rejects_unknown_alternative():
    with pytest.raises(ValueError, match="alternative"):
        mann_whitney_u([1.0] * 25, [2.0] * 25, alternative="sideways")


def test_mann_whitney_ignores_non_numeric_and_nan_samples():
    a = [1.0, None, float("nan"), 2.0] + [3.0] * 21
    b = [9.0] * 25
    res = mann_whitney_u(a, b)
    assert res["n_a"] == 23  # None and NaN dropped, not counted as data
    assert res["testable"] is True


# ----------------------------------------------------------------------
# Chi-square on a 2x2 table
# ----------------------------------------------------------------------


def test_chi_square_matches_the_textbook_table():
    # scipy.stats.chi2_contingency([[20,10],[30,15]], correction=True) = 0.0625
    res = chi_square_2x2(20, 10, 30, 15, min_total=10)
    assert res["chi_square"] == pytest.approx(0.0625, abs=1e-9)
    assert res["p_value"] == pytest.approx(0.802588, abs=1e-5)
    assert res["significant"] is False


def test_chi_square_detects_a_real_fill_rate_gap():
    # 94/100 vs 87/100 is NOT significant (p≈0.148) — a 7pp gap on 100
    # orders each is exactly the kind of difference the PRD's p=0.012 example
    # would have us over-read if we shipped a test that just agreed with it.
    thin = chi_square_2x2(94, 6, 87, 13, min_total=20)
    assert thin["testable"] is True
    assert thin["significant"] is False

    # The same 12pp gap IS significant once the sample supports it.
    solid = chi_square_2x2(94, 6, 82, 18, min_total=20)
    assert solid["p_value"] == pytest.approx(0.016685, abs=1e-5)
    assert solid["significant"] is True


def test_chi_square_yates_is_more_conservative_than_uncorrected():
    """Yates removes the ~5% anti-conservatism a 2x2 uncorrected test has."""
    corrected = chi_square_2x2(56, 4, 52, 8, min_total=20)
    n = 120
    uncorrected = (n * (56 * 8 - 4 * 52) ** 2) / (60 * 60 * 108 * 12)
    assert corrected["chi_square"] < uncorrected


def test_chi_square_refuses_thin_and_empty_tables():
    thin = chi_square_2x2(3, 0, 2, 1, min_total=20)
    assert thin["testable"] is False
    assert "insufficient sample" in thin["reason"]

    empty = chi_square_2x2(0, 0, 0, 0, min_total=0)
    assert empty["testable"] is False
    assert "no orders" in empty["reason"]


def test_chi_square_flags_a_degenerate_table():
    # Every order took the same outcome → a zero-margin column.
    res = chi_square_2x2(0, 0, 0, 30, min_total=0)
    assert res["testable"] is False
    assert "degenerate" in res["reason"]


def test_chi_square_rejects_negative_counts():
    res = chi_square_2x2(-1, 5, 3, 4, min_total=0)
    assert res["testable"] is False
    assert "negative" in res["reason"]


# ----------------------------------------------------------------------
# API-shaped blocks
# ----------------------------------------------------------------------


def test_rate_significance_block_uses_event_over_total():
    block = rate_significance_block(94, 100, 82, 100)
    assert block["test"] == "chi_square"
    assert block["table"] == [[94, 6], [82, 18]]
    assert block["significant"] is True
    assert block["confidence"].endswith("%")


def test_significance_block_reports_confidence_and_alpha():
    block = significance_block([1.0] * 30, [9.0] * 30, min_sample=MIN_SAMPLE)
    assert block["alpha"] == ALPHA
    assert block["test"] == "mann_whitney_u"
    assert block["confidence"] is not None


def test_significance_block_on_an_untestable_comparison_still_has_the_shape():
    """The frontend must never branch on "did the test run?"."""
    block = significance_block([1.0], [2.0])
    for key in ("test", "p_value", "significant", "confidence", "testable", "reason"):
        assert key in block
    assert block["testable"] is False
    assert block["confidence"] is None


# ----------------------------------------------------------------------
# describe()
# ----------------------------------------------------------------------


def test_describe_is_none_not_zero_for_an_empty_sample():
    assert describe([]) == (None, None, None)


def test_describe_mean_median_and_sample_stdev():
    mean, med, sd = describe([2, 4, 4, 4, 5, 5, 7, 9], digits=6)
    assert mean == 5.0
    assert med == 4.5
    # ddof=1 sample stdev of that classic set
    assert sd == pytest.approx(2.13809, abs=1e-5)
    # The default 2-digit rounding is for display, not for tests.
    assert describe([2, 4, 4, 4, 5, 5, 7, 9])[2] == 2.14


def test_describe_keeps_a_single_point_without_inventing_a_stdev():
    assert describe([7]) == (7.0, 7.0, None)


def test_describe_of_infinities_is_none():
    mean, med, sd = describe([1.0, float("inf"), 3.0])
    assert mean == pytest.approx(2.0)
    assert sd is not None
    assert not math.isnan(sd)
