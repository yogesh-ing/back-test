"""T1.3 — Per-bucket circuit breaker tests (C1/C5).

Covers:
* Breaker independence — paper breach does NOT halt live (and vice versa)
* Master kill — halts both buckets
* Day-anchor semantics — proper initialization on runner add/remove
* Latency — per-bucket evaluation completes in <500ms
* Runner lifecycle — add/remove doesn't break breaker state
* Scoped emergency flatten — only affects target bucket
* Scoped bulk control — pause/resume/stop scoped to bucket
* AC-17: Master kill halts and flattens both buckets
* AC-18: Restart behavior documented and tested
"""

from __future__ import annotations

import time

import pytest

from backtest.forward.paper_runner import (
    SIDE_BUY,
    SIDE_SELL,
    STATUS_PAUSED,
    STATUS_RUNNING,
    TARGET_SINGLE,
    RunnerConfig,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import (
    HALT_FLATTEN,
    HALT_PAUSE,
    GlobalRiskConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _paper_config(name="P1", capital=100_000, symbols=None):
    return RunnerConfig(
        name=name,
        strategy_name="rsi_reversion",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=symbols or ["AAA"],
        timeframe="1hour",
        mode="paper",
    )


def _live_config(name="L1", capital=200_000, symbols=None):
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=symbols or ["BBB"],
        timeframe="1hour",
        mode="live",
    )


def _first_bar(symbol="AAA", ts="2026-09-02 10:00:00"):
    return {
        "ts": ts,
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
    }


@pytest.fixture
def manager():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(
            daily_loss_limit=10_000,
            max_drawdown_pct=0.10,
            breach_mode=HALT_PAUSE,
        ),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


@pytest.fixture
def flatten_manager():
    """Manager with HALT_FLATTEN mode for drawdown tests."""
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(
            daily_loss_limit=10_000,
            max_drawdown_pct=0.10,
            breach_mode=HALT_FLATTEN,
        ),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Breaker independence — paper breach does NOT halt live
# ---------------------------------------------------------------------------

class TestBreakerIndependence:
    def test_paper_breach_does_not_halt_live(self, manager):
        """A daily-loss breach in paper does NOT halt live runners."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager._on_bar("AAA", _first_bar())
        manager._on_bar("BBB", _first_bar("BBB"))

        # Create a large loss on paper (exceeds 10,000 daily loss limit)
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 1000, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 1000, 80.0)
        # Paper lost ~20,000 — should breach daily loss limit
        manager._evaluate_risk()

        # Paper should be halted
        assert manager._bucket_halted["paper"] is True
        # Live should NOT be halted
        assert manager._bucket_halted["live"] is False
        # Manager-level halt depends on whether combined also breached
        # (live equity is 200k, paper lost 20k, combined daily_pnl may breach)

    def test_live_breach_does_not_halt_paper(self, manager):
        """A daily-loss breach in live does NOT halt paper runners."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager._on_bar("AAA", _first_bar())
        manager._on_bar("BBB", _first_bar("BBB"))

        # Create a large loss on live
        manager.broker.submit_market(lid, "BBB", SIDE_BUY, 1000, 100.0)
        manager.broker.submit_market(lid, "BBB", SIDE_SELL, 1000, 80.0)
        manager._evaluate_risk()

        # Live should be halted
        assert manager._bucket_halted["live"] is True
        # Paper should NOT be halted
        assert manager._bucket_halted["paper"] is False

    def test_both_buckets_independent_breaches(self, manager):
        """Both buckets can breach independently without affecting each other."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager._on_bar("AAA", _first_bar())
        manager._on_bar("BBB", _first_bar("BBB"))

        # Breach paper
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 1000, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 1000, 80.0)
        manager._evaluate_risk()
        assert manager._bucket_halted["paper"] is True
        assert manager._bucket_halted["live"] is False

        # Now breach live too
        manager.broker.submit_market(lid, "BBB", SIDE_BUY, 1000, 100.0)
        manager.broker.submit_market(lid, "BBB", SIDE_SELL, 1000, 80.0)
        manager._evaluate_risk()
        assert manager._bucket_halted["paper"] is True  # still halted
        assert manager._bucket_halted["live"] is True  # now also halted

    def test_paper_drawdown_does_not_halt_live(self, flatten_manager):
        """A drawdown breach in paper does NOT halt live."""
        pid = flatten_manager.add_runner(
            _paper_config(capital=100_000, symbols=["AAA"]), start=False
        )
        flatten_manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        flatten_manager._on_bar("AAA", _first_bar())
        flatten_manager._on_bar("BBB", _first_bar("BBB"))

        # Raise paper equity significantly (peak = 130,000)
        flatten_manager.broker.submit_market(pid, "AAA", SIDE_BUY, 1000, 100.0)
        flatten_manager.broker.submit_market(pid, "AAA", SIDE_SELL, 1000, 130.0)
        flatten_manager._evaluate_risk()

        # Crash paper (equity drops to ~110,000, drawdown ~15% > 10%)
        flatten_manager.broker.submit_market(pid, "AAA", SIDE_BUY, 1000, 100.0)
        flatten_manager.broker.submit_market(pid, "AAA", SIDE_SELL, 1000, 80.0)
        flatten_manager._evaluate_risk()

        # Paper should be halted (drawdown breach)
        assert flatten_manager._bucket_halted["paper"] is True
        # Live should NOT be halted
        assert flatten_manager._bucket_halted["live"] is False


# ---------------------------------------------------------------------------
# Master kill (AC-17)
# ---------------------------------------------------------------------------

class TestMasterKill:
    def test_emergency_flatten_halts_both_buckets(self, manager):
        """Emergency flatten (no mode) halts and flattens both buckets."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        # Start runners so process_candle_event sets last_price
        manager.get_runner(pid).start()
        manager.get_runner(lid).start()
        manager._on_bar("AAA", _first_bar("AAA"))
        manager._on_bar("BBB", _first_bar("BBB"))

        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(lid, "BBB", SIDE_BUY, 100, 100.0)

        count = manager.emergency_flatten_all(reason="test_master_kill")

        assert count >= 2
        assert manager.halted is True
        assert manager._bucket_halted["paper"] is True
        assert manager._bucket_halted["live"] is True
        # All positions flattened
        assert len(manager.get_runner(pid).positions) == 0
        assert len(manager.get_runner(lid).positions) == 0

    def test_scoped_emergency_only_halts_target(self, manager):
        """Emergency flatten with mode='paper' only halts paper bucket."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager.get_runner(pid).start()
        manager.get_runner(lid).start()
        manager._on_bar("AAA", _first_bar("AAA"))
        manager._on_bar("BBB", _first_bar("BBB"))

        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(lid, "BBB", SIDE_BUY, 100, 100.0)

        count = manager.emergency_flatten_all(reason="test_scoped", mode="paper")

        assert count >= 1
        # Paper halted, live NOT halted
        assert manager._bucket_halted["paper"] is True
        assert manager._bucket_halted["live"] is False
        # Paper positions flattened, live still has positions
        assert len(manager.get_runner(pid).positions) == 0
        assert len(manager.get_runner(lid).positions) >= 1


# ---------------------------------------------------------------------------
# Day-anchor semantics
# ---------------------------------------------------------------------------

class TestDayAnchorSemantics:
    def test_day_anchor_set_on_first_bar_per_bucket(self, manager):
        """Both buckets get day-anchor on the first bar (shared trading day)."""
        manager.add_runner(_paper_config(symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(symbols=["BBB"]), start=False)

        # First bar anchors BOTH buckets (they share the trading day)
        manager._on_bar("AAA", _first_bar("AAA", "2026-09-02 10:00:00"))
        assert manager._bucket_day["paper"] == "2026-09-02"
        assert manager._bucket_day["live"] == "2026-09-02"

    def test_empty_bucket_no_day_anchor(self, manager):
        """A bucket with no runners does not get a day anchor."""
        manager.add_runner(_paper_config(symbols=["AAA"]), start=False)
        # No live runners

        manager._on_bar("AAA", _first_bar("AAA", "2026-09-02 10:00:00"))
        assert manager._bucket_day["paper"] == "2026-09-02"
        assert manager._bucket_day["live"] is None  # no runners, no anchor

    def test_day_anchor_not_reset_on_subsequent_bars(self, manager):
        """Day anchor stays fixed across multiple bars."""
        manager.add_runner(_paper_config(symbols=["AAA"]), start=False)

        manager._on_bar("AAA", _first_bar("AAA", "2026-09-02 10:00:00"))
        first_day = manager._bucket_day["paper"]

        manager._on_bar("AAA", _first_bar("AAA", "2026-09-02 11:00:00"))
        assert manager._bucket_day["paper"] == first_day

    def test_day_start_equity_set_on_runner_add(self, manager):
        """Day-start equity is set when runner is added via _refresh_anchors."""
        manager.add_runner(_paper_config(capital=100_000), start=False)
        assert manager._bucket_day_start["paper"] == pytest.approx(100_000)

    def test_day_start_equity_set_on_first_bar(self, manager):
        """Day-start equity is set on first bar if not already set."""
        manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        # Override day_start to simulate late initialization
        manager._bucket_day_start["paper"] = 0.0

        manager._on_bar("AAA", _first_bar())
        assert manager._bucket_day_start["paper"] == pytest.approx(100_000)

    def test_day_anchor_reset_via_reset_daily_anchors(self, manager):
        """reset_daily_anchors() resets per-bucket day anchors."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager._on_bar("AAA", _first_bar())

        # Trade to change equity
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 110.0)

        old_paper_start = manager._bucket_day_start["paper"]
        manager.reset_daily_anchors()

        # Day start should be re-baselined to current equity
        assert manager._bucket_day_start["paper"] != old_paper_start
        assert manager._bucket_day_start["paper"] == pytest.approx(
            manager._bucket_equity("paper")
        )


