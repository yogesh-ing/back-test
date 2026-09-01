"""Ticket #8 — live end-to-end through the canonical engine path.

The hard gate: no real broker/feed is reachable here, so the live-only
branches are exercised with a DETERMINISTIC live simulator — a fake live
broker (``place_order`` + ``poll_fill``, canned REAL fills) and the engine's
streaming market-data handler (``inject_bar`` → ``get_latest_data``), the
same seams the production ``MStockBroker`` / ``MStockLiveFeed`` implement.

Proven end-to-end:
* fresh live start — boots empty, live broker fills, classified live
* streaming ``_new_bars`` dedupe (T7) against repeated bars
* resumed live — v3 state restore, SAME loop methods, armed order fills on
  the FIRST resumed live bar
* no silent classification downgrade (live never resurrects as paper;
  a paper engine never claims live)
* ``mode='live'`` flows config → state v3 → ``portfolios``/``fills`` rows
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from backtest.db.manager import DatabaseManager
from backtest.db.models import Portfolio as PortfolioRow
from backtest.forward.engine import ForwardTestingEngine
from backtest.simulator.fill_providers import BrokerFillProvider
from backtest.strategy.base import Strategy


# ---------------------------------------------------------------------------
# Deterministic live simulator
# ---------------------------------------------------------------------------


class FakeLiveBroker:
    """Duck-typed live broker: ``place_order`` + ``poll_fill``.

    Models a real venue honestly: polls return ``None`` while unexecuted;
    once the venue reports, the canned fill row is returned EXACTLY ONCE
    (a repeated row would over-fill the order — the executor treats every
    provider response as new execution).
    """

    def __init__(self, fill_row: dict | None, polls_before_fill: int = 1):
        self.fill_row = fill_row
        self.polls_before_fill = int(polls_before_fill)
        self.placed: list = []
        self.polled: list = []
        self._reported = False

    def place_order(self, order):
        self.placed.append(order)
        return f"BROKER-{len(self.placed)}"

    def poll_fill(self, broker_order_id):
        self.polled.append(broker_order_id)
        if self._reported or self.fill_row is None:
            return None
        if len(self.polled) <= self.polls_before_fill:
            return None
        self._reported = True
        return self.fill_row


class ThresholdStrategy(Strategy):
    name = ""
    params = {"threshold": 100}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "threshold_test"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return (candles["close"] > self.threshold).astype(int)


def _bar(bar_no: int, close: float, volume: int = 10_000):
    """Deterministic live bar, numbered 1-based (bar 1 = 2024-01-01)."""
    return {
        "symbol": "TEST",
        "timestamp": f"2024-01-{bar_no:02d}T09:15:00+05:30",
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": volume,
    }


# ---------------------------------------------------------------------------
# Engine fixture
# ---------------------------------------------------------------------------


def _live_engine(state_file: Path, broker: FakeLiveBroker, db: DatabaseManager | None = None):
    engine = ForwardTestingEngine(
        config_dict={
            "portfolio": {"name": "LiveRun", "initial_capital": 100_000},
            "strategy": {"name": "threshold_test"},
            "data": {
                "symbols": ["TEST"],
                "provider": "mock",  # streaming feed: inject_bar drives it
                "start_date": "2024-01-01",
                "end_date": "2026-01-01",
                "timeframe": "1day",
                "mode": "live",
                "source": "mstock",
            },
            # Ticket #9 — these tests pin LIVE PLUMBING (place-once, dedupe,
            # resume, classification), not risk caps. The default live bucket
            # would cap the 476-lot entry to ~95, so the fixture opts into a
            # permissive live bucket through the sanctioned explicit override
            # (risk.buckets.<bucket>); the bucket-risk boundary itself is
            # covered by tests/forward/test_bucket_risk.py.
            "risk": {
                "buckets": {
                    "live": {
                        "max_position_value": None,
                        "max_position_pct": None,
                        "max_gross_exposure_pct": None,
                        "max_open_positions": None,
                        "min_trade_value": None,
                    },
                },
            },
            "system": {
                "state_file": str(state_file),
                # 10ms poll cadence: a real loop cadence that isn't a CPU
                # spin while staying fast enough for deterministic tests.
                "loop_interval_seconds": 0.01,
                "backtest_mode": False,  # the LIVE loop (poll → dedupe → fill)
                "save_state_interval_minutes": 0,
                "dry_run": False,
            },
        },
        strategy=ThresholdStrategy(),
        broker=broker,
        db_manager=db,
    )
    engine.initialize_system()
    engine.adapter.min_bars = 2
    try:
        engine.validator.config.gap_detection_enabled = False
        engine.validator.config.spike_detection_enabled = False
        engine.data_handler.validator.config.gap_detection_enabled = False
        engine.data_handler.validator.config.spike_detection_enabled = False
    except Exception:  # noqa: BLE001
        pass
    return engine


def _run_live_until(engine, first_bar: int, n_bars: int, timeout: float = 15.0):
    """Drive the REAL ``run_loop`` in a thread, one bar per poll cycle.

    Each bar is injected and then the loop is given a moment to observe it
    (the live poll dedupes repeats, so advancing one bar at a time makes the
    per-bar decisions deterministic). Duplicates of every injected bar are
    also injected to exercise the streaming dedupe on every cycle.
    """
    engine._running = True
    thread = threading.Thread(target=engine.run_loop, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    for i in range(n_bars):
        bar_no = first_bar + i
        close = 99.0 if bar_no == 1 else 105.0  # exactly one 0→1 transition
        engine.data_handler.inject_bar(_bar(bar_no, close))
        # Wait until THIS bar was digested by the loop's dedupe (the adapter
        # appends exactly one row per NEW bar; repeats and re-polls add none).
        while time.monotonic() < deadline:
            bars = engine.adapter._bars.get("TEST")
            if bars is not None and len(bars) >= bar_no:
                break
            time.sleep(0.02)
        # A little extra time so the same bar is re-polled at least once
        # (dedupe exercise) and the executor can retry a working order.
        time.sleep(0.05)

    engine._running = False
    thread.join(timeout=5)
    assert not thread.is_alive(), "run_loop did not exit after _running=False"


# ---------------------------------------------------------------------------
# Fresh live start + live-only responsibilities
# ---------------------------------------------------------------------------


def test_fresh_live_start_uses_broker_fill_and_classifies_live():
    """Fresh live: broker fills (never simulated), portfolio/state live."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        broker = FakeLiveBroker(
            fill_row={"tradingsymbol": "TEST", "transaction_type": "BUY",
                      "quantity": 100, "price": 104.5, "brokerage": 20},
            polls_before_fill=1,
        )
        engine = _live_engine(state_file, broker)

        # Live-only: the executor carries the broker seam, never simulated.
        assert isinstance(engine.executor.fill_provider, BrokerFillProvider)
        assert engine.executor.fill_provider.broker is broker
        # Live classification on the FRESH portfolio.
        assert engine.portfolio.mode == "live"
        assert engine.portfolio.source == "mstock"

        _run_live_until(engine, first_bar=1, n_bars=4)
        engine.state_manager.save_state(engine)

        # The live broker was the ONLY fill path: placed once, polled.
        assert len(broker.placed) == 1
        assert len(broker.polled) >= 2
        position = engine.portfolio.positions["TEST"]
        # The broker's REAL (partial) fill of 100 is what the book holds —
        # not the simulated full size (476); the order remains working
        # (PARTIAL), so the broker id lives on the order.
        assert float(position.quantity) == 100
        assert engine.portfolio.orders_for("TEST")[0].broker_order_id == "BROKER-1"

        # state file: v3 + live classification.
        payload = json.loads(state_file.read_text())
        assert payload["state_version"] == 3
        assert payload["mode"] == "live"
        assert payload["source"] == "mstock"


