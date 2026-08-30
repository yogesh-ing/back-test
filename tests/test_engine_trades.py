"""G1/G2 — trade accounting is measured on realised P&L, and counted once.

Before this fix ``win_rate`` came from the *sign* of the position (every
long-only strategy reported 100%, win or lose) and ``num_trades`` counted position
*transitions* (an entry and its exit = "2 trades" for one round trip). Both are
pinned here, at the unit level (``engine/trades.py``) and at the level the UI
actually reads (``compute_metrics`` + ``BacktestAdapter`` agreeing).
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.engine.trades import trade_stats, walk_trades


def _series(values, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="B")
    return pd.Series([float(v) for v in values], index=idx)


def _frame(closes):
    close = _series(closes)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000.0,
        },
        index=close.index,
    )


# ---------------------------------------------------------------------------
# walk_trades / trade_stats
# ---------------------------------------------------------------------------


def test_flat_run_has_no_trades():
    equity, close = _series([1000, 1000, 1000]), _series([100, 101, 99])
    trades = walk_trades(equity, _series([0, 0, 0]), close)
    assert trades == []
    stats = trade_stats(trades)
    assert stats["num_trades"] == 0
    assert stats["win_rate"] == 0.0  # nothing closed → no opinion, not 0% of wins


def test_losing_long_run_is_a_loss_not_a_win():
    """The exact G1 regression: long-only position losing money."""
    equity = _series([1000, 1000, 900, 800, 700, 700])
    close = _series([100, 100, 90, 80, 70, 70])
    position = _series([0, 1, 1, 1, 0, 0])

    trades = walk_trades(equity, position, close)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "LONG"
    assert t.is_open is False
    assert t.pnl == pytest.approx(-300.0)  # equity[4] - equity[0]
    assert t.result == "Loss"

    stats = trade_stats(trades)
    assert stats["num_trades"] == 1
    assert stats["closed_trades"] == 1
    assert stats["win_rate"] == 0.0, "a losing long-only run must not report 100%"
    assert stats["losing_trades"] == 1


def test_short_trade_uses_the_equity_curve_not_a_price_ratio():
    # Short entered when equity was 1000, closed with equity at 1100 → +100.
    equity = _series([1000, 1000, 1050, 1100, 1100])
    close = _series([100, 100, 95, 90, 90])
    trades = walk_trades(equity, _series([0, -1, -1, 0, 0]), close)
    assert len(trades) == 1
    assert trades[0].side == "SHORT"
    assert trades[0].pnl == pytest.approx(100.0)
    assert trades[0].result == "Win"


def test_open_position_counts_once_and_stays_out_of_win_rate():
    equity = _series([1000, 1000, 1100, 1200])
    close = _series([100, 100, 110, 120])
    trades = walk_trades(equity, _series([0, 1, 1, 1]), close)
    assert len(trades) == 1
    assert trades[0].is_open is True
    assert trades[0].exit_date == str(equity.index[-1].date())

    stats = trade_stats(trades)
    assert stats["num_trades"] == 1  # counted as a trade…
    assert stats["open_trades"] == 1
    assert stats["closed_trades"] == 0
    assert stats["win_rate"] == 0.0, "…but it is not evidence of a win"


def test_zero_pnl_trade_is_flat_not_a_win():
    stats = trade_stats(
        walk_trades(
            _series([1000, 1000, 1000, 1000]), _series([0, 1, 1, 0]), _series([100, 100, 100, 100])
        )
    )
    assert stats["win_rate"] == 0.0
    assert stats["winning_trades"] == 0 and stats["losing_trades"] == 0


def test_sign_flip_tiles_the_curve_without_double_counting():
    equity = _series([1000, 1000, 1100, 1050, 1150, 1150])
    close = _series([100, 100, 110, 105, 115, 115])
    trades = walk_trades(equity, _series([0, 1, 1, -1, -1, 0]), close)
    assert [t.side for t in trades] == ["LONG", "SHORT"]
    assert trades[0].pnl == pytest.approx(100.0)  # equity[2] - equity[0]
    assert trades[1].pnl == pytest.approx(50.0)  # equity[5] - equity[2]
    # Every bar is attributed exactly once → the trade P&L sums to the run's P&L.
    assert sum(t.pnl for t in trades) == pytest.approx(equity.iloc[-1] - equity.iloc[0])


def test_trade_ids_are_sequential_and_rows_carry_dates():
    equity = _series([1000, 1000, 1100, 1100, 900, 900])
    close = _series([100, 100, 110, 110, 90, 90])
    trades = walk_trades(equity, _series([0, 1, 0, 1, 0, 0]), close)
    assert [t.id for t in trades] == [1, 2]
    for t in trades:
        assert t.entry_date <= t.exit_date
        # Trade fields (the adapter is what renames entry_date → date for the UI)
        assert {
            "id",
            "entry_date",
            "exit_date",
            "side",
            "entry",
            "exit",
            "pnl",
            "result",
            "is_open",
        } == set(t.to_dict())


# ---------------------------------------------------------------------------
# compute_metrics + adapter, i.e. what the pages render
# ---------------------------------------------------------------------------


def _run(signals, closes, **cfg):
    candles = _frame(closes)
    result = Backtester(BacktestConfig(initial_capital=1000.0, **cfg)).run(
        candles, _series(signals)
    )
    return result


def test_metrics_count_round_trips_not_transitions():
    result = _run([1, 1, 1, 0, 0, 0], [100, 100, 95, 90, 90, 90])
    m = result.metrics
    assert m["num_trades"] == 1, "one entry + one exit is one trade (was counted as 2)"
    assert m["closed_trades"] == 1 and m["open_trades"] == 0
    assert m["win_rate"] == 0.0 and m["losing_trades"] == 1


def test_metrics_win_rate_tracks_the_winning_trades():
    """Lagged positions make this run: in at bar 1 (win), bar 3 (loss), bar 5 (open).

    So 3 trades, 2 closed, 1 win / 1 loss → 50% — and the open +300 winner is
    deliberately not in that denominator.
    """
    result = _run(
        [1, 0, 1, 0, 1, 0], [100, 110, 110, 100, 100, 130], commission_pct=0.0, slippage_pct=0.0
    )
    m = result.metrics
    assert list(result.position) == [0.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    assert m["num_trades"] == 3
    assert m["closed_trades"] == 2 and m["open_trades"] == 1
    assert m["winning_trades"] == 1 and m["losing_trades"] == 1
    assert m["win_rate"] == pytest.approx(0.5)


def test_realised_pnl_and_total_pnl_agree():
    result = _run([1, 1, 0, 1, 0, 0], [100, 110, 105, 105, 120, 120])
    m = result.metrics
    assert m["realised_pnl"] == pytest.approx(m["total_return"] * 1000.0, abs=1e-6)
    assert m["best_trade_pnl"] >= m["worst_trade_pnl"]


@pytest.mark.parametrize(
    "strategy,params",
    [
        ("sma_crossover", {"fast": 5, "slow": 15}),
        ("rsi_reversion", {}),
        ("buy_and_hold", {}),
        ("donchian_breakout", {}),
    ],
)
def test_adapter_card_and_table_agree_on_every_strategy(strategy, params):
    """The acceptance check for G1/G2: no card may contradict the rows it sums up."""
    from backtest.adapters.backtest_adapter import BacktestAdapter
    from backtest.data.synthetic import SyntheticSource
    from backtest.runner import run_on_candles

    candles = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    result = run_on_candles(
        candles, strategy, params, "DEMO", BacktestConfig(initial_capital=100_000.0)
    )
    payload = BacktestAdapter(result).to_all()
    m, rows = payload["metrics"], payload["trades"]

    assert m["total_trades"] == len(rows), "Trades card must equal the number of rows"
    assert m["closed_trades"] + m["open_trades"] == m["total_trades"]
    # Σ row P&L == the P&L card (they are the same computation now).
    assert sum(r["pnl"] for r in rows) == pytest.approx(m["total_pnl"], abs=1.0)
    # Win rate is over closed rows only.
    closed_rows = [r for r in rows if not r["is_open"]]
    wins = sum(1 for r in closed_rows if r["result"] == "Win")
    expected = 100.0 * wins / len(closed_rows) if closed_rows else 0.0
    assert m["win_rate_pct"] == pytest.approx(expected, abs=0.01)
    # A losing-but-long-only run can no longer claim 100%.
    if m["total_return_pct"] < 0 and closed_rows:
        assert m["win_rate_pct"] < 100.0


def test_result_dict_keys_are_backwards_compatible():
    """Consumers (CLI --sort-by, comparison tool, dashboards) read these keys."""
    m = _run([1, 1, 0, 0], [100, 110, 105, 105]).metrics
    for key in (
        "total_return",
        "cagr",
        "volatility",
        "sharpe",
        "max_drawdown",
        "calmar",
        "num_trades",
        "win_rate",
        "exposure",
        "final_equity",
        "bars",
    ):
        assert key in m
    for key in (
        "closed_trades",
        "open_trades",
        "winning_trades",
        "losing_trades",
        "realised_pnl",
        "avg_trade_pnl",
        "best_trade_pnl",
        "worst_trade_pnl",
    ):
        assert key in m, f"new breakdown key {key} missing"
