"""Ticket #9 — per-bucket risk limits through the canonical engine path.

The core boundary: a paper bucket and a real-fills bucket with IDENTICAL
inputs must size differently, unless the config explicitly overrides the
bucket's limit. Plus: the source gate, the risk teeth behind the T8
no-downgrade guard, and bucket re-keying on restore.

NOTE (tox): tox runs ``pytest -k "not live"``, so no function name in this
module carries the substring "live" — node-id deselection would silently
drop these tests. Module name: ``test_bucket_risk.py`` (also safe).
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.simulator.errors import ValidationError
from forward.test_live_engine import (
    FakeLiveBroker,
    ThresholdStrategy,
    _bar,
    _run_live_until,
)


# ---------------------------------------------------------------------------
# Deterministic engine fixture (same shape as test_live_engine._live_engine)
# ---------------------------------------------------------------------------


def _make_engine(
    state_file: Path,
    mode: str,
    source: str,
    broker=None,
    risk_buckets=None,
    name: str = "BucketRun",
):
    config = {
        "portfolio": {"name": name, "initial_capital": 100_000},
        "strategy": {"name": "threshold_test"},
        "data": {
            "symbols": ["TEST"],
            "provider": "mock",  # streaming feed: inject_bar drives it
            "start_date": "2024-01-01",
            "end_date": "2026-01-01",
            "timeframe": "1day",
            "mode": mode,
            "source": source,
        },
        "system": {
            "state_file": str(state_file),
            "loop_interval_seconds": 0.01,
            "backtest_mode": False,
            "save_state_interval_minutes": 0,
            "dry_run": False,
        },
    }
    if risk_buckets is not None:
        config["risk"] = {"buckets": risk_buckets}

    from backtest.forward.engine import ForwardTestingEngine

    engine = ForwardTestingEngine(
        config_dict=config,
        strategy=ThresholdStrategy(),
        broker=broker,
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


# ---------------------------------------------------------------------------
# The boundary: identical inputs, different bucket => different risk
# ---------------------------------------------------------------------------


def test_paper_and_real_fills_size_identically_but_risk_differs():
    """Same 2 bars, same strategy, same 100k: the paper bucket sizes the
    full risk-based entry (476); the real-fills bucket caps it at the 10%
    position cap / 10k value cap (95)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        paper = _make_engine(tmp / "paper.json", "paper", "synthetic")
        broker = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        real = _make_engine(tmp / "real.json", "live", "mstock", broker=broker)

        # The portfolio limits themselves are bucket-keyed (never a global).
        assert paper.portfolio.limits.max_position_value is None
        assert paper.portfolio.limits.max_position_pct is None
        assert real.portfolio.limits.max_position_value == Decimal("10000.0000")
        assert real.portfolio.limits.max_position_pct == Decimal("0.10")
        assert real.portfolio.limits.max_open_positions == 5
        assert real.portfolio.limits.min_trade_value == Decimal("1000.0000")

        # Sizer constraints carry the same bucket caps.
        assert paper.sizer.config.constraints.max_position_value is None
        assert real.sizer.config.constraints.max_position_value == Decimal("10000.0000")

        _run_live_until(paper, first_bar=1, n_bars=2)
        _run_live_until(real, first_bar=1, n_bars=2)

        paper_qty = paper.portfolio.orders_for("TEST")[0].quantity
        real_qty = real.portfolio.orders_for("TEST")[0].quantity

        assert paper_qty == Decimal("476")  # raw risk-based entry (1% risk/2% stop)
        assert real_qty == Decimal("95")  # capped by the real-fills bucket
        assert paper_qty != real_qty


