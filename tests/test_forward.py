import json
from pathlib import Path

import pandas as pd

from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.forward.paper import run_live_papertrade, run_walkforward, save_state, load_state
from backtest.forward.portfolio import Portfolio
from backtest.strategy.registry import get_strategy


def test_walkforward_reconciles_with_vectorized_backtest():
    source = SyntheticSource()
    candles = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    strategy_cls = get_strategy("sma_crossover")
    signals = strategy_cls().generate_signals(candles)
    backtest = Backtester(BacktestConfig(initial_capital=100000.0)).run(candles, signals)

    walk = run_walkforward(
        source,
        ["sma_crossover"],
        "DEMO",
        "2021-01-01",
        "2024-01-01",
        {"sma_crossover": 100000.0},
    )

    assert len(walk["equity"]["sma_crossover"]) == len(backtest.equity)
    pd.testing.assert_series_equal(
        pd.Series(walk["equity"]["sma_crossover"], index=candles.index),
        backtest.equity,
        rtol=1e-5,
        atol=1e-5,
        check_names=False,
    )


def test_strategy_capital_isolation():
    source = SyntheticSource()
    walk = run_walkforward(
        source,
        ["sma_crossover", "rsi_reversion"],
        "DEMO",
        "2021-01-01",
        "2024-01-01",
        {"sma_crossover": 50_000.0, "rsi_reversion": 50_000.0},
    )

    assert set(walk["portfolio"].accounts) == {"sma_crossover", "rsi_reversion"}
    assert walk["portfolio"].accounts["sma_crossover"].cash >= 0
    assert walk["portfolio"].accounts["rsi_reversion"].cash >= 0
    assert walk["portfolio"].accounts["sma_crossover"].cash + walk["portfolio"].accounts["rsi_reversion"].cash >= 0


def test_portfolio_snapshot_roundtrip(tmp_path):
    portfolio = Portfolio({"sma_crossover": 100_000.0})
    portfolio.accounts["sma_crossover"].position = 1.0
    portfolio.accounts["sma_crossover"].cash = 90_000.0
    portfolio.mark_to_market({"sma_crossover": 105.0})

    path = tmp_path / "portfolio.json"
    save_state(portfolio, str(path))
    loaded = load_state(str(path))

    assert loaded.snapshot() == portfolio.snapshot()
    assert loaded.allocations == portfolio.allocations
    assert loaded.accounts["sma_crossover"].position == portfolio.accounts["sma_crossover"].position
    assert loaded.accounts["sma_crossover"].cash == portfolio.accounts["sma_crossover"].cash


def test_live_papertrade_state_resume_roundtrip(tmp_path):
    source = SyntheticSource()
    path = tmp_path / "live_state.json"

    first = run_live_papertrade(
        source,
        ["sma_crossover"],
        "DEMO",
        {"sma_crossover": 100_000.0},
        from_date="2021-01-01",
        to_date="2021-12-31",
        interval="day",
        state_file=str(path),
    )

    second = run_live_papertrade(
        source,
        ["sma_crossover"],
        "DEMO",
        {"sma_crossover": 100_000.0},
        from_date="2021-01-01",
        to_date="2021-12-31",
        interval="day",
        state_file=str(path),
    )

    assert first["portfolio"].snapshot() == second["portfolio"].snapshot()
    assert path.exists()
    assert second["state"]["resume_count"] >= 1