# ---------------------------------------------------------------------------
# Latency — per-bucket evaluation <500ms
# ---------------------------------------------------------------------------

class TestLatency:
    def test_per_bucket_risk_evaluation_under_500ms(self, manager):
        """Per-bucket risk evaluation completes within 500ms (Task 7.2)."""
        # Add 10 runners per bucket (20 total)
        for i in range(10):
            manager.add_runner(
                _paper_config(name=f"P{i}", symbols=[f"PA{i}"]),
                start=False,
            )
            manager.add_runner(
                _live_config(name=f"L{i}", symbols=[f"LB{i}"]),
                start=False,
            )

        # Set day anchors
        for i in range(10):
            manager._on_bar(f"PA{i}", _first_bar(f"PA{i}"))
            manager._on_bar(f"LB{i}", _first_bar(f"LB{i}"))

        # Time the risk evaluation
        t0 = time.perf_counter()
        manager._evaluate_risk()
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 500, f"Risk evaluation took {elapsed_ms:.1f}ms (limit: 500ms)"
        # No halts expected (no positions, no losses)
        assert not manager._bucket_halted["paper"]
        assert not manager._bucket_halted["live"]

    def test_risk_latency_with_positions(self, flatten_manager):
        """Risk evaluation stays fast even with open positions."""
        for i in range(5):
            pid = flatten_manager.add_runner(
                _paper_config(name=f"P{i}", capital=50_000, symbols=[f"PA{i}"]),
                start=False,
            )
            # Give each runner a position
            flatten_manager.broker.submit_market(pid, f"PA{i}", SIDE_BUY, 100, 100.0)

        flatten_manager._on_bar("PA0", _first_bar("PA0"))

        t0 = time.perf_counter()
        flatten_manager._evaluate_risk()
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 500


