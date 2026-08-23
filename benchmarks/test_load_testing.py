"""Load testing for forward testing simulator (Step 24).

Simulates high load: many symbols, many ticks, many orders.

Run with: pytest benchmarks/test_load_testing.py -s
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from backtest.simulator.portfolio import Portfolio
from backtest.simulator.execution import OrderExecutor
from backtest.live.market_data_handler import MarketDataHandler


def test_load_many_symbols():
    """Load test: 100 symbols with mock data."""
    symbols = [f"SYM{i}" for i in range(100)]

    handler = MarketDataHandler(symbols=symbols, provider="mock", buffer_size=100)
    handler.connect()

    start = time.time()

    # Inject 10 bars per symbol
    for symbol in symbols:
        for i in range(10):
            bar = {
                "symbol": symbol,
                "timestamp": f"2024-01-0{(i%9)+1}T09:15:00+05:30",
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100 + i,
                "volume": 1000,
            }
            handler.inject_bar(bar)

    elapsed = time.time() - start
    print(f"Loaded {len(symbols)} symbols * 10 bars in {elapsed:.2f}s")

    stats = handler.get_stats()
    assert stats["bars_built"] == 1000
    assert elapsed < 10.0  # Should be fast


def test_load_many_orders():
    """Load test: 1000 orders execution."""

    portfolio = Portfolio(name="load_test", initial_capital=10_000_000)
    executor = OrderExecutor(portfolio=portfolio)

    from backtest.simulator.order import Order

    market_data = {"bid": 99, "ask": 101, "last": 100, "volume": 100000}

    start = time.time()

    for i in range(1000):
        order = Order(symbol=f"SYM{i%100}", side="buy", quantity=10, order_type="market")
        order.submit()
        executor.execute(order, market_data)

    elapsed = time.time() - start
    print(f"Executed 1000 orders in {elapsed:.2f}s")

    assert elapsed < 10.0


def test_load_engine_replay():
    """Load test: engine replay 1000 bars."""

    from backtest.forward.engine import ForwardTestingEngine
    import pandas as pd
    import numpy as np
    import tempfile
    from pathlib import Path

    class LargeDataSource:
        def get_candles(self, symbol, start, end, interval="day"):
            dates = pd.date_range(start, end, freq="1min", tz="UTC")[:1000]
            close = 100 + np.cumsum(np.random.randn(len(dates)) * 0.1)
            df = pd.DataFrame(
                {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000},
                index=dates,
            )
            return df

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "LoadTest"},
                "strategy": {"name": "sma_crossover", "parameters": {"fast": 2, "slow": 3}},
                "data": {"symbols": ["INFY"], "timeframe": "1min", "start_date": "2024-01-01", "end_date": "2024-01-10"},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0, "backtest_mode": True, "save_state_interval_minutes": 0},
            },
            data_source=LargeDataSource(),
        )
        engine.initialize_system()
        engine.adapter.min_bars = 2
        # Disable validation for speed
        try:
            engine.validator.config.gap_detection_enabled = False
            engine.validator.config.spike_detection_enabled = False
            engine.data_handler.validator.config.gap_detection_enabled = False
            engine.data_handler.validator.config.spike_detection_enabled = False
        except Exception:
            pass

        engine._running = True

        start = time.time()
        engine._run_backtest_mode()
        elapsed = time.time() - start

        print(f"Replayed 1000 bars in {elapsed:.2f}s, loops={engine._loop_count}")

        assert elapsed < 10.0
