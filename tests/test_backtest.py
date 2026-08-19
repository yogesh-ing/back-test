"""Acceptance tests for backtest engine (Card 03–04 invariants)."""

import pandas as pd

from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.strategy.registry import get_strategy, list_strategies


def test_synthetic_source_canonical_columns():
    """Test 1: Synthetic source returns canonical columns, ascending DatetimeIndex, > 50 rows."""
    source = SyntheticSource()
    candles = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    
    assert list(candles.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(candles.index, pd.DatetimeIndex)
    assert candles.index.is_monotonic_increasing
    assert len(candles) > 50


def test_synthetic_source_deterministic():
    """Test 2: Synthetic source is deterministic (same symbol/date ⇒ identical frame)."""
    source = SyntheticSource()
    candles1 = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    candles2 = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    
    pd.testing.assert_frame_equal(candles1, candles2)


def test_all_strategies_auto_registered():
    """Test 3: sma_crossover, rsi_reversion, buy_and_hold, donchian_breakout all auto-registered."""
    strategies = list_strategies()
    expected = {"sma_crossover", "rsi_reversion", "buy_and_hold", "donchian_breakout"}
    assert set(strategies) == expected


def test_unknown_strategy_param_raises():
    """Test 4: Unknown strategy param ⇒ ValueError."""
    strategy_cls = get_strategy("sma_crossover")
    try:
        strategy_cls(unknown_param=123)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "unknown" in str(e).lower()


def test_run_exposes_all_metrics():
    """Test 5: A full run exposes total_return, cagr, max_drawdown, sharpe, num_trades, win_rate, final_equity."""
    source = SyntheticSource()
    candles = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    strategy_cls = get_strategy("sma_crossover")
    signals = strategy_cls().generate_signals(candles)
    
    result = Backtester(BacktestConfig(initial_capital=100000.0)).run(candles, signals)
    metrics = result.metrics
    
    required_keys = {
        "total_return",
        "cagr",
        "max_drawdown",
        "sharpe",
        "num_trades",
        "win_rate",
        "final_equity",
        "bars",
    }
    assert set(metrics.keys()) >= required_keys
    assert len(result.equity) == metrics["bars"]


def test_no_lookahead():
    """Test 6: No look-ahead—first held position is 0."""
    source = SyntheticSource()
    candles = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    strategy_cls = get_strategy("sma_crossover")
    signals = strategy_cls().generate_signals(candles)
    
    result = Backtester(BacktestConfig(initial_capital=100000.0)).run(candles, signals)
    
    # First position should be 0 (signal at bar 0 is traded starting bar 1)
    assert result.position.iloc[0] == 0


def test_zero_cost_buy_and_hold():
    """Test 7: Zero-cost buy-and-hold total return == close[-1]/close[0]−1."""
    source = SyntheticSource()
    candles = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    strategy_cls = get_strategy("buy_and_hold")
    signals = strategy_cls().generate_signals(candles)
    
    result = Backtester(
        BacktestConfig(initial_capital=100000.0, commission_pct=0.0, slippage_pct=0.0)
    ).run(candles, signals)
    
    expected = candles["close"].iloc[-1] / candles["close"].iloc[0] - 1
    assert abs(result.metrics["total_return"] - expected) < 1e-6
