"""PRD Task 6.2 — BacktestAdapter tests."""

import json

import pandas as pd

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.runner import run_on_candles


def _run(strategy="sma_crossover", params=None, capital=100_000.0, symbol="DEMO"):
    candles = SyntheticSource().get_candles(symbol, "2021-01-01", "2024-01-01", "day")
    return candles, run_on_candles(candles, strategy, params or {}, symbol,
                                   BacktestConfig(initial_capital=capital))


def _adapter():
    _, result = _run()
    return BacktestAdapter(result)


def test_to_metrics_has_required_cards():
    m = _adapter().to_metrics()
    required = {"total_pnl", "total_return_pct", "win_rate_pct", "max_drawdown_pct",
                "sharpe", "total_trades"}
    assert required <= set(m)
    # PnL card sign is consistent with total return
    assert (m["total_pnl"] >= 0) == (m["total_return_pct"] >= 0)


def test_to_equity_shape_and_benchmark():
    eq = _adapter().to_equity()
    assert set(eq) == {"dates", "values", "benchmark"}
    n = len(eq["values"])
    assert n == len(eq["dates"]) == len(eq["benchmark"])
    assert n > 50
    # benchmark starts at initial capital (buy & hold from bar 0)
    assert abs(eq["benchmark"][0] - 100_000.0) < 1e-6
    assert all(isinstance(d, str) for d in eq["dates"])


def test_to_drawdown_worst_is_non_positive():
    dd = _adapter().to_drawdown()
    assert set(dd) == {"dates", "values", "worst_dd_pct", "worst_dd_date"}
    assert dd["worst_dd_pct"] <= 0.0
    # reported worst matches the min of the series
    assert abs(min(dd["values"]) * 100 - dd["worst_dd_pct"]) < 1e-6


def test_to_trades_fields_and_results():
    trades = _adapter().to_trades()
    assert trades, "expected at least one trade"
    required = {"id", "date", "side", "entry", "exit", "pnl", "result"}
    for t in trades:
        assert required <= set(t)
        assert t["side"] in {"LONG", "SHORT"}
        assert t["result"] in {"Win", "Loss"}
        assert (t["result"] == "Win") == (t["pnl"] >= 0)
    # ids are sequential 1..N
    assert [t["id"] for t in trades] == list(range(1, len(trades) + 1))


def test_buy_and_hold_yields_single_round_trip():
    _, result = _run("buy_and_hold")
    trades = BacktestAdapter(result).to_trades()
    assert len(trades) == 1
    assert trades[0]["side"] == "LONG"


def test_to_signals_shape():
    sig = _adapter().to_signals()
    assert set(sig) == {"candles", "buys", "sells"}
    assert sig["candles"], "candle list populated"
    assert {"date", "open", "high", "low", "close"} <= set(sig["candles"][0])
    for m in sig["buys"] + sig["sells"]:
        assert set(m) == {"date", "price"}


def test_to_compare_combines_payload():
    cmp = _adapter().to_compare()
    for key in ("total_return_pct", "win_rate_pct", "max_drawdown_pct",
                "sharpe", "total_trades", "metrics", "equity"):
        assert key in cmp


def test_to_all_is_json_serializable():
    out = _adapter().to_all()
    assert set(out) == {"config", "metrics", "equity", "drawdown", "trades", "signals"}
    # must round-trip through json (no numpy scalars / Timestamps)
    json.dumps(out)
    assert out["config"]["strategy"] == "sma_crossover"


def test_adapter_does_not_mutate_input():
    _, result = _run()
    equity_before = result.equity.copy()
    BacktestAdapter(result).to_all()
    pd.testing.assert_series_equal(result.equity, equity_before)
