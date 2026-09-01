"""Tests for Step 20: Main Forward Testing Engine."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backtest.forward.engine import (
    DataConfig,
    ForwardTestingEngine,
    ForwardTestingConfig,
    PortfolioConfig,
    StrategyConfig,
    RiskConfig,
    STATE_VERSION,
    StateManager,
    load_forward_config,
)
from backtest.data.source_tags import SOURCE_TAG_VALUES
from backtest.simulator import CommissionCalculator, ExecutionConfig, OrderSide, SlippageCalculator
from backtest.simulator.execution import OrderExecutor
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


class StepOpenDataSource:
    """Deterministic bars whose opens differ from the previous closes, so a
    fill anchored at the wrong price is provable (ticket F-01)."""

    def __init__(self, rows: list[tuple[str, float, float]]):
        # rows: (date, open, close)
        dates = pd.date_range(rows[0][0], periods=len(rows), freq="D", tz="UTC")
        open_ = [r[1] for r in rows]
        close = [r[2] for r in rows]
        self._df = pd.DataFrame(
            {
                "open": open_,
                "high": [max(o, c) * 1.01 for o, c in zip(open_, close)],
                "low": [min(o, c) * 0.99 for o, c in zip(open_, close)],
                "close": close,
                "volume": 1_000_000,  # never a liquidity constraint
            },
            index=dates,
        )

    def get_candles(self, symbol, start, end, interval="day"):
        return self._df.copy()


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
# State file format v2 (ticket F-04): mode/source classification, versioning
# ---------------------------------------------------------------------------


def _state_engine_mock(**data_kwargs):
    """Minimal engine mock whose config carries the REAL data classification."""

    class MockPortfolio:
        portfolio_id = "pf-f04-test"

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

        def __init__(self, config):
            self.config = config

    config = ForwardTestingConfig(data=DataConfig(**data_kwargs))
    return MockEngine(config)


@pytest.mark.parametrize(
    "data_kwargs, expected",
    [
        ({"mode": "paper", "source": "synthetic"}, ("paper", "synthetic")),
        ({"mode": "paper", "source": "mstock"}, ("paper", "mstock")),
        ({"mode": "live", "source": "mstock"}, ("live", "mstock")),
        # backtest replays simulated fills -> paper bucket (ticket P1.1)
        ({"mode": "backtest", "source": "synthetic"}, ("paper", "synthetic")),
    ],
)
def test_state_payload_classification_from_real_config(data_kwargs, expected):
    """State file carries mode/source derived from the engine's ACTUAL config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        manager = StateManager(state_file)
        engine = _state_engine_mock(**data_kwargs)

        saved = manager.save_state(engine)
        assert Path(saved).exists()

        payload = json.loads(state_file.read_text())
        assert payload["state_version"] == STATE_VERSION
        assert payload["engine_id"] == "pf-f04-test"
        assert (payload["mode"], payload["source"]) == expected
        # Source strings come from the canonical T3/SOURCE_TAGS vocabulary
        assert payload["source"] in SOURCE_TAG_VALUES
        # The payload itself is reloadable and stable
        assert manager.load_state()["mode"] == expected[0]
        assert manager.load_state()["source"] == expected[1]


def test_legacy_v1_state_file_loaded_and_migrated_in_memory():
    """Pre-F-04 state (no state_version) loads, is normalized, and the file is
    only rewritten on the next save (load stays read-only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        state_file.write_text(json.dumps({"portfolio": {}, "loop_count": 7}))
        manager = StateManager(state_file)
        engine = _state_engine_mock(mode="live", source="mstock")

        loaded = manager.load_state(engine=engine)
        assert loaded is not None
        assert loaded["state_version"] == STATE_VERSION
        assert loaded["mode"] == "live"
        assert loaded["source"] == "mstock"
        assert loaded["loop_count"] == 7

        # Read-only on load: disk still holds the legacy v1 shape
        on_disk = json.loads(state_file.read_text())
        assert "state_version" not in on_disk
        assert "mode" not in on_disk

        # Next save migrates the file to v2 with the engine's classification
        manager.save_state(engine)
        migrated = json.loads(state_file.read_text())
        assert migrated["state_version"] == STATE_VERSION
        assert migrated["mode"] == "live"
        assert migrated["source"] == "mstock"


def test_state_file_future_version_refused():
    """A file written by a newer version must not be loaded silently."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        state_file.write_text(json.dumps({"state_version": STATE_VERSION + 1, "portfolio": {}}))
        assert StateManager(state_file).load_state() is None