def test_live_streaming_dedupe_holds_against_repeated_bars():
    """T7 dedupe against a STREAMING feed: the same bar repeated does not
    re-fire signals or orders; only new bars advance the clock."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        broker = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        engine = _live_engine(state_file, broker)

        _run_live_until(engine, first_bar=1, n_bars=4)

        # 4 distinct bars → exactly one 0→1 transition → one live order.
        assert len(broker.placed) == 1
        assert engine._last_bar_ts["TEST"] is not None
        # And no transition re-fire from the duplicated polls: the second
        # bar's signal history would have doubled on a dedupe leak.
        signals = engine.adapter.signal_history
        buy_signals = [s for s in signals if getattr(s, "action", None) is not None
                       and str(getattr(s, "action", "")).upper() == "BUY"]
        assert len(buy_signals) == 1


def test_live_order_stays_working_until_broker_reports_fill():
    """A live order is NOT simulated: until ``poll_fill`` returns a row the
    executor keeps it working and retries on the next live bar."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        broker = FakeLiveBroker(
            fill_row={"tradingsymbol": "TEST", "transaction_type": "BUY",
                      "quantity": 100, "price": 104.5},
            polls_before_fill=2,
        )
        engine = _live_engine(state_file, broker)
        _run_live_until(engine, first_bar=1, n_bars=6)

        assert len(broker.placed) == 1
        assert len(broker.polled) >= 3
        # By the final bar the broker reported the fill: the book shows the
        # REAL fill quantity (partial 100 → order still working) and no
        # simulated size was ever used.
        assert len(engine.portfolio.filled_orders) == 0  # still PARTIAL
        assert float(engine.portfolio.positions["TEST"].quantity) == 100
        assert engine.portfolio.orders_for("TEST")[0].broker_order_id == "BROKER-1"