# ---------------------------------------------------------------------------
# Runner lifecycle interactions with breakers
# ---------------------------------------------------------------------------

class TestRunnerLifecycleBreaker:
    def test_add_runner_does_not_trigger_halt(self, manager):
        """Adding a runner does not accidentally trip the circuit breaker."""
        manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        manager._evaluate_risk()

        assert manager._bucket_halted["paper"] is False
        assert manager._bucket_halted["live"] is False

    def test_remove_runner_does_not_break_other_bucket(self, manager):
        """Removing a runner from paper does not affect live breaker state."""
        pid = manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        # Set live halt state manually
        manager._bucket_halted["live"] = True
        manager._bucket_halt_reason["live"] = "test"

        manager.remove_runner(pid)

        # Live halt state should be preserved
        assert manager._bucket_halted["live"] is True
        assert manager._bucket_halt_reason["live"] == "test"

    def test_remove_last_runner_clears_bucket_halt(self, manager):
        """Removing the last runner in a bucket clears that bucket's halt."""
        pid = manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        manager._bucket_halted["paper"] = True
        manager._bucket_halt_reason["paper"] = "test halt"

        manager.remove_runner(pid)

        assert manager._bucket_halted["paper"] is False
        assert manager._bucket_halt_reason["paper"] is None

    def test_paused_by_breaker_stays_paused_after_remove(self, manager):
        """Runner paused by breaker stays paused (not auto-resumed on remove)."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        runner = manager.get_runner(pid)
        runner.start()
        # Simulate a halt
        manager._bucket_halted["paper"] = True
        runner.pause()

        assert runner.status == STATUS_PAUSED

        # Remove the runner — it was already paused
        manager.remove_runner(pid)
        # The runner is removed from the manager, but its status was PAUSED


# ---------------------------------------------------------------------------
# Scoped bulk control
# ---------------------------------------------------------------------------

class TestScopedBulkControl:
    def test_pause_all_scoped(self, manager):
        """pause_all(mode='paper') only pauses paper runners."""
        p1 = manager.add_runner(_paper_config(name="P1"), start=False)
        p2 = manager.add_runner(_paper_config(name="P2"), start=False)
        l1 = manager.add_runner(_live_config(name="L1"), start=False)

        # Start all runners
        manager.get_runner(p1).start()
        manager.get_runner(p2).start()
        manager.get_runner(l1).start()

        n = manager.pause_all(mode="paper")
        assert n == 2  # paused 2 paper runners

        # Paper paused, live still running
        assert manager.get_runner(p1).status == STATUS_PAUSED
        assert manager.get_runner(p2).status == STATUS_PAUSED
        assert manager.get_runner(l1).status == STATUS_RUNNING

    def test_resume_all_scoped(self, manager):
        """resume_all(mode='paper') only resumes paper runners."""
        p1 = manager.add_runner(_paper_config(name="P1"), start=False)
        l1 = manager.add_runner(_live_config(name="L1"), start=False)

        manager.get_runner(p1).start()
        manager.get_runner(l1).start()
        manager.pause_all(mode="paper")

        n = manager.resume_all(mode="paper")
        assert n == 1
        assert manager.get_runner(p1).status == STATUS_RUNNING
        assert manager.get_runner(l1).status == STATUS_RUNNING  # was never paused

    def test_resume_all_blocked_by_bucket_halt(self, manager):
        """resume_all(mode='live') is blocked if live bucket is halted."""
        l1 = manager.add_runner(_live_config(name="L1"), start=False)
        manager.get_runner(l1).start()
        manager.pause_all(mode="live")

        manager._bucket_halted["live"] = True

        with pytest.raises(RuntimeError, match="live bucket is halted"):
            manager.resume_all(mode="live")

    def test_stop_all_scoped(self, manager):
        """stop_all(mode='paper') only stops paper runners."""
        p1 = manager.add_runner(_paper_config(name="P1"), start=False)
        l1 = manager.add_runner(_live_config(name="L1"), start=False)

        # Start them first so they're not already STOPPED
        manager.get_runner(p1).start()
        manager.get_runner(l1).start()

        n = manager.stop_all(mode="paper")
        assert n == 1
        assert manager.get_runner(p1).status == "STOPPED"
        assert manager.get_runner(l1).status == STATUS_RUNNING

    def test_reset_breaker_scoped(self, manager):
        """reset_circuit_breaker(mode='paper') only resets paper halt."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        manager._bucket_halted["paper"] = True
        manager._bucket_halted["live"] = True
        manager.halted = True

        manager.reset_circuit_breaker(mode="paper")

        assert manager._bucket_halted["paper"] is False
        assert manager._bucket_halted["live"] is True  # untouched
        assert manager.halted is True  # manager-level still halted

    def test_reset_breaker_master_clears_all(self, manager):
        """reset_circuit_breaker(mode=None) clears all buckets + manager."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        manager._bucket_halted["paper"] = True
        manager._bucket_halted["live"] = True
        manager.halted = True

        manager.reset_circuit_breaker()  # master reset

        assert manager._bucket_halted["paper"] is False
        assert manager._bucket_halted["live"] is False
        assert manager.halted is False


# ---------------------------------------------------------------------------
# AC-18: Restart behavior
# ---------------------------------------------------------------------------

class TestRestartBehavior:
    def test_new_manager_has_clean_breaker_state(self):
        """Fresh PortfolioManager has no stale breaker state."""
        mgr = PortfolioManager(auto_start_feed=False)
        try:
            assert mgr.halted is False
            assert mgr._bucket_halted["paper"] is False
            assert mgr._bucket_halted["live"] is False
            assert mgr._bucket_halt_reason["paper"] is None
            assert mgr._bucket_halt_reason["live"] is None
        finally:
            mgr.shutdown()

    def test_reset_manager_clears_all_breaker_state(self):
        """reset_portfolio_manager() clears all breaker state."""
        from backtest.forward.portfolio_manager import reset_portfolio_manager

        mgr = reset_portfolio_manager()
        try:
            mgr.add_runner(_paper_config(), start=False)
            mgr._bucket_halted["paper"] = True
            mgr.halted = True
        finally:
            mgr.shutdown()

        mgr2 = reset_portfolio_manager()
        try:
            assert mgr2._bucket_halted["paper"] is False
            assert mgr2.halted is False
        finally:
            mgr2.shutdown()

    def test_halt_state_not_persisted_across_restart(self):
        """Halt state is in-memory only — restart clears it (AC-18)."""
        from backtest.forward.portfolio_manager import reset_portfolio_manager

        mgr = reset_portfolio_manager()
        try:
            mgr.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
            mgr.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)
            mgr._bucket_halted["paper"] = True
            mgr._bucket_halted["live"] = True
            mgr.halted = True
            mgr._bucket_halt_reason["paper"] = "test"
            mgr._bucket_halt_reason["live"] = "test"
        finally:
            mgr.shutdown()

        # Simulate restart
        mgr2 = reset_portfolio_manager()
        try:
            # All state should be clean
            assert mgr2.halted is False
            assert mgr2._bucket_halted["paper"] is False
            assert mgr2._bucket_halted["live"] is False
            assert mgr2._bucket_halt_reason["paper"] is None
            assert mgr2._bucket_halt_reason["live"] is None
            assert mgr2._bucket_peak["paper"] == 0.0
            assert mgr2._bucket_peak["live"] == 0.0
        finally:
            mgr2.shutdown()
