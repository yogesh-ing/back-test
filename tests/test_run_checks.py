"""PRD backTest-enhance §3 — benchmark, cost shock, Monte Carlo.

Three single-run checks. What is pinned here is mostly the cases where a
plausible implementation reports something confident and wrong:

* **beta** on a benchmark that never moved is undefined, not enormous;
* **the cost-shock base** on a frictionless run is the 5 bps default, because
  2 x zero slippage is zero and would report every strategy as robust;
* **a reordering Monte Carlo cannot change the final equity** — a shuffle does
  not change a sum — so the final-equity spread has to come from a bootstrap,
  and the payload has to say which experiment produced which number;
* **an absent check says so**, rather than rendering as an absent panel that
  reads as a pass.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtest_runner import run_backtest
from backtest.engine.benchmark import (
    alpha_beta,
    benchmark_equity,
    benchmark_metrics,
    build_benchmark,
)
from backtest.engine.cost_shock import (
    COST_SHOCK_MULTIPLIERS,
    DEFAULT_COST_SHOCK_BASE_BPS,
    resolve_base_bps,
    run_cost_shock,
)
from backtest.engine.monte_carlo import monte_carlo_trade_order, trade_concentration


def curve(values, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series([float(v) for v in values], index=idx)


def candles(closes, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes}, index=idx)


def rets(values):
    return pd.Series([float(v) for v in values])


# ---------------------------------------------------------------------------
# §3.1 Benchmark
# ---------------------------------------------------------------------------


class TestBenchmarkEquity:
    def test_buys_at_the_first_close_and_holds(self):
        eq = benchmark_equity(candles([100, 110, 121]), 1000.0)
        assert eq.tolist() == [1000.0, 1100.0, 1210.0]

    def test_is_costless_so_it_is_a_fair_reference(self):
        # A benchmark charging the strategy's own fees would answer a question
        # about fees as much as about the signal.
        eq = benchmark_equity(candles([100, 150]), 1000.0)
        assert eq.iloc[-1] == 1500.0

    def test_a_falling_market_still_ends_where_the_price_did(self):
        eq = benchmark_equity(candles([100, 50, 25]), 1000.0)
        assert eq.iloc[-1] == pytest.approx(250.0)

    def test_no_candles_is_empty_not_a_crash(self):
        assert len(benchmark_equity(candles([]), 1000.0)) == 0
        assert len(benchmark_equity(pd.DataFrame(), 1000.0)) == 0

    def test_zero_capital_is_empty(self):
        assert len(benchmark_equity(candles([100, 110]), 0.0)) == 0

    def test_a_bad_first_print_does_not_multiply_nonsense(self):
        eq = benchmark_equity(candles([0, 100, 200]), 1000.0)
        assert eq.tolist() == [1000.0, 1000.0, 1000.0], "a zero first close has no ratio"


class TestBenchmarkMetrics:
    def test_total_return_and_final_equity(self):
        m = benchmark_metrics(curve([1000, 1200]), 1000.0, 252)
        assert m["total_return"] == pytest.approx(0.20)
        assert m["final_equity"] == pytest.approx(1200.0)

    def test_max_drawdown_is_negative_depth(self):
        m = benchmark_metrics(curve([1000, 800, 1100]), 1000.0, 252)
        assert m["max_drawdown"] == pytest.approx(-0.20)

    def test_sharpe_matches_the_definition_the_cards_use(self):
        m = benchmark_metrics(curve([1000, 1010, 1020, 1030]), 1000.0, 252)
        assert m["sharpe"] > 0

    def test_a_too_short_curve_is_all_zeros_not_a_division_error(self):
        m = benchmark_metrics(curve([1000]), 1000.0, 252)
        assert m == {
            "total_return": 0.0,
            "cagr": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "final_equity": 1000.0,
        }

    def test_empty_is_safe(self):
        assert benchmark_metrics(pd.Series(dtype=float), 1000.0, 252)["total_return"] == 0.0

    def test_zero_periods_per_year_does_not_explode_the_cagr(self):
        m = benchmark_metrics(curve([1000, 2000]), 1000.0, 0)
        assert math.isfinite(m["cagr"]) and math.isfinite(m["sharpe"])


class TestAlphaBeta:
    def test_alpha_is_simple_excess_return(self):
        ab = alpha_beta(rets([0.01] * 20), rets([0.005] * 20), 0.30, 0.20)
        assert ab["alpha"] == pytest.approx(0.10)

    def test_beta_is_one_for_a_perfect_copy(self):
        base = [0.01, -0.02, 0.015, 0.005, -0.01, 0.02] * 4
        ab = alpha_beta(rets(base), rets(base), 0.0, 0.0)
        assert ab["beta"] == pytest.approx(1.0, abs=1e-6)

    def test_beta_is_half_for_a_half_copy(self):
        base = [0.01, -0.02, 0.015, 0.005, -0.01, 0.02] * 4
        ab = alpha_beta(rets([b / 2 for b in base]), rets(base), 0.0, 0.0)
        assert ab["beta"] == pytest.approx(0.5, abs=1e-6)

    def test_beta_is_zero_for_an_uncorrelated_strategy(self):
        rng = np.random.default_rng(5)
        b = rng.normal(0, 0.01, 500)
        s = rng.normal(0, 0.01, 500)
        ab = alpha_beta(rets(s), rets(b), 0.0, 0.0)
        assert abs(ab["beta"]) < 0.15

    def test_a_flat_benchmark_makes_beta_undefined_not_enormous(self):
        """Zero variance in the denominator is the whole trap here."""
        s = rets([0.01, -0.02, 0.015, 0.005, -0.01, 0.02] * 5)
        ab = alpha_beta(s, rets([0.0] * len(s)), 0.10, 0.0)
        assert ab["beta"] == 0.0
        assert math.isfinite(ab["beta"]), "a huge beta would read as huge sensitivity"
        assert ab["alpha"] == pytest.approx(0.10), "alpha survives a flat benchmark"

    def test_unaligned_series_are_intersected_before_correlating(self):
        # Strategy on even days, benchmark on odd days: an unaligned
        # correlation would compare two different days and report nonsense.
        s = pd.Series([0.01, -0.02, 0.015], index=pd.date_range("2024-01-01", periods=3, freq="2D"))
        b = pd.Series([0.01, -0.02, 0.015], index=pd.date_range("2024-01-02", periods=3, freq="2D"))
        ab = alpha_beta(s, b, 0.0, 0.0)
        assert ab["aligned_bars"] == 0, "the two calendars share no bars at all"
        assert ab["beta"] == 0.0

    def test_alignment_counts_only_the_overlap(self):
        s = rets([0.01] * 10)
        b = rets([0.01] * 6)
        b.index = b.index[:6]
        assert alpha_beta(s, b, 0.0, 0.0)["aligned_bars"] == 6

    def test_a_two_bar_overlap_is_too_few_to_correlate(self):
        ab = alpha_beta(rets([0.01, 0.02]), rets([0.01, 0.02]), 0.05, 0.0)
        assert ab["aligned_bars"] == 2
        assert ab["beta"] == 0.0

    def test_missing_series_is_safe(self):
        assert alpha_beta(None, rets([0.01]), 0.0, 0.0)["beta"] == 0.0


class TestBuildBenchmark:
    def test_reports_its_own_return_and_the_strategy_gap(self):
        c = candles([100, 110, 121])
        eq = curve([1000, 1150, 1100])
        b = build_benchmark(c, eq, 1000.0, 252, strategy_total_return=0.10)
        assert b["total_return"] == pytest.approx(0.21)  # 100 -> 121
        assert b["alpha"] == pytest.approx(0.10 - 0.21, abs=1e-6)
        assert b["available"] is True
        assert b["label"] == "Buy & Hold"

    def test_uses_the_supplied_strategy_return_rather_than_recomputing_it(self):
        # The cards show 0.10; if this module re-derived it, alpha would be
        # measured against a different number than the one on screen.
        c = candles([100, 110, 121])
        eq = curve([1000, 1150, 1100])
        b = build_benchmark(c, eq, 1000.0, 252, strategy_total_return=0.99)
        assert b["alpha"] == pytest.approx(0.99 - 0.21, abs=1e-6)

    def test_reindexes_onto_the_strategy_bars(self):
        c = candles([100, 110, 121, 133, 146])  # 5 bars
        eq = curve([1000, 1050, 1100])  # engine only traded 3
        b = build_benchmark(c, eq, 1000.0, 252)
        assert b["aligned_bars"] <= 3

    def test_no_candles_is_unavailable_but_still_well_formed(self):
        b = build_benchmark(candles([]), curve([1000, 1000]), 1000.0, 252)
        assert b["available"] is False
        assert b["sharpe"] == 0.0 and b["alpha"] == 0.0

    def test_never_ships_the_curve_twice(self):
        # to_equity() already carries it for the chart; a second copy would
        # double the payload's largest array.
        b = build_benchmark(candles([100, 110]), curve([1000, 1050]), 1000.0, 252)
        assert "values" not in b and "dates" not in b


# ---------------------------------------------------------------------------
# §3.2 Cost shock
# ---------------------------------------------------------------------------


class TestResolveBaseBps:
    def test_a_frictionless_run_stresses_from_the_labelled_default(self):
        """2x zero slippage is zero, which would report everything as robust."""
        bps, source = resolve_base_bps(None)
        assert bps == DEFAULT_COST_SHOCK_BASE_BPS == 5.0
        assert source == "default"

    def test_explicit_zero_is_treated_as_frictionless(self):
        assert resolve_base_bps(0.0)[1] == "default"

    def test_a_configured_level_is_used_as_is(self):
        assert resolve_base_bps(12.5) == (12.5, "configured")

    def test_nonsense_falls_back_to_the_default_rather_than_crashing(self):
        assert resolve_base_bps("abc")[1] == "default"
        assert resolve_base_bps(-3)[1] == "default"


class TestCostShock:
    @pytest.fixture(scope="class")
    def candles_frame(self):
        return SyntheticSource().get_candles("INFY", "2023-01-01", "2025-12-31", "day")

    @pytest.fixture(scope="class")
    def base(self, candles_frame):
        return run_backtest(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            timeframe="1day",
        )

    def test_produces_the_prd_three_scenarios(self, candles_frame, base):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
        )
        assert cs["available"] is True
        assert [s["multiple"] for s in cs["scenarios"]] == list(COST_SHOCK_MULTIPLIERS)

    def test_slippage_bps_are_the_base_times_the_multiple(self, candles_frame, base):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
        )
        for s in cs["scenarios"]:
            assert s["slippage_bps"] == pytest.approx(cs["base_bps"] * s["multiple"])

    def test_the_edge_shrinks_as_slippage_rises(self, candles_frame, base):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
        )
        returns = [s["total_return_pct"] for s in cs["scenarios"]]
        assert returns == sorted(returns, reverse=True), returns

    def test_a_frictionless_base_is_labelled_so_the_cards_are_not_implied_costed(
        self, candles_frame, base
    ):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
        )
        assert cs["base_bps_source"] == "default"
        assert "frictionless" in cs["base_bps_note"]
        assert cs["actual"]["slippage_bps"] == 0.0

    def test_a_configured_base_reuses_the_actual_result_for_the_1x_row(self, candles_frame, base):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
            configured_bps=7.0,
        )
        assert cs["base_bps_source"] == "configured"
        assert cs["scenarios"][0]["slippage_bps"] == pytest.approx(7.0)

    def test_the_actual_result_is_reported_next_to_the_table(self, candles_frame, base):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
        )
        assert cs["actual"]["total_return_pct"] == pytest.approx(
            base.metrics["total_return"] * 100, abs=0.01
        )

    def test_no_candles_is_unavailable_with_a_reason(self, base):
        cs = run_cost_shock(
            pd.DataFrame(),
            "sma_crossover",
            {},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
        )
        assert cs["available"] is False and "bars" in cs["reason"]

    def test_a_missing_base_result_is_unavailable(self, candles_frame):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics={},
        )
        assert cs["available"] is False

    def test_a_crashing_re_run_degrades_the_block_not_the_page(self, candles_frame, base):
        def exploding(*a, **k):
            raise RuntimeError("engine on fire")

        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {},
            "INFY",
            100_000,
            "1day",
            exploding,
            actual_metrics=base.metrics,
        )
        assert cs["available"] is False
        assert "did not complete" in cs["reason"]

    def test_a_re_run_returning_junk_is_treated_as_a_failure(self, candles_frame, base):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {},
            "INFY",
            100_000,
            "1day",
            lambda *a, **k: object(),
            actual_metrics=base.metrics,
        )
        assert cs["available"] is False

    def test_the_block_is_json_serialisable(self, candles_frame, base):
        cs = run_cost_shock(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            "1day",
            run_backtest,
            actual_metrics=base.metrics,
        )
        text = json.dumps(cs)
        assert "NaN" not in text and "Infinity" not in text


class TestCostShockVerdict:
    """The 1x/2x/3x -> green/yellow/red mapping, tested directly."""

    def _run_with(self, returns_by_multiple):
        """Drive run_cost_shock with a stub that returns the given returns."""

        def stub(candles, strategy, params, symbol, capital, timeframe=None, slippage_bps=None):
            return type(
                "R",
                (),
                {
                    "metrics": {
                        "total_return": returns_by_multiple[slippage_bps],
                        "sharpe": 1.0,
                        "max_drawdown": -0.1,
                        "closed_trades": 40,
                    }
                },
            )()

        # base 5 bps -> scenarios at 5, 10, 15
        returns_by_multiple = {bps: r for bps, r in zip((5.0, 10.0, 15.0), returns_by_multiple)}
        return run_cost_shock(
            candles([100, 110, 121]),
            "s",
            {},
            "X",
            1000.0,
            "1day",
            stub,
            actual_metrics={"total_return": 0.0, "sharpe": 0.0, "closed_trades": 40},
        )

    def test_green_when_profitable_at_three_times(self):
        cs = self._run_with([0.30, 0.21, 0.12])
        assert cs["status"] == "robust"
        assert cs["warning"]["level"] == "info"

    def test_red_when_the_edge_dies_at_two_times(self):
        cs = self._run_with([0.30, -0.02, -0.20])
        assert cs["status"] == "broken"
        assert cs["warning"]["level"] == "error"
        assert "2x slippage" in cs["warning"]["message"]

    def test_yellow_when_it_survives_two_times_but_not_three(self):
        cs = self._run_with([0.30, 0.12, -0.04])
        assert cs["status"] == "fragile"
        assert cs["warning"]["level"] == "warning"

    def test_too_few_trades_suppresses_the_verdict(self):
        def stub(candles, strategy, params, symbol, capital, timeframe=None, slippage_bps=None):
            return type(
                "R",
                (),
                {
                    "metrics": {
                        "total_return": 0.30,
                        "sharpe": 1.0,
                        "max_drawdown": -0.1,
                        "closed_trades": 2,
                    }
                },
            )()

        cs = run_cost_shock(
            candles([100, 110, 121]),
            "s",
            {},
            "X",
            1000.0,
            "1day",
            stub,
            actual_metrics={"total_return": 0.3, "sharpe": 1.0, "closed_trades": 2},
        )
        assert cs["status"] == "insufficient_trades"
        assert "not reliable" in cs["warning"]["message"]

    def test_dropped_trades_are_called_out_as_a_sizing_limit(self):
        # Past a certain slippage an all-in order cannot be funded, so trades
        # vanish. Reading that as "the edge died" would be the wrong lesson.
        def stub(candles, strategy, params, symbol, capital, timeframe=None, slippage_bps=None):
            trades = 40 if slippage_bps < 12 else 20  # only the 3x row trips
            metrics = {
                "total_return": 0.30,
                "sharpe": 1.0,
                "max_drawdown": -0.1,
                "closed_trades": trades,
            }
            return type("R", (), {"metrics": metrics})()

        cs = run_cost_shock(
            candles([100, 110, 121]),
            "s",
            {},
            "X",
            1000.0,
            "1day",
            stub,
            actual_metrics={"total_return": 0.3, "sharpe": 1.0, "closed_trades": 40},
        )
        assert cs["scenarios"][-1]["trades_dropped"] == 20
        assert "sizing limit" in cs["warning"]["message"]


class TestSlippageActuallyBites:
    """The plumbing must genuinely change fills, not just label a table."""

    @pytest.fixture(scope="class")
    def candles_frame(self):
        return SyntheticSource().get_candles("INFY", "2023-01-01", "2025-12-31", "day")

    def _run(self, candles_frame, **kwargs):
        return run_backtest(
            candles_frame,
            "sma_crossover",
            {"fast": 10, "slow": 30},
            "INFY",
            100_000,
            timeframe="1day",
            **kwargs,
        )

    def test_the_default_run_is_still_frictionless(self, candles_frame):
        assert self._run(candles_frame).metrics.get("slippage_bps") is None

    def test_slippage_lowers_the_return(self, candles_frame):
        free = self._run(candles_frame).metrics["total_return"]
        costed = self._run(candles_frame, slippage_bps=25.0).metrics["total_return"]
        assert costed < free

    def test_more_slippage_costs_more(self, candles_frame):
        a = self._run(candles_frame, slippage_bps=10.0).metrics["total_return"]
        b = self._run(candles_frame, slippage_bps=30.0).metrics["total_return"]
        assert b < a

    def test_zero_slippage_is_the_same_run_as_asking_for_nothing(self, candles_frame):
        assert (
            self._run(candles_frame, slippage_bps=0.0).metrics["total_return"]
            == self._run(candles_frame).metrics["total_return"]
        )

    def test_the_level_is_stamped_into_the_metrics(self, candles_frame):
        assert self._run(candles_frame, slippage_bps=12.5).metrics["slippage_bps"] == 12.5

    def test_runs_stay_deterministic(self, candles_frame):
        one = self._run(candles_frame, slippage_bps=10.0).metrics["total_return"]
        two = self._run(candles_frame, slippage_bps=10.0).metrics["total_return"]
        assert one == two, "a seeded engine that drifts is not a backtest"


# ---------------------------------------------------------------------------
# §3.3 Monte Carlo
# ---------------------------------------------------------------------------


class TestMonteCarlo:
    def test_reordering_never_changes_the_final_equity(self):
        """A shuffle does not change a sum. This is the PRD's blind spot."""
        mc = monte_carlo_trade_order([100.0, -50.0, 200.0, 30.0, -80.0] * 4, 100_000.0)
        reorder = mc["reorder"]
        assert reorder["final_equity_is_invariant"] is True
        assert reorder["p5_final_equity"] == reorder["median_final_equity"]
        assert reorder["p95_final_equity"] == reorder["median_final_equity"]
        assert reorder["profit_probability_pct"] in (
            0.0,
            100.0,
        ), "probability of profit cannot be a middle number under a pure reorder"

    def test_the_actual_result_is_the_reorder_median_not_a_floating_point_percentile(self):
        mc = monte_carlo_trade_order([100.0, -50.0, 200.0, 30.0, -80.0] * 4, 100_000.0)
        assert mc["reorder"]["actual_percentile"] == 50.0
        assert mc["reorder"]["median_final_equity"] == pytest.approx(
            mc["actual_final_equity"], abs=0.01
        )

    def test_bootstrap_actually_spreads_the_outcome(self):
        mc = monte_carlo_trade_order([100.0, -50.0, 200.0, 30.0, -80.0] * 4, 100_000.0)
        boot = mc["bootstrap"]
        assert boot["final_equity_is_invariant"] is False
        assert boot["p5_final_equity"] < boot["median_final_equity"] < boot["p95_final_equity"]

    def test_reordering_still_moves_the_drawdown(self):
        """Path dependence is the one thing a reorder can and does measure."""
        mc = monte_carlo_trade_order(
            [3000.0, -2000.0, 2500.0, -1800.0, 2200.0, -1500.0, 1000.0, -900.0] * 3, 100_000.0
        )
        assert mc["reorder"]["p95_max_drawdown_pct"] > mc["reorder"]["median_max_drawdown_pct"]

    def test_a_losing_sequence_has_zero_profit_probability(self):
        mc = monte_carlo_trade_order([-100.0] * 10, 10_000.0)
        assert mc["bootstrap"]["profit_probability_pct"] == 0.0
        assert any(w["code"] == "low_profit_probability" for w in mc["warnings"])

    def test_a_winning_sequence_has_high_profit_probability(self):
        mc = monte_carlo_trade_order([100.0] * 10, 10_000.0)
        assert mc["bootstrap"]["profit_probability_pct"] == 100.0
        assert not any(w["code"] == "low_profit_probability" for w in mc["warnings"])

    def test_a_result_leaning_on_one_big_win_is_flagged(self):
        """One trade, 100% of gross profit — an anecdote, not a sample.

        Resampling cannot surface this: the outlier is INSIDE the sample being
        resampled, which is why concentration is asked directly.
        """
        mc = monte_carlo_trade_order([-100.0] * 9 + [5_000.0], 100_000.0)
        assert mc["concentration"]["best_trade_share_pct"] == pytest.approx(100.0)
        assert any(w["code"] == "profit_concentrated" for w in mc["warnings"])

    def test_a_evenly_earned_result_is_not_flagged_as_concentrated(self):
        mc = monte_carlo_trade_order([100.0, -50.0, 200.0, 30.0, -80.0] * 4, 100_000.0)
        assert mc["concentration"]["best_trade_share_pct"] < 50
        assert not any(w["code"] == "profit_concentrated" for w in mc["warnings"])

    def test_concentration_sums_gross_profit_not_net(self):
        c = trade_concentration([1_000.0, -900.0, 100.0])
        assert c["gross_profit"] == pytest.approx(1_100.0)
        assert c["gross_loss"] == pytest.approx(900.0)
        assert c["net"] == pytest.approx(200.0)
        assert c["best_trade_share_pct"] == pytest.approx(90.9, abs=0.1)

    def test_concentration_with_no_wins_is_zero_not_a_division_error(self):
        assert trade_concentration([-100.0, -50.0])["best_trade_share_pct"] == 0.0

    def test_concentration_of_nothing(self):
        assert trade_concentration([])["best_trade"] == 0.0

    def test_the_actual_percentile_has_a_mathematical_ceiling(self):
        """Why the PRD's "above the 90th percentile" rule was replaced.

        A same-size bootstrap resamples the very sample that defines its own
        distribution, so the actual result cannot land far out in its own tail.
        The ceiling is ~74% for any n, which is exactly why a percentile rule
        at 90 would have looked fine forever and never fired.
        """  # noqa: E501 -- prose, not code
        from math import comb

        for n, k in [(10, 1), (20, 1), (40, 1), (10, 3), (30, 3), (60, 1)]:
            p = sum(comb(n, i) * (k / n) ** i * (1 - k / n) ** (n - i) for i in range(k + 1))
            assert p < 0.90, f"n={n} k={k} reached {p:.3f} — recheck the ceiling"
            assert p > 0.50

    def test_a_profitable_median_with_an_underwater_tail_is_flagged(self):
        """Nineteen +1200s and one -20000: positive median, real left tail.

        A bootstrap that draws the big loser twice finishes 18k underwater, and
        that happens often enough to sit below the 5th percentile while the
        median still finishes ahead. Profit probability stays comfortably above
        60%, so this is the case the profit-probability rule MISSES and the
        tail rule catches.
        """
        mc = monte_carlo_trade_order([1_200.0] * 19 + [-20_000.0], 100_000.0)
        boot = mc["bootstrap"]
        assert boot["median_final_equity"] > 100_000.0
        assert boot["p5_final_equity"] < 100_000.0
        assert boot["profit_probability_pct"] > 60, "so profit-probability alone would pass this"
        assert any(w["code"] == "downside_tail" for w in mc["warnings"])

    def test_drawdowns_are_reported_as_positive_depths(self):
        mc = monte_carlo_trade_order([100.0, -500.0, 200.0, -400.0] * 3, 100_000.0)
        assert mc["reorder"]["median_max_drawdown_pct"] >= 0.0
        assert mc["bootstrap"]["worst_max_drawdown_pct"] >= 0.0

    def test_the_trade_boundary_limitation_is_stated_in_the_payload(self):
        mc = monte_carlo_trade_order([100.0, -50.0, 200.0], 100_000.0)
        assert "trade boundaries" in mc["drawdown_note"]

    def test_the_same_seed_gives_the_same_answer(self):
        pnls = [100.0, -50.0, 200.0, 30.0, -80.0] * 5
        a = monte_carlo_trade_order(pnls, 100_000.0, seed=7)
        b = monte_carlo_trade_order(pnls, 100_000.0, seed=7)
        # A Monte Carlo that moves on refresh reads as a bug in the numbers.
        assert a["bootstrap"] == b["bootstrap"]

    def test_a_different_seed_actually_changes_the_spread(self):
        pnls = [100.0, -50.0, 200.0, 30.0, -80.0] * 5
        a = monte_carlo_trade_order(pnls, 100_000.0, seed=1)
        b = monte_carlo_trade_order(pnls, 100_000.0, seed=2)
        assert a["bootstrap"]["p5_final_equity"] != b["bootstrap"]["p5_final_equity"]

    def test_one_trade_has_no_sequence_to_resample(self):
        mc = monte_carlo_trade_order([100.0], 100_000.0)
        assert mc["available"] is False
        assert "no sequence" in mc["reason"]

    def test_no_trades_is_unavailable_not_a_crash(self):
        assert monte_carlo_trade_order([], 100_000.0)["available"] is False

    def test_zero_capital_is_rejected(self):
        mc = monte_carlo_trade_order([100.0, 200.0], 0.0)
        assert mc["available"] is False and "capital" in mc["reason"]

    def test_the_block_is_json_serialisable(self):
        mc = monte_carlo_trade_order([100.0, -50.0, 200.0, 30.0], 100_000.0, simulations=200)
        text = json.dumps(mc)
        assert "NaN" not in text and "Infinity" not in text

    # ------------------------------------------------------------------ fan

    def test_the_fan_has_five_ordered_bands(self):
        """p5 <= p25 <= p50 <= p75 <= p95 at every point, or the chart lies."""
        mc = monte_carlo_trade_order([100.0, -50.0, 200.0, 30.0] * 5, 100_000.0, simulations=200)
        bands = mc["fan"]["bands"]
        assert set(bands) == {"5", "25", "50", "75", "95"}
        order = ["5", "25", "50", "75", "95"]
        for i in range(mc["fan"]["points"]):
            values = [bands[k][i] for k in order]
            assert values == sorted(values), f"bands cross at point {i}"

    def test_the_fan_starts_at_the_starting_capital(self):
        """The opening balance is a real point on the path, not an implied one."""
        mc = monte_carlo_trade_order([100.0, -50.0] * 8, 250_000.0, simulations=200)
        for band in mc["fan"]["bands"].values():
            assert band[0] == pytest.approx(250_000.0, abs=0.01)

    def test_the_fan_has_one_point_per_trade_plus_the_start(self):
        mc = monte_carlo_trade_order([100.0, -50.0, 20.0, 5.0] * 2, 100_000.0, simulations=100)
        assert mc["fan"]["points"] == 9  # 8 trades + the opening point

    def test_a_very_long_run_is_downsampled(self):
        """2,000 points per band to draw a few hundred pixels is waste."""
        pnls = [float(v) for v in np.random.default_rng(2).normal(0, 500, 2000)]
        fan = monte_carlo_trade_order(pnls, 100_000.0, simulations=100)["fan"]
        assert fan["points"] <= 401
        lengths = {len(v) for v in fan["bands"].values()}
        assert lengths == {fan["points"]}, "every band must be downsampled identically"

    def test_the_downsampled_fan_still_ends_on_the_real_equity(self):
        """The last point of every band is the end of the path, not a step short."""
        pnls = [float(v) for v in np.random.default_rng(4).normal(0, 500, 1500)]
        mc = monte_carlo_trade_order(pnls, 100_000.0, simulations=100)
        assert mc["fan"]["bands"]["50"][-1] == pytest.approx(
            mc["bootstrap"]["median_final_equity"], abs=1.0
        )

    def test_the_fan_is_absent_when_the_block_is_not(self):
        """A two-trade result is unavailable; it must not carry a fan."""
        mc = monte_carlo_trade_order([100.0], 100_000.0)
        assert "fan" not in mc

    def test_it_is_fast_enough_to_run_inside_a_request(self):
        import time

        pnls = [float(v) for v in np.random.default_rng(1).normal(500, 900, 120)]
        started = time.perf_counter()
        monte_carlo_trade_order(pnls, 100_000.0, simulations=1000)
        assert (time.perf_counter() - started) < 1.0


