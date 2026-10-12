"""PRD backTest-enhance §2 — the richer metrics, end to end.

`tests/test_metrics_risk.py` pins each estimator on its own. What is pinned
HERE is the wiring, which is where a §2 slice usually breaks:

* ``compute_metrics`` still returns every key it returned before, so the
  Compare/Optimize/Forward surfaces that already read them are untouched;
* the new keys use the PRD's exact vocabulary (``omega``, ``cvar_95``,
  ``max_drawdown_duration_days``, …) rather than a private naming scheme;
* ``BacktestAdapter.to_all()`` — the JSON the page actually renders — carries
  all of them, with ``trade_count_flag`` surviving as a STRING so the UI can
  tell `warn` from `insufficient`;
* the durations are measured on real bar counts, not inferred from exposure,
  which is the whole reason ``Trade.bars_held`` exists.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig
from backtest.engine.metrics import compute_metrics
from backtest.engine.metrics_risk import FLAG_INSUFFICIENT, TRADE_COUNT_OK, TRADE_COUNT_WARN
from backtest.engine.trades import walk_trades
from backtest.runner import run_on_candles

# Keys that shipped before §2. Losing any of these silently breaks a page that
# is not in this slice.
PRE_EXISTING_KEYS = {
    "total_return",
    "cagr",
    "volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "var_95",
    "es_95",
    "var_99",
    "es_99",
    "num_trades",
    "closed_trades",
    "open_trades",
    "winning_trades",
    "losing_trades",
    "win_rate",
    "realised_pnl",
    "avg_trade_pnl",
    "best_trade_pnl",
    "worst_trade_pnl",
    "profit_factor",
    "expectancy",
    "gross_profit",
    "gross_loss",
    "max_consecutive_losses",
    "exposure",
    "avg_holding_bars",
    "final_equity",
    "bars",
}

# The PRD §2.1 table, verbatim.
PRD_KEYS = {
    "sortino",
    "omega",
    "skewness",
    "kurtosis",
    "var_95",
    "cvar_95",
    "ulcer_index",
    "max_drawdown_duration_days",
    "max_drawdown_recovery_days",
    "time_in_drawdown_pct",
    "drawdowns_over_10pct",
    "profit_factor",
    "expectancy_inr",
    "payoff_ratio",
    "max_consecutive_wins",
    "max_consecutive_losses",
    "avg_trade_duration_bars",
    "median_trade_duration_bars",
    "trade_count_flag",
    "sharpe_std_error",
}


def _metrics_from(equity_values, position_values, *, capital=100_000.0, periods_per_year=252):
    """Drive `compute_metrics` with a hand-built curve — no strategy needed."""
    idx = pd.bdate_range("2023-01-02", periods=len(equity_values))
    equity = pd.Series([float(v) for v in equity_values], index=idx)
    position = pd.Series([float(v) for v in position_values], index=idx)

    class _Result:
        pass

    result = _Result()
    result.equity = equity
    result.returns = equity.pct_change().fillna(0.0)
    result.position = position
    config = BacktestConfig(initial_capital=capital)
    config.periods_per_year = periods_per_year
    result.config = config
    return compute_metrics(result), equity, position


def _build_runs(lengths, *, step=0.001, capital=100_000.0):
    """Equity/position for consecutive trades held for `lengths` bars each."""
    equity, position = [capital], [0.0]
    for length in lengths:
        for _ in range(length):
            equity.append(equity[-1] * (1 + step))
            position.append(1.0)
        equity.append(equity[-1])
        position.append(0.0)
    return _metrics_from(equity, position, capital=capital)


def _sawtooth(n_bars, n_trades, *, gain=0.01, loss=-0.005, capital=100_000.0):
    """A curve that closes exactly `n_trades` trades, in alternating runs."""
    equity, position = [capital], [0.0]
    if n_trades <= 0:
        equity.extend([capital] * 10)
        position.extend([0.0] * 10)
        return _metrics_from(equity, position, capital=capital)
    bars = max(2, n_bars // n_trades)
    for i in range(n_trades):
        step = gain if i % 2 == 0 else loss
        for b in range(bars):
            equity.append(equity[-1] * (1 + step / bars))
            position.append(1.0)
        equity.append(equity[-1])
        position.append(0.0)
    return _metrics_from(equity, position, capital=capital)


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------


class TestBackwardsCompatibility:
    def test_no_pre_existing_metric_disappeared(self):
        m, _, _ = _sawtooth(400, 10)
        assert PRE_EXISTING_KEYS <= set(m), PRE_EXISTING_KEYS - set(m)

    def test_pre_existing_values_are_unchanged_in_shape(self):
        m, _, _ = _sawtooth(400, 10)
        assert m["max_drawdown"] <= 0.0
        assert 0.0 <= m["win_rate"] <= 1.0
        assert m["num_trades"] >= m["closed_trades"]
        assert m["avg_holding_bars"] > 0

    def test_max_consecutive_losses_still_comes_from_the_closed_trade_walk(self):
        m, _, _ = _sawtooth(400, 12)
        # 12 alternating Win/Loss trades: the longest run of either is 1.
        assert m["max_consecutive_losses"] == 1
        assert m["max_consecutive_wins"] == 1

    def test_streaks_agree_with_the_trade_table(self):
        m, equity, position = _sawtooth(400, 8)
        results = [t.result for t in walk_trades(equity, position) if not t.is_open]
        # Rebuild the longest loss run independently.
        best = run = 0
        for r in results:
            run = run + 1 if r == "Loss" else 0
            best = max(best, run)
        assert m["max_consecutive_losses"] == best

    def test_var_and_es_keep_their_original_sign_convention(self):
        m, _, _ = _sawtooth(600, 20, gain=0.004, loss=-0.03)
        assert m["var_95"] <= 0.0, "a negative return VaR must stay negative"
        assert m["es_95"] <= m["var_95"]


# ---------------------------------------------------------------------------
# The PRD key set
# ---------------------------------------------------------------------------


class TestPrdKeyVocabulary:
    def test_every_prd_key_is_present(self):
        m, _, _ = _sawtooth(600, 20)
        assert PRD_KEYS <= set(m), PRD_KEYS - set(m)

    def test_kurtosis_is_excess_so_a_normal_reads_near_zero(self):
        # The raw fourth moment of a normal is 3; the PRD asks for the excess.
        rng = np.random.default_rng(3)
        m, _, _ = _metrics_from(100_000 * np.cumprod(1 + rng.normal(0, 0.004, 2000)), np.ones(2000))
        assert abs(m["kurtosis"]) < 0.5, f"expected excess kurtosis, got {m['kurtosis']}"

    def test_cvar_95_is_never_better_than_var_95(self):
        m, _, _ = _sawtooth(600, 20)
        assert m["cvar_95"] <= m["var_95"] + 1e-12

    def test_cvar_is_aliased_from_es_not_recomputed_to_something_else(self):
        # CVaR 95% IS Expected Shortfall 95%. If these ever drift apart, one of
        # the two cards on the page is lying.
        m, _, _ = _sawtooth(600, 20)
        assert m["cvar_95"] == pytest.approx(m["es_95"])

    def test_omega_is_above_one_only_when_the_curve_gains_overall(self):
        gaining, _, _ = _sawtooth(400, 10, gain=0.01, loss=-0.004)
        losing, _, _ = _sawtooth(400, 10, gain=0.004, loss=-0.02)
        assert gaining["omega"] > 1.0
        assert losing["omega"] < 1.0

    def test_expectancy_inr_is_the_rupee_form_of_expectancy(self):
        m, _, _ = _sawtooth(400, 10)
        assert m["expectancy_inr"] == pytest.approx(m["expectancy"])

    def test_var_and_cvar_rupee_forms_scale_with_final_equity(self):
        m, _, _ = _sawtooth(400, 10, capital=1_000_000.0)
        assert m["var_95_inr"] == pytest.approx(m["var_95"] * m["final_equity"])
        assert m["cvar_95_inr"] == pytest.approx(m["cvar_95"] * m["final_equity"])

    def test_every_value_is_json_serialisable(self):
        # A NaN or an Infinity here becomes `NaN` in the response body, which
        # json.loads accepts and every consumer downstream then mishandles.
        m, _, _ = _sawtooth(600, 20)
        text = json.dumps({k: v for k, v in m.items() if not isinstance(v, (dict, list))})
        for token in ("NaN", "Infinity"):
            assert token not in text, f"{token} must never reach the payload"

    def test_no_infinite_omega_on_a_lossless_curve(self):
        m, _, _ = _metrics_from([100_000 * (1.001**i) for i in range(200)], [1.0] * 199 + [0.0])
        assert math.isfinite(m["omega"])


# ---------------------------------------------------------------------------
# Drawdown detail
# ---------------------------------------------------------------------------


class TestDrawdownDetailWiring:
    def test_duration_is_never_longer_than_the_run(self):
        m, _, _ = _sawtooth(800, 30)
        # 800 business days ≈ 1120 calendar days; the worst episode cannot
        # outlast the run it happened in.
        assert 0 <= m["max_drawdown_duration_days"] <= 1500

    def test_recovery_days_do_not_exceed_duration_days(self):
        m, _, _ = _sawtooth(800, 30)
        assert m["max_drawdown_recovery_days"] <= m["max_drawdown_duration_days"]

    def test_time_underwater_is_a_percentage(self):
        m, _, _ = _sawtooth(800, 30)
        assert 0.0 <= m["time_in_drawdown_pct"] <= 100.0

    def test_deep_drawdowns_are_counted_not_guessed(self):
        # Five 12% dips off a 100k peak, each recovered: five episodes, all
        # over the 10% line. (A 94k bar against a 100k peak is only -6%.)
        equity, position = [100_000.0], [0.0]
        for _ in range(5):
            for level in (88_000.0, 100_000.0):
                equity.append(level)
                position.append(1.0 if level < 100_000.0 else 0.0)
        m, _, _ = _metrics_from(equity, position)
        assert m["drawdowns_over_10pct"] >= 5
        assert m["drawdown_episodes"] >= 5

    def test_a_monotonic_gainer_reports_no_drawdowns(self):
        m, _, _ = _metrics_from([100_000 * (1.002**i) for i in range(300)], [1.0] * 299 + [0.0])
        assert m["drawdown_episodes"] == 0
        assert m["time_in_drawdown_pct"] == 0.0
        assert m["ulcer_index"] == 0.0


# ---------------------------------------------------------------------------
# Trade quality
# ---------------------------------------------------------------------------


class TestTradeQualityWiring:
    def test_durations_are_measured_from_the_bar_walk(self):
        """Lengths 2×9 then a 40-bar hold: true mean 5.8, true median 2.

        The older ``exposure × bars / num_trades`` estimate is algebraically the
        same as the mean whenever the trade spans tile the curve, so the gain
        here is the MEDIAN — which that estimate cannot produce at all.
        """
        lengths = [2] * 9 + [40]
        m = _build_runs(lengths)[0]
        assert m["closed_trades"] == len(lengths)
        assert m["avg_trade_duration_bars"] == pytest.approx(float(np.mean(lengths)), abs=0.01)
        assert m["median_trade_duration_bars"] == pytest.approx(2.0)
        # The estimate agrees with the mean — as the algebra says it must.
        assert m["avg_holding_bars"] == pytest.approx(m["avg_trade_duration_bars"], abs=0.01)
        # …and neither of them is the median, which is the number that describes
        # the trade you are actually in.
        assert m["median_trade_duration_bars"] < m["avg_trade_duration_bars"] / 2

    def test_the_median_is_not_dragged_by_one_long_hold(self):
        m = _build_runs([2] * 9 + [40], step=0.0005)[0]
        assert m["median_trade_duration_bars"] == pytest.approx(2.0)
        assert m["avg_trade_duration_bars"] > m["median_trade_duration_bars"]

    def test_an_open_trade_is_excluded_from_the_durations(self):
        """Its duration is right-censored: held at least N bars, not exactly N."""
        equity, position = [100_000.0], [0.0]
        for _ in range(3):  # three 4-bar closed trades
            for _ in range(4):
                equity.append(equity[-1] * 1.001)
                position.append(1.0)
            equity.append(equity[-1])
            position.append(0.0)
        equity.append(equity[-1] * 1.001)  # ...then a 1-bar OPEN position
        position.append(1.0)
        m, _, _ = _metrics_from(equity, position)
        assert m["closed_trades"] == 3
        assert m["avg_trade_duration_bars"] == pytest.approx(
            4.0
        ), "a 1-bar censored open trade must not drag the average down"

    def test_payoff_ratio_separates_lopsided_gains_from_steady_ones(self):
        """Two runs with the SAME profit factor and completely different shape.

        A — 10 wins of +100, 10 losses of -50  → PF 2.00, payoff 2.0, wins 50%.
        B —  2 wins of +500, 18 losses of -27.8 → PF 2.00, payoff 18.0, wins 10%.

        A profit factor cannot tell these apart; B is one outsized win from
        going nowhere, and that is precisely the risk a payoff ratio exists to
        surface.
        """

        def build(pnls):
            equity, position = [100_000.0], [0.0]
            level = 100_000.0
            for pnl in pnls:
                level += pnl
                equity.append(level)
                position.append(1.0)
                equity.append(level)
                position.append(0.0)
            return _metrics_from(equity, position)[0]

        steady = build([100.0] * 10 + [-50.0] * 10)
        lopsided = build([500.0] * 2 + [-500.0 / 18] * 18)

        # Same headline profit factor...
        assert steady["profit_factor"] == pytest.approx(2.0, abs=0.01)
        assert lopsided["profit_factor"] == pytest.approx(2.0, abs=0.01)
        # ...a nine-fold difference in payoff ratio.
        assert steady["payoff_ratio"] == pytest.approx(2.0, abs=0.01)
        assert lopsided["payoff_ratio"] == pytest.approx(18.0, abs=0.1)
        assert lopsided["payoff_ratio"] > steady["payoff_ratio"] * 5
        assert steady["win_rate"] == pytest.approx(0.5)
        assert lopsided["win_rate"] == pytest.approx(0.1)

    def test_no_trades_gives_zeros_not_a_crash(self):
        m, _, _ = _metrics_from([100_000.0] * 30, [0.0] * 30)
        assert m["closed_trades"] == 0
        assert m["max_consecutive_wins"] == 0
        assert m["max_consecutive_losses"] == 0
        assert m["payoff_ratio"] == 0.0
        assert m["avg_trade_duration_bars"] == 0.0
        assert m["median_trade_duration_bars"] == 0.0


# ---------------------------------------------------------------------------
# Statistical confidence
# ---------------------------------------------------------------------------


class TestStatisticalConfidenceWiring:
    def test_the_flag_follows_the_prd_thresholds(self):
        assert _sawtooth(300, 35)[0]["trade_count_flag"] == "ok"
        assert _sawtooth(300, 24)[0]["trade_count_flag"] == "warn"
        assert _sawtooth(300, 7)[0]["trade_count_flag"] == "insufficient"
        assert _sawtooth(300, 0)[0]["trade_count_flag"] == "insufficient"

    def test_the_flag_counts_closed_trades_not_open_ones(self):
        equity, position = [100_000.0], [0.0]
        for _ in range(5):
            for _ in range(3):
                equity.append(equity[-1] * 1.001)
                position.append(1.0)
            equity.append(equity[-1])
            position.append(0.0)
        for _ in range(10):  # one long position, still open
            equity.append(equity[-1] * 1.0005)
            position.append(1.0)
        m, _, _ = _metrics_from(equity, position)
        assert m["num_trades"] == 6 and m["closed_trades"] == 5
        assert m["trade_count_flag"] == "insufficient"

    def test_sufficient_is_the_inverse_of_the_insufficient_flag(self):
        for n in (35, 24, 7):
            m = _sawtooth(300, n)[0]
            assert m["trade_count_sufficient"] == (m["trade_count_flag"] != FLAG_INSUFFICIENT)

    def test_the_threshold_constants_are_the_prd_ones(self):
        # ≥30 closed trades is "ok", 20-29 is "warn", <20 is "insufficient".
        assert (TRADE_COUNT_OK, TRADE_COUNT_WARN) == (30, 20)

    def test_standard_error_is_derived_from_the_reported_sharpe(self):
        m, _, _ = _sawtooth(600, 30)
        expected = math.sqrt((1 + (m["sharpe"] ** 2) / 2) / m["closed_trades"])
        assert m["sharpe_std_error"] == pytest.approx(expected, abs=1e-3)

    def test_the_interval_is_centred_on_the_sharpe(self):
        m, _, _ = _sawtooth(600, 30)
        assert m["sharpe_ci_low"] == pytest.approx(m["sharpe"] - m["sharpe_std_error"], abs=1e-6)
        assert m["sharpe_ci_high"] == pytest.approx(m["sharpe"] + m["sharpe_std_error"], abs=1e-6)

    def test_a_thin_sample_reports_a_wider_interval_than_a_thick_one(self):
        thin = _sawtooth(300, 8)[0]
        thick = _sawtooth(300, 40)[0]
        assert thin["sharpe_std_error"] > thick["sharpe_std_error"]

    def test_no_closed_trades_gives_a_zero_error_not_a_division_by_zero(self):
        m, _, _ = _metrics_from([100_000.0] * 30, [0.0] * 30)
        assert m["sharpe_std_error"] == 0.0


# ---------------------------------------------------------------------------
# The JSON payload the page actually reads
# ---------------------------------------------------------------------------


class TestAdapterPayload:
    @pytest.fixture(scope="class")
    @classmethod
    def payload(cls):
        candles = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
        result = run_on_candles(
            candles, "sma_crossover", {}, "DEMO", BacktestConfig(initial_capital=100_000.0)
        )
        return BacktestAdapter(result).to_all()

    def test_every_new_metric_reaches_the_payload(self, payload):
        m = payload["metrics"]
        for key in (
            "omega",
            "skewness",
            "kurtosis",
            "ulcer_index",
            "cvar_95_pct",
            "max_drawdown_duration_days",
            "max_drawdown_recovery_days",
            "time_in_drawdown_pct",
            "drawdowns_over_10pct",
            "expectancy_inr",
            "payoff_ratio",
            "max_consecutive_wins",
            "avg_trade_duration_bars",
            "median_trade_duration_bars",
            "sharpe_std_error",
            "trade_count_flag",
        ):
            assert key in m, f"{key} missing from BacktestAdapter.to_all()['metrics']"

    def test_the_payload_keeps_the_original_cards(self, payload):
        m = payload["metrics"]
        for key in (
            "total_pnl",
            "total_return_pct",
            "win_rate_pct",
            "max_drawdown_pct",
            "sharpe",
            "total_trades",
            "closed_trades",
            "open_trades",
        ):
            assert key in m

    def test_trade_count_flag_survives_as_a_string(self, payload):
        """The UI branches on three states, so a truthy boolean would lose one."""
        flag = payload["metrics"]["trade_count_flag"]
        assert isinstance(flag, str)
        assert flag in {"ok", "warn", "insufficient"}

    def test_the_whole_payload_still_serialises_to_json(self, payload):
        text = json.dumps(payload)
        assert "NaN" not in text and "Infinity" not in text
        assert json.loads(text)["metrics"]["sharpe"] == pytest.approx(payload["metrics"]["sharpe"])

    def test_counts_are_numbers_not_strings(self, payload):
        m = payload["metrics"]
        for key in (
            "max_drawdown_duration_days",
            "drawdowns_over_10pct",
            "max_consecutive_wins",
            "sharpe_std_error",
        ):
            assert isinstance(m[key], (int, float)), f"{key} must be numeric"
            assert not isinstance(m[key], bool)
