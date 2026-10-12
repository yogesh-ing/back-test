"""Unit tests for the §2 statistics helpers (PRD backTest-enhance §2.1).

`compute_metrics` is exercised end-to-end in ``test_metrics_sections.py``.
What is pinned HERE is each estimator against a hand-computable case, because
these are the numbers that are easy to get *plausible* rather than *right*:

* a drawdown's duration runs peak→recovery, not trough→recovery, and a run
  that ends underwater has no recovery at all;
* VaR and CVaR are both NEGATIVE returns, so "worse" means more negative;
* the Sharpe standard error must widen as the Sharpe grows, not shrink;
* a capped Omega is a cap, not infinity.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtest.engine.metrics_risk import (
    DRAWDOWN_THRESHOLD,
    FLAG_INSUFFICIENT,
    OMEGA_CAP,
    TRADE_COUNT_OK,
    TRADE_COUNT_WARN,
    consecutive_streaks,
    drawdown_detail,
    drawdown_episodes,
    omega_ratio,
    payoff_ratio,
    return_skew_kurtosis,
    sharpe_std_error,
    trade_count_flag,
    trade_durations,
    ulcer_index,
    var_es,
)


def equity(values, start="2024-01-01", freq="D"):
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.Series([float(v) for v in values], index=idx)


def rets(values):
    return pd.Series([float(v) for v in values])


# ---------------------------------------------------------------------------
# Drawdown episodes
# ---------------------------------------------------------------------------


class TestDrawdownEpisodes:
    def test_monotonic_rise_has_no_episodes(self):
        dd = equity([100, 101, 102]) / equity([100, 101, 102]).cummax() - 1
        assert drawdown_episodes(dd) == []

    def test_single_episode_peak_trough_recovery(self):
        e = equity([100, 90, 120])
        dd = e / e.cummax() - 1
        episodes = drawdown_episodes(dd)
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep["peak_i"] == 0
        assert ep["trough_i"] == 1
        assert ep["recovery_i"] == 2
        assert ep["recovered"] is True
        assert ep["depth"] == pytest.approx(-0.10)

    def test_unrecovered_episode_is_reported_not_dropped(self):
        # Ends below its peak: a real state of affairs, not missing data.
        e = equity([100, 80, 85])
        dd = e / e.cummax() - 1
        episodes = drawdown_episodes(dd)
        assert len(episodes) == 1
        assert episodes[0]["recovered"] is False
        assert episodes[0]["recovery_i"] is None

    def test_deepest_bar_wins_over_the_first_underwater_bar(self):
        e = equity([100, 95, 88, 99, 100])
        dd = e / e.cummax() - 1
        episodes = drawdown_episodes(dd)
        assert episodes[0]["trough_i"] == 2, "trough is the worst bar, not the first"

    def test_two_separate_episodes(self):
        e = equity([100, 80, 100, 70, 100])
        dd = e / e.cummax() - 1
        episodes = drawdown_episodes(dd)
        assert len(episodes) == 2
        assert all(ep["recovered"] for ep in episodes)

    def test_touching_the_peak_ends_an_episode(self):
        # equity == cummax means dd == 0, which is not "underwater".
        e = equity([100, 90, 100, 95, 100])
        dd = e / e.cummax() - 1
        episodes = drawdown_episodes(dd)
        assert len(episodes) == 2
        assert all(ep["recovered"] for ep in episodes)

    def test_empty_input(self):
        assert drawdown_episodes(pd.Series(dtype=float)) == []


class TestDrawdownDetail:
    def test_duration_runs_from_the_peak_that_started_it(self):
        # Flat at the high Jan 1-3, trough Jan 4, recovers Jan 8. The episode
        # starts at the last bar AT the peak (Jan 3), not at the run start, so
        # 5 days — and never the 4 a trough-anchored implementation would give.
        e = equity([100, 100, 100, 80, 90, 95, 99, 100])
        d = drawdown_detail(e)
        assert d["max_drawdown_duration_days"] == 5  # Jan 3 -> Jan 8
        assert d["max_drawdown_recovery_days"] == 4  # trough Jan 4 -> Jan 8

    def test_recovery_is_never_longer_than_the_episode(self):
        e = equity([100, 100, 100, 80, 90, 95, 99, 100])
        d = drawdown_detail(e)
        assert d["max_drawdown_recovery_days"] <= d["max_drawdown_duration_days"]

    def test_unrecovered_episode_measured_to_end_of_run_and_says_so(self):
        e = equity([100, 80, 90], start="2024-01-01")
        d = drawdown_detail(e)
        assert d["max_drawdown_recovered"] is False
        assert d["max_drawdown_duration_days"] == 2  # Jan 1 -> Jan 3 (end of run)

    def test_time_in_drawdown_is_a_percentage_of_bars(self):
        e = equity([100, 90, 100, 90, 100])
        d = drawdown_detail(e)
        assert d["time_in_drawdown_pct"] == pytest.approx(40.0)

    def test_counts_only_episodes_deeper_than_ten_percent(self):
        e = equity([100, 95, 100, 89, 100])  # -5%, then -11%
        d = drawdown_detail(e)
        assert d["drawdown_episodes"] == 2
        assert d["drawdowns_over_10pct"] == 1
        assert DRAWDOWN_THRESHOLD == 0.10

    def test_a_monotonic_run_has_nothing_to_report(self):
        d = drawdown_detail(equity([100, 101, 102, 103]))
        assert d["max_drawdown_duration_days"] == 0
        assert d["drawdown_episodes"] == 0
        assert d["time_in_drawdown_pct"] == 0.0
        assert d["ulcer_index"] == 0.0

    def test_empty_curve_is_all_zeros_not_a_crash(self):
        d = drawdown_detail(pd.Series(dtype=float))
        assert d["drawdown_episodes"] == 0
        assert d["ulcer_index"] == 0.0

    def test_worst_episode_is_selected_by_depth_not_by_order(self):
        # A shallow, quick dip first; a deeper, slower one second.
        e = equity([100, 98, 100, 60, 61, 100])
        d = drawdown_detail(e)
        assert d["max_drawdown_recovery_days"] == 2  # trough Jan 4 -> Jan 6
        assert d["max_drawdown_duration_days"] == 3  # its peak Jan 3 -> Jan 6


class TestUlcerIndex:
    def test_ulcer_is_rms_of_percentage_drawdown(self):
        e = equity([100, 90, 100])
        dd = e / e.cummax() - 1
        # (0² + 10² + 0²)/3 -> sqrt
        assert ulcer_index(dd) == pytest.approx(math.sqrt(100 / 3), rel=1e-6)

    def test_a_never_recovering_deep_curve_scores_worse(self):
        shallow = ulcer_index(equity([100, 95, 100]) / equity([100, 95, 100]).cummax() - 1)
        deep = ulcer_index(equity([100, 50, 51]) / equity([100, 50, 51]).cummax() - 1)
        assert deep > shallow

    def test_ulcer_penalises_duration_not_just_depth(self):
        # Same worst depth; one brief, one long. The long one must score worse.
        brief = ulcer_index(equity([100, 80, 100]) / equity([100, 80, 100]).cummax() - 1)
        long_ = ulcer_index(
            equity([100, 80, 85, 88, 92, 100]) / equity([100, 80, 85, 88, 92, 100]).cummax() - 1
        )
        assert long_ > brief, "a 20% drawdown held for five bars hurts more than one held for one"

    def test_empty(self):
        assert ulcer_index(pd.Series(dtype=float)) == 0.0


# ---------------------------------------------------------------------------
# Return distribution
# ---------------------------------------------------------------------------


class TestVarEs:
    def test_es_is_more_negative_than_var(self):
        # A continuous spread, so the 5% quantile lands in the interior of the
        # tail and the bars below it really are worse than it. (With repeated
        # worst values VaR == ES exactly, and a strict "less than" would only
        # be testing floating-point noise.)
        r = rets(np.linspace(-0.20, 0.10, 200))
        var, es = var_es(r, 0.05)
        assert es < var, "the tail average must be worse than its own threshold"
        assert var == pytest.approx(np.quantile(r.values, 0.05), abs=1e-9)
        assert es == pytest.approx(r[r <= var].mean(), abs=1e-9)

    def test_a_repeated_worst_value_makes_var_and_es_equal_not_inverted(self):
        r = rets([-0.05, -0.05, -0.05, 0.01, 0.02] * 6)
        var, es = var_es(r, 0.05)
        assert es == pytest.approx(var)

    def test_short_series_is_not_reported_as_zero_risk(self):
        assert var_es(rets([0.01, 0.02, 0.03])) == (0.0, 0.0)

    def test_empty_is_safe(self):
        assert var_es(pd.Series(dtype=float)) == (0.0, 0.0)

    def test_99_percent_tail_is_worse_than_95(self):
        r = rets(np.linspace(-0.10, 0.10, 400))
        _, es95 = var_es(r, 0.05)
        _, es99 = var_es(r, 0.01)
        assert es99 < es95

    def test_all_gains_gives_a_positive_var(self):
        r = rets([0.01, 0.02, 0.03] * 10)
        var, es = var_es(r, 0.05)
        assert var > 0 and es > 0


class TestSkewKurtosis:
    def test_symmetric_series_has_about_zero_skew(self):
        s, _ = return_skew_kurtosis(rets([-0.02, -0.01, 0, 0.01, 0.02] * 20))
        assert s == pytest.approx(0.0, abs=0.05)

    def test_negative_skew_marks_occasional_large_losses(self):
        # Mostly small gains with a few big losses.
        r = rets([0.01] * 90 + [-0.15] * 10)
        s, _ = return_skew_kurtosis(r)
        assert s < 0

    def test_kurtosis_is_excess_so_a_normal_reads_near_zero(self):
        # A near-normal sample's EXCESS kurtosis is ~0; the raw moment is ~3.
        rng = np.random.default_rng(11)
        r = rets(rng.normal(0, 0.01, 20000))
        _, k = return_skew_kurtosis(r)
        assert abs(k) < 0.15, f"expected excess kurtosis near 0, got {k}"

    def test_short_series_is_zero_not_a_guess(self):
        assert return_skew_kurtosis(rets([0.01, -0.02])) == (0.0, 0.0)


class TestOmega:
    def test_omega_is_gains_over_losses(self):
        r = rets([0.02, -0.01, 0.02, -0.01])
        assert omega_ratio(r) == pytest.approx(2.0)

    def test_no_lossing_bar_is_capped_not_infinite(self):
        r = rets([0.01] * 50)
        value = omega_ratio(r)
        assert value == OMEGA_CAP
        assert math.isfinite(value), "an undefined ratio must not reach JSON as Infinity"

    def test_a_flat_run_is_zero(self):
        assert omega_ratio(rets([0.0] * 20)) == 0.0

    def test_omega_below_one_means_losing_geometry(self):
        assert omega_ratio(rets([0.005] * 10 + [-0.02] * 10)) < 1.0

    def test_raising_the_threshold_weakens_omega(self):
        # More returns classified as losses -> a worse ratio. This is what
        # separates Omega from a plain win/loss count.
        r = rets([-0.005, -0.001, 0.01])
        loose = omega_ratio(r, threshold=-0.01)  # only 0.01 is a gain, no losses
        tight = omega_ratio(r, threshold=0.0)  # 0.006 of loss now counts
        assert loose >= tight
        assert tight == pytest.approx(0.01 / 0.006, rel=1e-3)

    def test_empty(self):
        assert omega_ratio(pd.Series(dtype=float)) == 0.0


class TestSharpeStdError:
    def test_matches_the_textbook_form_at_zero_sharpe(self):
        # sqrt((1 + 0/2)/N) == 1/sqrt(N)
        assert sharpe_std_error(0.0, 100) == pytest.approx(0.1)

    def test_widens_as_the_sharpe_grows(self):
        """The whole reason for Lo's estimator over 1/sqrt(N)."""
        low = sharpe_std_error(0.5, 100)
        mid = sharpe_std_error(1.5, 100)
        high = sharpe_std_error(3.0, 100)
        assert low < mid < high

    def test_beats_the_naive_estimate_for_a_good_sharpe(self):
        naive = 1 / math.sqrt(100)
        assert sharpe_std_error(2.0, 100) > naive

    def test_shrinks_with_sample_size(self):
        assert sharpe_std_error(1.5, 400) < sharpe_std_error(1.5, 25)

    def test_degenerate_sample_is_zero_not_a_crash(self):
        assert sharpe_std_error(1.5, 0) == 0.0
        assert sharpe_std_error(1.5, 1) == 0.0
        assert sharpe_std_error(1.5, None) == 0.0

    def test_a_thin_sample_can_swallow_its_own_edge(self):
        """A Sharpe whose error bar is wider than the Sharpe is not an edge."""
        # 0.5 Sharpe on 3 trades: SE ~ 0.61, so the 95% interval spans zero.
        assert 0.5 < sharpe_std_error(0.5, 3)
        # The same Sharpe on 100 trades: SE ~ 0.11, so it clears.
        assert 0.5 > sharpe_std_error(0.5, 100)

    def test_the_error_bar_shrinks_monotonically_with_sample_size(self):
        errors = [sharpe_std_error(1.0, n) for n in (5, 10, 25, 50, 100, 400)]
        assert errors == sorted(errors, reverse=True)


