"""Ticket #7 — forward state: save → teardown → restore == never stopped.

T5 (F-04) proved the state file *serializes* mode/source correctly. These
tests prove the engine *restores through its real lifecycle*: a resumed
engine continues the SAME canonical bar clock (signal → submit → step at the
next bar's open) and reaches byte-meaningfully identical state/behaviour to
an engine that never stopped — including an order that was armed at teardown.

The resume scenario is built by running the engine over a PREFIX of the
frame and saving (the engine's adapter/indicators use only bars seen so far,
so stopping at bar K leaves exactly the same in-memory state a truncated run
does), then restoring with the FULL frame.
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from backtest.forward.engine import STATE_VERSION, ForwardTestingEngine
from backtest.simulator.engine_loop import Bar
from backtest.simulator.enums import OrderSide
from backtest.simulator.execution import free_executor
from backtest.simulator.order import Order
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.base import Strategy

# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------
#
# 14 daily bars. Signal (close > 100): 0 on bar 0, 1 on bars 1..9, 0 on bars
# 10..13. With threshold=100 there is exactly one 0→1 transition (bar 1) and
# one 1→0 transition (bar 10).
#   bar 1  -> BUY submitted; bar 2 -> BUY FILLS at bar 2's OPEN (100.5)
#   bar 10 -> SELL submitted; bar 11 -> SELL FILLS at bar 11's OPEN (79.5)
# K=2 teardown therefore leaves the BUY armed-unfilled: the exact in-flight
# timing a restart must preserve.

_CLOSES = [99, 101, 101, 101, 101, 101, 101, 101, 101, 101, 80, 80, 80, 80]


def _frame(n_bars: int | None = None):
    closes = _CLOSES[:n_bars] if n_bars else _CLOSES
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [10_000] * len(closes),
        },
        index=idx,
    )


class ThresholdStrategy(Strategy):
    name = ""
    params = {"threshold": 100}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "threshold_test"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return (candles["close"] > self.threshold).astype(int)


class FrameBacktestSource:
    """DataSource over a fixed frame; ``get_candles`` returns it verbatim."""

    def __init__(self, candles: pd.DataFrame):
        self.candles = candles

    def get_candles(self, symbol, start, end, interval="day"):
        return self.candles


def _build_engine(state_file: Path, candles: pd.DataFrame):
    engine = ForwardTestingEngine(
        config_dict={
            "portfolio": {"name": "RoundTrip", "initial_capital": 100_000},
            "strategy": {"name": "threshold_test"},
            "data": {
                "symbols": ["TEST"],
                "provider": "mock",
                "start_date": "2024-01-01",
                "end_date": "2024-01-14",
                "timeframe": "1day",
            },
            "system": {
                "state_file": str(state_file),
                "loop_interval_seconds": 0,
                "backtest_mode": True,
                "save_state_interval_minutes": 0,
            },
        },
        strategy=ThresholdStrategy(),
        data_source=FrameBacktestSource(candles),
    )
    engine.initialize_system()
    engine.adapter.min_bars = 2
    # Deterministic daily bars: disable gap/spike validation like the other
    # backtest-engine tests.
    try:
        engine.validator.config.gap_detection_enabled = False
        engine.validator.config.spike_detection_enabled = False
        engine.data_handler.validator.config.gap_detection_enabled = False
        engine.data_handler.validator.config.spike_detection_enabled = False
    except Exception:  # noqa: BLE001
        pass
    return engine


def _run(engine):
    engine._running = True
    engine._run_backtest_mode()


def _snapshot(engine):
    pf = engine.portfolio
    return {
        "equity": float(pf.calculate_total_equity()),
        "cash": str(pf.current_cash),
        "positions": {
            sym: (float(p.quantity), str(p.average_entry_price)) for sym, p in pf.positions.items()
        },
        "closed": len(pf.closed_positions),
        "realized_pnl": str(pf.realized_pnl),
        "pending_orders": len(pf.pending_orders),
        "filled_orders": len(pf.filled_orders),
        "last_target": dict(engine.adapter._last_target),
        "loop_count": engine._loop_count,
        "processed_bars": dict(engine._processed_bars),
        "bars_seen": {s: len(df) for s, df in engine.adapter._bars.items()},
    }


# ---------------------------------------------------------------------------
# The core assertion: resume == never stopped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stop_after_bars",
    [2, 6],
    ids=["armed-order-at-teardown", "settled-position-at-teardown"],
)
def test_resume_equals_never_stopped(stop_after_bars):
    """A restored engine reproduces a never-stopped engine exactly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        full = _frame()

        # Never stopped: full run in one go.
        never = _build_engine(state_file, full)
        _run(never)
        expected = _snapshot(never)

        # Teardown: run only the prefix, save state.
        torn = _build_engine(state_file, full.iloc[:stop_after_bars])
        _run(torn)
        torn.state_manager.save_state(torn)

        # Restore: fresh engine on the FULL frame, same state file.
        restored = _build_engine(state_file, full)
        assert restored._loop_count == stop_after_bars  # runtime restored
        assert restored._processed_bars == {"TEST": stop_after_bars}
        _run(restored)
        actual = _snapshot(restored)

        assert actual == expected, (
            f"resume diverged after stopping at bar {stop_after_bars}:\n"
            f"expected={expected}\nactual={actual}"
        )