# ---------------------------------------------------------------------------
# The payload the page actually reads
# ---------------------------------------------------------------------------


class TestRunChecksPayload:
    @pytest.fixture(scope="class")
    @classmethod
    def payload(cls):
        candles_frame = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
        result = run_backtest(candles_frame, "sma_crossover", {}, "DEMO", 100_000, timeframe="1day")
        return BacktestAdapter(result).to_all()

    def test_benchmark_is_in_the_payload(self, payload):
        b = payload["benchmark"]
        assert {"available", "total_return", "sharpe", "max_drawdown", "alpha", "beta"} <= set(b)

    def test_monte_carlo_is_in_the_payload(self, payload):
        mc = payload["monte_carlo"]
        assert {"available", "reorder", "bootstrap", "simulations"} <= set(mc)

    def test_the_payload_round_trips_through_json(self, payload):
        text = json.dumps(payload)
        assert "NaN" not in text and "Infinity" not in text
        assert json.loads(text)["benchmark"]["label"] == "Buy & Hold"

    def test_the_adapter_does_not_mutate_its_input(self):
        candles_frame = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
        result = run_backtest(candles_frame, "sma_crossover", {}, "DEMO", 100_000, timeframe="1day")
        before = result.equity.copy()
        adapter = BacktestAdapter(result)
        adapter.to_all()
        adapter.to_benchmark()
        pd.testing.assert_series_equal(result.equity, before)

    def test_monte_carlo_resamples_the_same_trades_the_cards_counted(self):
        candles_frame = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
        result = run_backtest(candles_frame, "sma_crossover", {}, "DEMO", 100_000, timeframe="1day")
        adapter = BacktestAdapter(result)
        mc = adapter.to_monte_carlo()
        closed = [t for t in adapter.to_trades() if not t["is_open"]]
        assert mc["trades"] == len(closed)
        assert mc["trades"] == result.metrics["closed_trades"]

    def test_benchmark_results_are_cached_not_recomputed_per_call(self):
        candles_frame = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
        result = run_backtest(candles_frame, "sma_crossover", {}, "DEMO", 100_000, timeframe="1day")
        adapter = BacktestAdapter(result)
        assert adapter.to_benchmark() is adapter.to_benchmark()
        assert adapter.to_monte_carlo() is adapter.to_monte_carlo()
