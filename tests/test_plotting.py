"""Acceptance tests for plotting (Card 04)."""

import tempfile
from pathlib import Path

import matplotlib.pyplot as plt

from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.engine.plotting import plot_result
from backtest.strategy.registry import get_strategy


def test_plot_result_returns_figure_and_writes_png():
    """Test 19: plot_result returns a figure with 3 axes and writes a PNG."""
    source = SyntheticSource()
    candles = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    strategy_cls = get_strategy("sma_crossover")
    signals = strategy_cls().generate_signals(candles)

    result = Backtester(BacktestConfig(initial_capital=100000.0)).run(candles, signals)

    with tempfile.TemporaryDirectory() as tmpdir:
        png_path = Path(tmpdir) / "test_plot.png"
        fig = plot_result(result, path=str(png_path))

        # Check figure has 3 axes
        assert fig is not None
        assert len(fig.axes) == 3, f"expected 3 axes, got {len(fig.axes)}"

        # Check PNG was written
        assert png_path.exists()
        assert png_path.stat().st_size > 0

        plt.close(fig)
