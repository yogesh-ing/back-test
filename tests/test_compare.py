"""Acceptance tests for strategy comparison (Card 04)."""

from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig
from backtest.runner import compare_strategies
from backtest.strategy.registry import get_strategy


def test_compare_strategies_runs_all():
    """Test 13: compare_strategies runs all requested strategies; each has sharpe."""
    source = SyntheticSource()
    strategies = ["sma_crossover", "rsi_reversion", "buy_and_hold"]
    
    results = compare_strategies(
        source,
        "DEMO",
        "2021-01-01",
        "2024-01-01",
        strategies,
        "day",
        BacktestConfig(initial_capital=100000.0),
    )
    
    assert set(results.keys()) == set(strategies)
    for name, result in results.items():
        assert "sharpe" in result.metrics
        assert isinstance(result.metrics["sharpe"], float)


def test_compare_strategies_share_bars():
    """Test 14: All results share an identical bar count (same candles reused)."""
    source = SyntheticSource()
    strategies = ["sma_crossover", "rsi_reversion", "buy_and_hold", "donchian_breakout"]
    
    results = compare_strategies(
        source,
        "DEMO",
        "2021-01-01",
        "2024-01-01",
        strategies,
        "day",
    )
    
    bar_counts = [result.metrics["bars"] for result in results.values()]
    assert len(set(bar_counts)) == 1, f"bar counts differ: {bar_counts}"


def test_no_stop_leak():
    """Test 15: donchian_breakout + sma_crossover together ⇒ donchian has stop_loss, sma has None."""
    donchian = get_strategy("donchian_breakout")
    sma = get_strategy("sma_crossover")
    
    assert donchian.stop_loss is not None
    assert sma.stop_loss is None