def test_resumed_engine_fills_pending_order_at_next_bar_open():
    """The bar clock is canonical after restore: the order armed at bar 1
    fills on the FIRST resumed bar (bar 2), at exactly the same price a
    never-stopped engine fills at."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        full = _frame()

        never = _build_engine(state_file, full)
        _run(never)
        # The full run's order ledger carries the exact entry fill (the
        # closed position zeroes its quantity, so the buy ORDER is the
        # reference for the size; the closed position keeps the entry price).
        never_buy = never.portfolio.filled_orders[0]
        never_position = never.portfolio.closed_positions[0]

        torn = _build_engine(state_file, full.iloc[:2])
        _run(torn)
        # At teardown the BUY is armed (submitted at bar 1, stepped once).
        assert len(torn.portfolio.pending_orders) == 1
        assert len(torn.executor._armed) == 1
        torn.state_manager.save_state(torn)

        restored = _build_engine(state_file, full)
        restored._running = True
        _run_one_bar(restored)

        position = restored.portfolio.positions["TEST"]
        assert float(position.quantity) == float(never_buy.quantity)
        assert position.average_entry_price == never_position.average_entry_price
        # The pending queue drained on the first resumed bar (filled, not
        # re-armed) — the restore did not push the fill one bar later.
        assert len(restored.executor._pending) == 0


def _run_one_bar(engine):
    """Process exactly the next unprocessed bar (bar index 2)."""
    symbol = engine.config.data.symbols[0]
    candles = engine.data_source.get_candles(
        symbol,
        engine.config.data.start_date,
        engine.config.data.end_date,
        engine.config.data.timeframe,
    )
    offset = engine._processed_bars.get(symbol, 0)
    assert offset == 2
    idx, row = candles.iloc[offset:].iterrows().__next__()
    bar = {
        "symbol": symbol,
        "timestamp": idx,
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": int(row["volume"]),
        "timeframe": engine.config.data.timeframe,
    }
    engine.data_handler.inject_bar(bar)
    assert engine.validator.validate(bar)
    sigs = engine.adapter.on_bar_close(bar)
    if sigs:
        engine._submit_orders(sigs, bar)
    engine.executor.step({symbol: engine._to_executor_bar(symbol, bar)})
    engine.portfolio.sync_orders()
    engine.portfolio.update_prices({symbol: bar["close"]})
    engine._loop_count += 1
    engine._processed_bars[symbol] = offset + 1


# ---------------------------------------------------------------------------
# State internals
# ---------------------------------------------------------------------------


def test_state_v3_payload_carries_executor_and_runtime():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        engine = _build_engine(state_file, _frame().iloc[:6])
        _run(engine)
        engine.state_manager.save_state(engine)

        payload = json.loads(state_file.read_text())
        assert payload["state_version"] == STATE_VERSION == 3
        assert "executor" in payload
        assert "engine_runtime" in payload
        assert payload["engine_runtime"]["loop_count"] == 6
        assert payload["engine_runtime"]["processed_bars"] == {"TEST": 6}
        # v3 keeps the v2 classification from T5.
        assert payload["mode"] == "paper"
        assert payload["source"] == "synthetic"


def test_v2_state_file_still_loads_without_executor_runtime():
    """Old v2 files (no executor/engine_runtime) still restore portfolio and
    adapter state; a settled-position resume then equals never-stopped."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        full = _frame()

        torn = _build_engine(state_file, full.iloc[:6])
        _run(torn)
        torn.state_manager.save_state(torn)

        # Downgrade the payload to the v2 shape (no executor/engine_runtime).
        payload = json.loads(state_file.read_text())
        payload["state_version"] = 2
        payload.pop("executor", None)
        payload.pop("engine_runtime", None)
        state_file.write_text(json.dumps(payload))

        never = _build_engine(state_file, full)
        _run(never)
        expected_equity = float(never.portfolio.calculate_total_equity())

        restored = _build_engine(state_file, full)
        # v2 runtime defaults: the replay restarts from bar 0, but the
        # portfolio + adapter state is restored, so a settled-position resume
        # converges to the same final P&L (transition signals are past).
        _run(restored)
        assert float(restored.portfolio.calculate_total_equity()) == pytest.approx(
            expected_equity, rel=1e-9
        )


