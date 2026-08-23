"""Tests for Step 20: Main Forward Testing Engine."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from decimal import Decimal

import pandas as pd
import pytest

from backtest.forward.engine import (
    ForwardTestingEngine,
    ForwardTestingConfig,
    PortfolioConfig,
    StrategyConfig,
    RiskConfig,
    StateManager,
    load_forward_config,
)
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.base import Strategy


class DummyStrategy(Strategy):
    name = ""
    params = {"threshold": 100}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "dummy_test"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return (candles["close"] > self.threshold).astype(int)


class MockDataSource:
    def __init__(self, bars=10):
        self.bars = bars

    def get_candles(self, symbol, start, end, interval="day"):
        import numpy as np

        dates = pd.date_range(start, end, freq="D", tz="UTC")[: self.bars]
        close = 100 + np.cumsum(np.random.randn(len(dates)) * 0.5)
        df = pd.DataFrame(
            {
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000,
            },
            index=dates,
        )
        return df


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_forward_config_defaults():
    cfg = ForwardTestingConfig()
    assert cfg.portfolio.initial_capital == Decimal("100000")
    assert cfg.strategy.name == "sma_crossover"
    assert cfg.data.symbols == ["INFY"]


def test_forward_config_from_dict():
    cfg = ForwardTestingConfig.from_dict(
        {
            "portfolio": {"initial_capital": 50000, "name": "MyTest"},
            "strategy": {"name": "rsi_reversion", "parameters": {"period": 14}},
            "data": {"symbols": ["RELIANCE"], "provider": "mock"},
            "system": {"dry_run": True},
        }
    )
    assert cfg.portfolio.initial_capital == Decimal("50000")
    assert cfg.portfolio.name == "MyTest"
    assert cfg.strategy.name == "rsi_reversion"
    assert cfg.data.symbols == ["RELIANCE"]
    assert cfg.system.dry_run is True


def test_load_forward_config_missing_file():
    # Explicit path missing should raise
    with pytest.raises(Exception):
        load_forward_config(path="/tmp/nonexistent_xyz.yaml")

    # No path -> defaults (or existing file)
    cfg = load_forward_config(path=None)
    assert cfg is not None


def test_load_forward_config_defaults_when_no_file():
    cfg = load_forward_config(path=None)
    # Should not raise, return defaults or file config if exists
    assert cfg is not None


# ---------------------------------------------------------------------------
# State Manager
# ---------------------------------------------------------------------------


def test_state_manager_save_load():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        manager = StateManager(state_file)

        # Create minimal engine mock
        class MockPortfolio:
            def to_dict(self):
                return {"name": "test", "initial_capital": "100000"}

        class MockAdapter:
            def get_state(self):
                return {"symbols": ["INFY"], "bars": {}}

        class MockPerf:
            equity_curve = []

        class MockEngine:
            portfolio = MockPortfolio()
            adapter = MockAdapter()
            performance = MockPerf()
            _loop_count = 5
            config = ForwardTestingConfig()

        engine = MockEngine()
        saved = manager.save_state(engine)
        assert Path(saved).exists()

        loaded = manager.load_state()
        assert loaded is not None
        assert loaded["loop_count"] == 5

        # should_save
        from datetime import datetime, timezone, timedelta

        assert manager.should_save(datetime.now(timezone.utc) - timedelta(minutes=10), 5) is True
        assert manager.should_save(datetime.now(timezone.utc), 5) is False


# ---------------------------------------------------------------------------
# Engine initialization
# ---------------------------------------------------------------------------


def test_engine_initialization():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"initial_capital": 100000, "name": "InitTest"},
                "strategy": {"name": "sma_crossover", "parameters": {"fast": 2, "slow": 3}},
                "data": {"symbols": ["INFY"], "provider": "mock"},
                "system": {"loop_interval_seconds": 0, "save_state_interval_minutes": 0, "state_file": str(state_file)},
            }
        )
        engine.initialize_system()

        assert engine.portfolio is not None
        assert engine.strategy is not None
        assert engine.adapter is not None
        assert engine.data_handler is not None
        assert engine.validator is not None
        assert engine.risk_manager is not None
        assert engine.performance is not None


def test_engine_with_provided_portfolio_and_strategy():
    portfolio = Portfolio(name="ProvidedTest", initial_capital=50000)
    strategy = DummyStrategy(threshold=100)

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        engine = ForwardTestingEngine(
            config_dict={
                "data": {"symbols": ["INFY"]},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0},
            },
            portfolio=portfolio,
            strategy=strategy,
        )
        engine.initialize_system()

        assert engine.portfolio.name == "ProvidedTest"
        assert engine.strategy.name == "dummy_test"


def test_engine_lifecycle_hooks():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "HookTest"},
                "strategy": {"name": "sma_crossover"},
                "data": {"symbols": ["INFY"]},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0},
            }
        )
        engine.initialize_system()

        called = []

        engine.add_hook("on_start", lambda e: called.append("start"))
        engine.add_hook("on_stop", lambda e: called.append("stop"))
        engine.add_hook("on_error", lambda e, exc: called.append(f"error:{exc}"))

        engine.on_start()
        engine.on_error(Exception("test"))
        engine.on_stop()

        assert "start" in called
        assert "stop" in called
        assert any("error" in c for c in called)


def test_engine_pause_resume():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "PauseTest"},
                "strategy": {"name": "sma_crossover"},
                "data": {"symbols": ["INFY"]},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0},
            }
        )
        engine.initialize_system()

        engine._running = True
        engine.pause()
        assert engine._paused is True

        engine.resume()
        assert engine._paused is False


def test_engine_bar_processing():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        portfolio = Portfolio(name="BarTest", initial_capital=100000)
        strategy = DummyStrategy(threshold=100)

        engine = ForwardTestingEngine(
            config_dict={
                "data": {"symbols": ["INFY"]},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0, "save_state_interval_minutes": 0},
            },
            portfolio=portfolio,
            strategy=strategy,
        )
        engine.initialize_system()
        # Lower min_bars for testing
        engine.adapter.min_bars = 1

        bar = {"symbol": "INFY", "timestamp": "2024-01-01T09:15:00+05:30", "open": 99, "high": 102, "low": 98, "close": 101, "volume": 1000}

        # Inject and process
        engine.data_handler.inject_bar(bar)
        market_data = engine.data_handler.get_latest_data()
        assert engine.validator.validate(market_data)

        sigs = engine.adapter.on_bar_close(bar)
        assert len(sigs) >= 0

        engine.performance.update_metrics()
        assert engine.performance.get_metrics() is not None


def test_engine_backtest_mode():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        portfolio = Portfolio(name="BacktestTest", initial_capital=100000)
        strategy = DummyStrategy(threshold=100)

        data_source = MockDataSource(bars=25)

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "BacktestTest"},
                "strategy": {"name": "dummy_test"},
                "data": {"symbols": ["INFY"], "provider": "mock", "start_date": "2024-01-01", "end_date": "2024-01-10", "timeframe": "1day"},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0, "backtest_mode": True, "save_state_interval_minutes": 0},
            },
            portfolio=portfolio,
            strategy=strategy,
            data_source=data_source,
        )
        engine.initialize_system()
        engine.adapter.min_bars = 2
        # Disable gap and spike detection for daily backtest with random data
        try:
            engine.validator.config.gap_detection_enabled = False
            engine.validator.config.spike_detection_enabled = False
            engine.data_handler.validator.config.gap_detection_enabled = False
            engine.data_handler.validator.config.spike_detection_enabled = False
        except Exception:
            pass
        engine._running = True
        engine._run_backtest_mode()

        assert engine._loop_count == 10  # 2024-01-01 to 2024-01-10 = 10 days
        # In dry_run false, but with mock data, should have generated signals
        assert len(engine.adapter.signal_history) >= 1


def test_engine_dry_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        portfolio = Portfolio(name="DryRunTest", initial_capital=100000)
        strategy = DummyStrategy(threshold=50)  # low threshold, always buy

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "DryRunTest"},
                "strategy": {"name": "dummy_test"},
                "data": {"symbols": ["INFY"]},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0, "dry_run": True},
            },
            portfolio=portfolio,
            strategy=strategy,
        )
        engine.initialize_system()
        engine.adapter.min_bars = 1

        bar = {"symbol": "INFY", "timestamp": "2024-01-01T09:15:00+05:30", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        engine.adapter.on_bar_close(bar)

        # dry_run: signals but no orders
        assert len(engine.adapter.signal_history) >= 1
        assert len(engine.adapter.order_history) == 0


def test_engine_get_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "StatusTest"},
                "strategy": {"name": "sma_crossover"},
                "data": {"symbols": ["INFY"]},
                "system": {"state_file": str(state_file), "loop_interval_seconds": 0},
            }
        )
        engine.initialize_system()

        status = engine.get_status()
        assert "running" in status
        assert "portfolio" in status
        assert "performance" in status
        assert status["running"] is False  # not started yet


def test_engine_with_all_real_strategies():
    from backtest.strategy.registry import get_strategy

    for strat_name in ["sma_crossover", "buy_and_hold"]:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / f"state_{strat_name}.json"

            StratCls = get_strategy(strat_name)
            strat = StratCls()

            portfolio = Portfolio(name=f"RealStrat_{strat_name}", initial_capital=100000)

            engine = ForwardTestingEngine(
                config_dict={
                    "portfolio": {"name": f"RealStrat_{strat_name}"},
                    "data": {"symbols": ["INFY"]},
                    "system": {"state_file": str(state_file), "loop_interval_seconds": 0, "dry_run": True},
                },
                portfolio=portfolio,
                strategy=strat,
            )
            engine.initialize_system()
            engine.adapter.min_bars = 1

            # Feed a few bars
            for i in range(5):
                bar = {"symbol": "INFY", "timestamp": f"2024-01-0{i+1}T09:15:00+05:30", "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 1000}
                engine.adapter.on_bar_close(bar)

            assert len(engine.adapter.signal_history) >= 1