# ---------------------------------------------------------------------------
# DB: mode='live' actually lands in portfolios/fills rows
# ---------------------------------------------------------------------------


def test_live_run_writes_live_rows_to_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        db = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
        db.connect()
        from backtest.db.models import Base

        Base.metadata.create_all(db.engine)

        broker = FakeLiveBroker(
            fill_row={"tradingsymbol": "TEST", "transaction_type": "BUY",
                      "quantity": 100, "price": 104.5, "brokerage": 20},
            polls_before_fill=1,
        )
        engine = _live_engine(state_file, broker, db=db)
        _run_live_until(engine, first_bar=1, n_bars=4)

        engine.portfolio.save_to_db(db)

        with db.session() as session:
            row = session.query(PortfolioRow).filter_by(name="LiveRun").one()
            assert row.mode == "live"
            assert row.source == "mstock"
            from backtest.db.models import Fill as FillRow, Order as OrderRow

            orders = session.query(OrderRow).filter_by(portfolio_id=row.portfolio_id).all()
            assert len(orders) == 1
            fills = session.query(FillRow).filter_by(order_id=orders[0].order_id).all()
            assert len(fills) == 1
            assert float(fills[0].fill_price) == 104.5


# ---------------------------------------------------------------------------
# Resumed live: v3 restore through the same loop; no downgrade
# ---------------------------------------------------------------------------


