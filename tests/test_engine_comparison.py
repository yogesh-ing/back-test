"""Cross-strategy comparison maths (PRD backTest-enhance §4.3 / §4.4 / §4.5).

Three functions carry the whole of Part 1's Compare analytics:
:func:`correlation_matrix` (are these four rows four bets or one?),
:func:`sharpe_significance` (did the top row actually win, or get lucky?) and
:func:`rebase_to_100` (put every curve on one starting line).

The tests below pin the *behaviour a user would be misled by*, not just the
arithmetic: that a flat curve reads "undefined" rather than 0, that two copies
of the same strategy are not declared "significantly different", and that the
bootstrap pairs the resampled days instead of unlinking them.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtest.engine.comparison import (
    HIGH_CORRELATION,
    SIGNIFICANCE_LEVELS,
    correlation_matrix,
    rebase_to_100,
    sharpe_significance,
)

DAILY_PPY = 252


def _daily(n: int = 500, seed: int = 7) -> pd.DatetimeIndex:
    return pd.bdate_range("2022-01-03", periods=n)


def _series(values, index=None) -> pd.Series:
    index = index if index is not None else _daily(len(values))
    return pd.Series(np.asarray(values, dtype="float64"), index=index)


def _market(seed: int = 11, n: int = 500) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0004, 0.01, n)


# ---------------------------------------------------------------------------
# §4.3 correlation_matrix
# ---------------------------------------------------------------------------


class TestCorrelationMatrix:
    def test_identical_strategies_correlate_at_one(self):
        """The sanity anchor: a strategy against itself is 1.00, not 'n/a'."""
        market = _market()
        out = correlation_matrix({"A": _series(market), "B": _series(market)})
        assert out["available"] is True
        assert out["matrix"][0][1] == pytest.approx(1.0)

    def test_uncorrelated_strategies_score_near_zero(self):
        rng = np.random.default_rng(3)
        a = _series(rng.normal(0.0004, 0.01, 500))
        b = _series(rng.normal(0.0004, 0.01, 500))
        out = correlation_matrix({"A": a, "B": b})
        assert abs(out["matrix"][0][1]) < 0.2

    def test_highly_correlated_pair_is_flagged(self):
        """§4.3: anything above 0.8 is a redundancy the reader must be told about."""
        market = _market()
        noisy = _series(market * 1.1 + np.random.default_rng(5).normal(0, 0.0005, 500))
        out = correlation_matrix({"A": _series(market), "B": noisy})
        high = out["high_correlation_pairs"]
        assert len(high) == 1
        assert high[0]["correlation"] > HIGH_CORRELATION
        assert {high[0]["a"], high[0]["b"]} == {"A", "B"}
        assert out["max_correlation"] == pytest.approx(high[0]["correlation"])

    def test_diversifying_pair_is_not_flagged(self):
        rng = np.random.default_rng(9)
        a = _series(rng.normal(0.0004, 0.01, 500))
        b = _series(rng.normal(0.0004, 0.01, 500))
        out = correlation_matrix({"A": a, "B": b})
        assert out["high_correlation_pairs"] == []
        assert out["warnings"] == []
        # The range is still reported: it is what the heatmap's colour scale is
        # built from, so the panel can colour a matrix that has no flagged pair.
        assert abs(out["max_correlation"]) < 0.2

    def test_flat_curve_is_undefined_not_zero(self):
        """
        A curve that never moved has no correlation to report.

        Reporting 0.00 would be a lie with the sign the reader cares about:
        it reads as "perfectly diversifying", which is the opposite of the
        truth, and would keep a dead strategy in a portfolio.
        """
        market = _market()
        out = correlation_matrix({"A": _series(market), "FLAT": _series(np.zeros(500))})
        assert out["matrix"][0][1] is None
        assert out["matrix"][1][0] is None
        assert out["high_correlation_pairs"] == []
        assert any(w["code"] == "undefined_correlation" for w in out["warnings"])

    def test_diagonal_is_one_for_every_label(self):
        rng = np.random.default_rng(21)
        out = correlation_matrix({n: _series(rng.normal(0, 0.01, 400)) for n in "ABC"})
        for i in range(3):
            assert out["matrix"][i][i] == pytest.approx(1.0)

    def test_series_are_aligned_on_shared_dates_only(self):
        """
        Slots can start on different bars. Correlation is measured on the
        intersection, and the panel says how many bars that was — a cell
        computed from 40 overlapping bars looks exactly as authoritative as
        one computed from 500.
        """
        rng = np.random.default_rng(4)
        a = _series(rng.normal(0, 0.01, 500))
        b = a.iloc[100:].copy()  # same values, 100 bars later
        out = correlation_matrix({"A": a, "B": b})
        assert out["aligned_bars"] == 400
        assert out["matrix"][0][1] == pytest.approx(1.0)

    def test_fewer_than_two_series_is_unavailable_not_an_error(self):
        assert correlation_matrix({})["available"] is False
        assert correlation_matrix({"only": _series(_market())})["available"] is False

    def test_disjoint_dates_yield_no_evidence(self):
        a = _series(_market(n=100))
        b = a.copy()
        b.index = pd.bdate_range("2030-01-01", periods=100)
        out = correlation_matrix({"A": a, "B": b})
        assert out["available"] is False
        assert out["matrix"] == []


# ---------------------------------------------------------------------------
# §4.4 sharpe_significance
# ---------------------------------------------------------------------------


class TestSharpeSignificance:
    def test_two_copies_of_one_strategy_are_never_significant(self):
        """
        The most important guarantee in the panel.

        A pair of near-identical strategies has a real but meaningless Sharpe
        gap. If the bootstrap called that a winner, the panel would be telling
        a user to promote a result that is pure noise.
        """
        market = _market()
        a = _series(market * 1.2)
        b = _series(market * 0.95 + np.random.default_rng(2).normal(0, 0.0008, 500))
        out = sharpe_significance({"A": a, "B": b}, DAILY_PPY, simulations=1000)
        pair = out["comparisons"][0]
        assert pair["verdict"] == "no_significant_difference"
        assert any(w["code"] == "no_significant_winner" for w in out["warnings"])

    def test_a_clearly_better_strategy_wins(self):
        rng = np.random.default_rng(31)
        strong = _series(rng.normal(0.0015, 0.008, 500))
        weak = _series(rng.normal(0.0000, 0.008, 500))
        out = sharpe_significance({"STRONG": strong, "WEAK": weak}, DAILY_PPY, simulations=1000)
        pair = out["comparisons"][0]
        assert pair["verdict"] == "a_better"
        assert pair["winner"] == "STRONG"
        assert pair["a_better_pct"] >= SIGNIFICANCE_LEVELS["a_better"]

    def test_paired_resampling_keeps_correlated_strategies_tied(self):
        """
        The reason this is a *paired* bootstrap (§4.4).

        Two strategies trading the same market days have correlated daily
        returns. Resampling each series independently would let the market
        shocks separate, manufacturing disagreement that never happened, and
        the panel would report a confident winner between two strategies that
        in truth took the same bets.
        """
        market = _market(n=500)
        a = _series(market * 1.1)
        b = _series(market * 0.9)  # perfectly collinear, different leverage
        out = sharpe_significance({"A": a, "B": b}, DAILY_PPY, simulations=1000)
        assert out["comparisons"][0]["verdict"] == "no_significant_difference"

    def test_every_pair_is_reported(self):
        rng = np.random.default_rng(41)
        out = sharpe_significance(
            {n: _series(rng.normal(0, 0.01, 300)) for n in "ABCD"}, DAILY_PPY, simulations=200
        )
        assert len(out["comparisons"]) == 6  # C(4,2) — no pair is dropped
        seen = {(c["a"], c["b"]) for c in out["comparisons"]}
        assert ("A", "D") in seen and ("B", "C") in seen

    def test_percentages_add_to_one_hundred(self):
        """The two-sided confidence must not double-count the same resamples."""
        rng = np.random.default_rng(51)
        out = sharpe_significance(
            {n: _series(rng.normal(0, 0.01, 300)) for n in "AB"}, DAILY_PPY, simulations=500
        )
        pair = out["comparisons"][0]
        assert pair["a_better_pct"] + pair["b_better_pct"] == pytest.approx(100.0, abs=1.0)

    def test_results_are_reproducible_for_a_seed(self):
        """A panel that reorders itself between two identical runs is not evidence."""
        rng = np.random.default_rng(61)
        series = {n: _series(rng.normal(0, 0.01, 300)) for n in "ABC"}
        one = sharpe_significance(series, DAILY_PPY, simulations=300, seed=1234)
        two = sharpe_significance(series, DAILY_PPY, simulations=300, seed=1234)
        assert [c["a_better_pct"] for c in one["comparisons"]] == [
            c["a_better_pct"] for c in two["comparisons"]
        ]

    def test_observed_sharpes_match_the_definition(self):
        series = _series(_market(n=300))
        out = sharpe_significance(
            {"A": series, "B": _series(_market(seed=12, n=300))}, DAILY_PPY, simulations=200
        )
        expected = series.mean() * DAILY_PPY / (series.std(ddof=0) * math.sqrt(DAILY_PPY))
        assert out["observed_sharpe"]["A"] == pytest.approx(expected, abs=1e-3)

    def test_single_strategy_has_nothing_to_compare(self):
        out = sharpe_significance({"A": _series(_market())}, DAILY_PPY)
        assert out["available"] is False
        assert out["comparisons"] == []


# ---------------------------------------------------------------------------
# §4.5 rebase_to_100
# ---------------------------------------------------------------------------


class TestRebaseTo100:
    def test_first_point_becomes_exactly_one_hundred(self):
        """The whole point of the chart: every line starts on the same mark."""
        out = rebase_to_100([40_000.0, 44_000.0, 39_000.0])
        assert out[0] == pytest.approx(100.0)

    def test_relative_moves_are_preserved(self):
        """Indexing must not change what happened; only the unit it is in."""
        out = rebase_to_100([100.0, 150.0, 75.0])
        assert out == pytest.approx([100.0, 150.0, 75.0])

    def test_a_loser_and_a_winner_share_the_same_start(self):
        winner = rebase_to_100([10_000.0, 20_000.0])
        loser = rebase_to_100([10_000.0, 5_000.0])
        assert winner[0] == loser[0] == pytest.approx(100.0)
        assert winner[-1] == pytest.approx(200.0)
        assert loser[-1] == pytest.approx(50.0)

    @pytest.mark.parametrize("base", [0.0, -1.0, float("inf"), float("nan")])
    def test_unusable_start_is_left_alone(self, base):
        """
        A zero, negative or non-finite base cannot be divided into an index.
        Filling the curve with infinities would blank the whole chart, so the
        input is returned untouched and the JS helper does the same.
        """
        values = [base, 10.0, 20.0]
        assert rebase_to_100(values) == values

    def test_empty_input_is_empty_output(self):
        assert rebase_to_100([]) == []