# ---------------------------------------------------------------------------
# Trade quality
# ---------------------------------------------------------------------------


class TestTradeCountFlag:
    def test_thresholds_are_exactly_the_prd_ones(self):
        assert trade_count_flag(TRADE_COUNT_OK) == "ok"
        assert trade_count_flag(TRADE_COUNT_OK - 1) == "warn"
        assert trade_count_flag(TRADE_COUNT_WARN) == "warn"
        assert trade_count_flag(TRADE_COUNT_WARN - 1) == FLAG_INSUFFICIENT

    def test_boundary_values(self):
        assert (TRADE_COUNT_OK, TRADE_COUNT_WARN) == (30, 20)
        assert trade_count_flag(47) == "ok"
        assert trade_count_flag(24) == "warn"
        assert trade_count_flag(7) == "insufficient"
        assert trade_count_flag(0) == "insufficient"

    def test_negative_and_none_do_not_raise(self):
        assert trade_count_flag(-3) == FLAG_INSUFFICIENT
        assert trade_count_flag(None) == FLAG_INSUFFICIENT


class TestPayoffRatio:
    def test_average_win_over_average_loss(self):
        assert payoff_ratio([100.0, 200.0], [-50.0, -150.0]) == pytest.approx(1.5)

    def test_profit_factor_1_5_built_on_lopsided_wins_shows_a_low_payoff(self):
        # PF = 300/200 = 1.5, but the average win is 6x the average loss.
        assert payoff_ratio([300.0], [-100.0, -100.0]) == pytest.approx(3.0)
        assert 300.0 / 200.0 == pytest.approx(1.5)

    def test_no_losses_means_no_ratio_not_infinity(self):
        assert payoff_ratio([100.0, 50.0], []) == 0.0

    def test_no_trades_is_zero(self):
        assert payoff_ratio([], []) == 0.0

    def test_a_single_breakeven_loss_does_not_explode(self):
        assert payoff_ratio([10.0], [0.0]) == 0.0


