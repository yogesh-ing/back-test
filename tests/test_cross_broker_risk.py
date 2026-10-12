"""Phase D — cross-broker risk (PRD-001 §D, TEST-001).

* per-segment breakers: a breach in one segment halts ONLY that segment's
  runners (its broker's book) — the neighbour segment keeps trading;
* global breakers evaluate the SUM across all segments/brokers: a combined
  loss trips the master kill even when each segment is inside its own limit;
* segment latch reset via ``reset_circuit_breaker("segment:<name>")``;
* ``get_segment_aggregates()`` rollup for the risk page's per-broker cards.
"""

from __future__ import annotations

import textwrap

import pytest

from backtest.forward.paper_runner import (
    SIDE_BUY,
    SIDE_SELL,
    STATUS_PAUSED,
    TARGET_SINGLE,
    RunnerConfig,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import HALT_PAUSE, GlobalRiskConfig

SEGMENTS_YAML = textwrap.dedent(
    """
    segments:
      seg_a:
        display_name: "Segment A (mStock)"
        broker: mstock
        mode: live
        allocated_capital: 200000
        risk:
          daily_loss_limit: 10000
          max_drawdown_pct: 90
      seg_b:
        display_name: "Segment B (Dhan)"
        broker: dhan
        mode: live
        allocated_capital: 200000
        risk:
          daily_loss_limit: 10000
          max_drawdown_pct: 90
    data:
      primary: mstock
    """
)


@pytest.fixture(autouse=True)
def _segments(tmp_path, monkeypatch):
    from backtest.brokers.segments import reset_segments_config

    path = tmp_path / "segments.yaml"
    path.write_text(SEGMENTS_YAML, encoding="utf-8")
    monkeypatch.setenv("SEGMENTS_CONFIG_PATH", str(path))
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    reset_segments_config()
    yield
    reset_segments_config()


def _segment_config(name, segment, symbol, capital=200_000):
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=[symbol],
        timeframe="1hour",
        mode="live",
        segment=segment,
    )


def _bar(ts="2026-09-02 10:00:00"):
    return {"ts": ts, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}


def _manager(daily_loss_limit=1_000_000_000.0):
    """Manager whose GLOBAL/bucket breakers are wide open by default —
    per-segment limits (from segments.yaml) are the ones under test."""
    from live_test_support import ARMED_KWARGS

    return PortfolioManager(
        **ARMED_KWARGS,
        risk_config=GlobalRiskConfig(
            daily_loss_limit=daily_loss_limit,
            max_drawdown_pct=0.99,
            breach_mode=HALT_PAUSE,
        ),
        auto_start_feed=False,
    )


def _spawn_two_segments(mgr):
    aid = mgr.add_runner(_segment_config("A1", "seg_a", "AAA"), start=False)
    bid = mgr.add_runner(_segment_config("B1", "seg_b", "BBB"), start=False)
    mgr._on_bar("AAA", _bar())
    mgr._on_bar("BBB", _bar())
    return aid, bid


def _lose(mgr, iid, symbol, qty=1000, entry=100.0, exit_=80.0):
    """Realize a ~(entry-exit)*qty loss in one runner's book."""
    mgr.broker.submit_market(iid, symbol, SIDE_BUY, qty, entry)
    mgr.broker.submit_market(iid, symbol, SIDE_SELL, qty, exit_)


class TestSegmentBreakers:
    def test_segment_breach_halts_only_that_segment(self):
        """seg_a loses > its 10k limit → seg_a halts, seg_b keeps trading."""
        mgr = _manager()
        try:
            aid, bid = _spawn_two_segments(mgr)
            _lose(mgr, aid, "AAA")  # ~20k loss > seg_a's 10k limit
            mgr._evaluate_risk()

            assert mgr._bucket_halted["segment:seg_a"] is True
            assert mgr._bucket_halted.get("segment:seg_b", False) is False
            # The breach NEVER escalates: master kill untouched.
            assert mgr.halted is False
            # seg_b's runner was NOT touched by seg_a's halt (isolation) —
            # neither latch nor pause reached it.
            assert mgr.get_runner(bid).status != STATUS_PAUSED
            assert mgr._bucket_halt_reason["segment:seg_a"]
            assert "loss" in mgr._bucket_halt_reason["segment:seg_a"].lower()
        finally:
            mgr.shutdown()

    def test_segment_latch_reset(self):
        mgr = _manager()
        try:
            aid, _ = _spawn_two_segments(mgr)
            _lose(mgr, aid, "AAA")
            mgr._evaluate_risk()
            assert mgr._bucket_halted["segment:seg_a"] is True

            mgr.reset_circuit_breaker("segment:seg_a")
            assert mgr._bucket_halted["segment:seg_a"] is False
            assert mgr._bucket_halt_reason["segment:seg_a"] is None
        finally:
            mgr.shutdown()

    def test_master_reset_clears_segment_latches_too(self):
        mgr = _manager()
        try:
            aid, _ = _spawn_two_segments(mgr)
            _lose(mgr, aid, "AAA")
            mgr._evaluate_risk()
            assert mgr._bucket_halted["segment:seg_a"] is True

            mgr.reset_circuit_breaker(None)
            assert mgr._bucket_halted["segment:seg_a"] is False
        finally:
            mgr.shutdown()

    def test_segment_aggregates_rollup(self):
        mgr = _manager()
        try:
            aid, _ = _spawn_two_segments(mgr)
            _lose(mgr, aid, "AAA")
            mgr._evaluate_risk()

            agg = mgr.get_segment_aggregates()
            assert set(agg) == {"seg_a", "seg_b"}
            assert agg["seg_a"]["broker"] == "mstock"
            assert agg["seg_a"]["halted"] is True
            assert agg["seg_a"]["runner_count"] == 1
            assert agg["seg_b"]["broker"] == "dhan"
            assert agg["seg_b"]["halted"] is False
        finally:
            mgr.shutdown()


class TestGlobalSumBreaker:
    def test_combined_loss_trips_master_kill(self):
        """Each segment stays under its own 10k limit, but the SUM breaches
        the global 15k limit → EVERYTHING halts (PRD Phase D)."""
        mgr = _manager(daily_loss_limit=15_000)
        try:
            aid, bid = _spawn_two_segments(mgr)
            # ~9k loss each: under the per-segment 10k, 18k combined > 15k.
            _lose(mgr, aid, "AAA", qty=450, entry=100.0, exit_=80.0)
            _lose(mgr, bid, "BBB", qty=450, entry=100.0, exit_=80.0)
            mgr._evaluate_risk()

            # Segments individually inside their limits — no segment latch.
            assert mgr._bucket_halted.get("segment:seg_a", False) is False
            assert mgr._bucket_halted.get("segment:seg_b", False) is False
            # But the manager-level (cross-broker SUM) breaker fired.
            assert mgr.halted is True
            assert "loss" in (mgr.halt_reason or "").lower()
        finally:
            mgr.shutdown()