def test_state_file_invalid_classification_falls_back_to_engine():
    """Garbage mode/source in a state file warns and falls back to the
    engine-derived values rather than propagating invalid classification."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        state_file.write_text(
            json.dumps({"state_version": 2, "mode": "sideways", "source": "quantum", "portfolio": {}})
        )
        manager = StateManager(state_file)
        engine = _state_engine_mock(mode="paper", source="synthetic")
        loaded = manager.load_state(engine=engine)
        assert loaded["mode"] == "paper"
        assert loaded["source"] == "synthetic"


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


# ---------------------------------------------------------------------------
# F-01 acceptance — the forward engine fills at the NEXT bar's open
# ---------------------------------------------------------------------------


def test_forward_engine_fills_at_next_open_not_signal_bar():
    """The strategy signals on bar ``t``; the fill must anchor to bar
    ``t+1``'s OPEN — never bar ``t``'s close (the F-01 look-ahead leak)."""
    #        date         open   close
    rows = [
        ("2024-01-01", 100.0, 100.0),   # t0: below 150, flat
        ("2024-01-02", 105.0, 200.0),   # t1: close 200 > 150 → BUY signal HERE
        ("2024-01-03", 210.0, 300.0),   # t2: fill MUST be 210 (open), not 200
        ("2024-01-04", 305.0, 400.0),   # t3: still long
        ("2024-01-05", 405.0, 90.0),    # t4: close 90 < 150 → SELL signal HERE
        ("2024-01-06", 95.0, 100.0),    # t5: close fill MUST be 95 (open), not 90
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        portfolio = Portfolio(name="F01Accept", initial_capital=100000)
        strategy = DummyStrategy(threshold=150)

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "F01Accept"},
                "strategy": {"name": "dummy_test"},
                "data": {
                    "symbols": ["INFY"],
                    "provider": "mock",
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-06",
                    "timeframe": "1day",
                },
                "system": {
                    "state_file": str(state_file),
                    "loop_interval_seconds": 0,
                    "backtest_mode": True,
                    "save_state_interval_minutes": 0,
                },
            },
            portfolio=portfolio,
            strategy=strategy,
            data_source=StepOpenDataSource(rows),
        )
        engine.initialize_system()

        # Zero-cost executor for exact price assertions (same idea as
        # tests/simulator/test_fill_timing.py).
        engine.executor = OrderExecutor(
            config=ExecutionConfig(seed=7, price_improvement_probability=Decimal("0")),
            slippage=SlippageCalculator.disabled(),
            fees=CommissionCalculator(),
            portfolio=engine.portfolio,
        )
        engine.adapter.min_bars = 1
        # Daily bars with deliberate opens ≠ prev closes trip the validator's
        # gap checks; disable (the existing backtest-mode test does the same).
        try:
            engine.validator.config.gap_detection_enabled = False
            engine.validator.config.spike_detection_enabled = False
            engine.data_handler.validator.config.gap_detection_enabled = False
            engine.data_handler.validator.config.spike_detection_enabled = False
        except Exception:
            pass

        engine._running = True
        engine._run_backtest_mode()

        buys = [o for o in engine.portfolio.filled_orders if o.side is OrderSide.BUY]
        sells = [o for o in engine.portfolio.filled_orders if o.side is OrderSide.SELL]

        assert len(buys) == 1, f"expected 1 fill, got {len(buys)}"
        assert len(sells) == 1, f"expected 1 fill, got {len(sells)}"
        # entry anchored at bar t2's OPEN (210), NOT bar t1's close (200)
        assert buys[0].average_fill_price == Decimal("210")
        assert buys[0].average_fill_price != Decimal("200")
        # exit anchored at bar t5's OPEN (95), NOT bar t4's close (90)
        assert sells[0].average_fill_price == Decimal("95")
        assert sells[0].average_fill_price != Decimal("90")