def test_resumed_live_reenters_same_loop_and_fills_armed_order_first_bar():
    """Teardown with the BUY armed (client-side only — the venue is first
    contacted at the first FILL attempt, so nothing was placed yet);
    restore → the SAME run_loop → the armed order is placed exactly once and
    fills on the first resumed live bar. Classification stays live."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"

        broker1 = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        torn = _live_engine(state_file, broker1)
        _run_live_until(torn, first_bar=1, n_bars=2)  # bar1: 99.0 (no signal), bar2: 105.0 (BUY)
        # The order is armed; no bar has arrived to attempt the fill, so the
        # venue has not seen it yet (client-side order only).
        assert len(broker1.placed) == 0
        assert len(torn.portfolio.pending_orders) == 1
        assert len(torn.executor._armed) == 1
        torn.state_manager.save_state(torn)

        payload = json.loads(state_file.read_text())
        assert payload["mode"] == "live"
        assert payload["source"] == "mstock"
        assert len(payload["executor"]["pending"]) == 1
        assert len(payload["executor"]["armed"]) == 1

        broker2 = FakeLiveBroker(
            fill_row={"tradingsymbol": "TEST", "transaction_type": "BUY",
                      "quantity": 100, "price": 104.5},
            polls_before_fill=0,  # fill on the FIRST poll after restore
        )
        restored = _live_engine(state_file, broker2)
        # No restore-specific branch: the same live loop resumes.
        assert restored.portfolio.mode == "live"
        assert restored.portfolio.source == "mstock"
        assert len(restored.portfolio.pending_orders) == 1
        assert len(restored.executor._armed) == 1

        _run_live_until(restored, first_bar=3, n_bars=2)  # bars 3 (place+fill), 4

        # Exactly ONE placement end-to-end (the resumed run's first attempt);
        # the armed order did not need an extra arm bar after restart.
        assert len(broker2.placed) == 1
        assert len(broker2.polled) >= 1
        assert broker2.placed[0].broker_order_id == "BROKER-1"
        assert len(restored.portfolio.filled_orders) == 0  # 100/476 → PARTIAL
        assert float(restored.portfolio.positions["TEST"].quantity) == 100
        assert restored.portfolio.orders_for("TEST")[0].broker_order_id == "BROKER-1"
        # Live classification survived the restart.
        assert restored.portfolio.mode == "live"
        assert restored.portfolio.source == "mstock"
        restored.state_manager.save_state(restored)
        assert json.loads(state_file.read_text())["mode"] == "live"


def test_live_state_never_resurrects_as_paper():
    """The no-downgrade guard: a LIVE config that restores a paper-tagged
    portfolio upgrades it rather than silently running paper."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        broker = FakeLiveBroker(fill_row=None, polls_before_fill=99)

        # A state file whose portfolio is stale paper/synthetic but whose
        # run classification (and engine config) is live.
        state_file.write_text(json.dumps({
            "state_version": 3,
            "mode": "live",
            "source": "mstock",
            "portfolio": {
                "name": "LiveRun",
                "initial_capital": "100000",
                "current_cash": "100000",
                "mode": "paper",
                "source": "synthetic",
                "limits": {},
                "positions": [],
                "closed_positions": [],
                "equity_history": [],
                "orders": [],
            },
            "adapter": {},
        }))

        engine = _live_engine(state_file, broker)
        assert engine.portfolio.mode == "live"
        assert engine.portfolio.source == "mstock"


def test_paper_engine_never_claims_live_on_restore():
    """A paper-config engine restoring a live-tagged portfolio reclassifies
    to paper (config authoritative) — never the reverse."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        state_file.write_text(json.dumps({
            "state_version": 3,
            "mode": "live",
            "source": "mstock",
            "portfolio": {
                "name": "LiveRun",
                "initial_capital": "100000",
                "current_cash": "100000",
                "mode": "live",
                "source": "mstock",
                "limits": {},
                "positions": [],
                "closed_positions": [],
                "equity_history": [],
                "orders": [],
            },
            "adapter": {},
        }))

        engine = ForwardTestingEngine(
            config_dict={
                "portfolio": {"name": "LiveRun", "initial_capital": 100_000},
                "strategy": {"name": "threshold_test"},
                "data": {"symbols": ["TEST"], "provider": "mock",
                         "mode": "paper", "source": "synthetic"},
                "system": {"state_file": str(state_file),
                           "loop_interval_seconds": 0},
            },
            strategy=ThresholdStrategy(),
        )
        engine.initialize_system()
        # Paper run: the portfolio must NOT claim live (config authoritative,
        # loud warning; no silent live claim).
        assert engine.portfolio.mode == "paper"
        assert engine.portfolio.source == "synthetic"