def test_explicit_bucket_override_wins_in_both_directions():
    """Config risk.buckets overrides the canonical bucket — a tightened
    paper bucket behaves like a capped one, and a loosened real-fills
    bucket sizes like free play."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Paper tightened via explicit override -> same 95 cap as the default
        # real-fills bucket.
        tight_paper = _make_engine(
            tmp / "tight-paper.json", "paper", "synthetic",
            risk_buckets={"paper": {
                "max_position_value": 10000, "max_position_pct": 0.10,
            }},
        )
        assert tight_paper.portfolio.limits.max_position_value == Decimal("10000.0000")

        # Real-fills loosened via explicit override -> the full 476 entry.
        broker = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        loose_real = _make_engine(
            tmp / "loose-real.json", "live", "mstock", broker=broker,
            risk_buckets={"live": {
                "max_position_value": None, "max_position_pct": None,
                "max_gross_exposure_pct": None, "max_open_positions": None,
                "min_trade_value": None,
            }},
        )
        assert loose_real.portfolio.limits.max_position_value is None

        _run_live_until(tight_paper, first_bar=1, n_bars=2)
        _run_live_until(loose_real, first_bar=1, n_bars=2)

        assert tight_paper.portfolio.orders_for("TEST")[0].quantity == Decimal("95")
        assert loose_real.portfolio.orders_for("TEST")[0].quantity == Decimal("476")


# ---------------------------------------------------------------------------
# Source gate (what can be traded), resolved where _classify resolves
# ---------------------------------------------------------------------------


def test_real_bucket_refuses_synthetic_data_before_any_trading():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "s.json"
        with pytest.raises(ValidationError, match="refuses source 'synthetic'"):
            _make_engine(state_file, "live", "synthetic")

        with pytest.raises(ValidationError, match="refuses source 'replay'"):
            _make_engine(state_file, "live", "replay")


# ---------------------------------------------------------------------------
# Risk teeth on the T8 downgrade guard
# ---------------------------------------------------------------------------


def test_misclassified_paper_book_refusing_real_run_raises():
    """A paper run with a 476-lot open book (approx 50k notional) is saved;
    a real-fills engine restoring it upgrades the label (T8) but the
    open book violates the real-fills bucket caps -> the run is REFUSED
    instead of silently trading at the wrong size."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "s.json"

        paper = _make_engine(state_file, "paper", "synthetic")
        _run_live_until(paper, first_bar=1, n_bars=4)
        assert float(paper.portfolio.positions["TEST"].quantity) > 0
        paper.state_manager.save_state(paper)

        payload = json.loads(state_file.read_text())
        assert payload["mode"] == "paper"
        assert payload["portfolio"]["mode"] == "paper"

        broker = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        with pytest.raises(ValidationError, match="RISK REFUSAL"):
            _make_engine(state_file, "live", "mstock", broker=broker)


def test_bucket_limits_rekeyed_on_restore_for_real_run():
    """A real-fills run restores with its limits re-keyed from the bucket
    (portfolio limits + sizer constraints), not from the state file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "s.json"

        broker = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        engine = _make_engine(state_file, "live", "mstock", broker=broker)
        _run_live_until(engine, first_bar=1, n_bars=2)
        engine.state_manager.save_state(engine)

        broker2 = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        restored = _make_engine(state_file, "live", "mstock", broker=broker2)

        assert restored.portfolio.mode == "live"
        assert restored.portfolio.source == "mstock"
        assert restored.portfolio.limits.max_position_value == Decimal("10000.0000")
        assert restored.portfolio.limits.max_position_pct == Decimal("0.10")
        assert restored.sizer.config.constraints.max_position_value == Decimal(
            "10000.0000"
        )


# ---------------------------------------------------------------------------
# Live-only risk check is wired where the bucket trades
# ---------------------------------------------------------------------------


class _RiskRecorder:
    """Records bucket pre-trade risk calls (duck-typed like the real manager)."""

    def __init__(self):
        self.calls = []

    def validate_order(self, order, current_price=None):
        self.calls.append((order, current_price))
        return type("_Allowed", (), {"allowed": True, "reason": ""})()


def test_bucket_pre_trade_risk_check_runs_for_real_fills_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        broker = FakeLiveBroker(fill_row=None, polls_before_fill=99)
        real = _make_engine(
            tmp / "real.json", "live", "mstock", broker=broker,
            risk_buckets={"live": {  # permissive: plumbing, not caps
                "max_position_value": None, "max_position_pct": None,
                "max_gross_exposure_pct": None, "max_open_positions": None,
                "min_trade_value": None,
            }},
        )
        recorder = _RiskRecorder()
        real.risk_manager = recorder
        _run_live_until(real, first_bar=1, n_bars=2)
        assert len(recorder.calls) >= 1
        order, price = recorder.calls[0]
        assert order.symbol == "TEST"

        paper = _make_engine(tmp / "paper.json", "paper", "synthetic")
        paper_recorder = _RiskRecorder()
        paper.risk_manager = paper_recorder
        _run_live_until(paper, first_bar=1, n_bars=2)
        assert paper_recorder.calls == []


# ---------------------------------------------------------------------------
# Canonical backtest runner resolves the paper bucket
# ---------------------------------------------------------------------------


def test_run_backtest_resolves_paper_bucket(monkeypatch):
    import pandas as pd

    from backtest.engine import backtest_runner

    idx = pd.date_range("2024-01-01", periods=30, freq="1D")
    candles = pd.DataFrame(
        {
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.5] * 30,
            "volume": [10000] * 30,
        },
        index=idx,
    )

    calls = []
    real_resolve = backtest_runner.resolve_bucket_risk

    def spy(mode, source, *args, **kwargs):
        calls.append((mode, source))
        return real_resolve(mode, source, *args, **kwargs)

    monkeypatch.setattr(backtest_runner, "resolve_bucket_risk", spy)
    result = backtest_runner.run_backtest(candles, "buy_and_hold", {}, "TEST", 100_000)
    assert calls and calls[0] == ("paper", "synthetic")
    assert result is not None