def test_portfolio_order_ledger_roundtrips_through_json():
    """Portfolio.to_dict/from_dict keeps working + terminal orders (the
    order ledger is part of the state file now)."""
    pf = Portfolio(name="ledger", initial_capital=100_000)
    order = Order(
        symbol="TEST",
        side=OrderSide.BUY,
        quantity=100,
        order_type="MARKET",
        portfolio_id=pf.portfolio_id,
    )
    order.validate()
    order.submit()
    pf.add_order(order)

    blob = json.loads(json.dumps(pf.to_dict()))
    restored = Portfolio.from_dict(blob)
    assert len(restored.pending_orders) == 1
    assert restored.pending_orders[0].order_id == order.order_id
    assert restored.pending_orders[0].status == order.status
    assert len(restored.orders_for("TEST")) == 1


def test_executor_state_roundtrip_keeps_armed_timing():
    """An order that survived one step must fill on the FIRST bar after a
    restore (armed semantics preserved)."""
    portfolio = Portfolio(name="exec-rt", initial_capital=100_000)
    executor = free_executor(portfolio)

    order = Order(
        symbol="TEST",
        side=OrderSide.BUY,
        quantity=10,
        order_type="MARKET",
        portfolio_id=portfolio.portfolio_id,
    )
    order.validate()
    order.submit()
    portfolio.add_order(order)  # the portfolio tracks what the executor queues
    executor.submit(order)
    # One bar: arms only (no fill — canonical bar clock).
    results = executor.step(_bar(99.5))
    assert results == []
    assert len(executor._pending) == 1
    assert len(executor._armed) == 1

    state = executor.get_state()
    fresh = free_executor(portfolio)
    fresh.restore_state(state, orders=portfolio.pending_orders)
    assert len(fresh._pending) == 1
    assert len(fresh._armed) == 1
    # First bar after restore: the armed order FILLS at the open (no extra
    # arm bar) — this is the exact timing a restart must preserve.
    results = fresh.step(_bar(100.5))
    assert len(results) == 1
    assert results[0].fill is not None
    assert results[0].fill.fill_price == Decimal("100.5")
    # A never-armed order would need one more bar; the restored one did not.
    assert not fresh._pending
    assert not fresh._armed


def _bar(open_price):
    return Bar(
        open=open_price,
        close=open_price,
        volume=10_000,
        timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
    )