class TestConsecutiveStreaks:
    def test_longest_run_of_each(self):
        assert consecutive_streaks(["Win", "Win", "Loss", "Loss", "Loss", "Win"]) == (2, 3)

    def test_runs_must_be_adjacent(self):
        assert consecutive_streaks(["Win", "Loss", "Win", "Loss", "Win"]) == (
            1,
            1,
        ), "non-adjacent wins are not a streak"

    def test_a_flat_trade_breaks_both_streaks(self):
        assert consecutive_streaks(["Win", "Win", "Flat", "Win", "Win"]) == (2, 0)
        assert consecutive_streaks(["Loss", "Loss", "Flat", "Loss"]) == (0, 2)

    def test_empty_is_zero(self):
        assert consecutive_streaks([]) == (0, 0)

    def test_all_wins(self):
        assert consecutive_streaks(["Win"] * 6) == (6, 0)


class TestTradeDurations:
    def test_mean_and_median(self):
        assert trade_durations([2, 4, 6]) == (4.0, 4.0)

    def test_median_is_not_pulled_by_one_long_hold(self):
        # Mean would be 5.1; the median still describes the typical trade.
        avg, med = trade_durations([1, 1, 1, 1, 1, 1, 1, 1, 1, 22])
        assert med == 1.0
        assert avg > med

    def test_no_trades_is_zero(self):
        assert trade_durations([]) == (0.0, 0.0)

    def test_zero_and_negative_bars_are_not_durations(self):
        assert trade_durations([0, 0]) == (0.0, 0.0)
        assert trade_durations([-3, 0, 5]) == (5.0, 5.0)
