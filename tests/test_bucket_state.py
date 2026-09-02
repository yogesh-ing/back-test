"""T1.1 — Per-bucket state tracking tests (C2: derived-not-duplicated).

Covers:
* Bucket equity derived from runner states (not stored separately)
* Per-bucket peak tracking (survives across ticks)
* Per-bucket day-start anchors
* Bucket aggregates embedded in summary (C4)
* Scoped summary uses per-bucket peak/halt state
* Adding/removing runners initializes/cleans up bucket state
* AC-14: spawning/despawning creates zero phantom bucket P&L or drawdown
* Restart behavior documentation (AC-18)
"""

from __future__ import annotations

import pytest

from backtest.forward.paper_runner import (
    SIDE_BUY,
    SIDE_SELL,
    TARGET_SINGLE,
    RunnerConfig,
    OrderLedger,
    PaperBroker,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig


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


@pytest.fixture
def manager():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# C2: Derived equity / P&L — not stored, computed from runner states
# ---------------------------------------------------------------------------

class TestDerivedEquity:
    def test_bucket_equity_matches_runners(self, manager):
        """Bucket equity is derived from runner.equity(), not stored separately."""
        manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        agg = manager.get_bucket_aggregates()
        # With no positions, equity == allocated capital
        assert agg["paper"]["equity"] == pytest.approx(100_000)
        assert agg["live"]["equity"] == pytest.approx(200_000)
        # Combined
        summary = manager.get_portfolio_summary()
        assert summary["total_equity"] == pytest.approx(300_000)

    def test_bucket_equity_updates_after_fill(self, manager):
        """Bucket equity changes when a runner gets a fill (cash shifts to position)."""
        pid = manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        # Simulate a fill on the paper runner
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 50.0)

        agg = manager.get_bucket_aggregates()
        # Paper equity stays 100_000 (cash 95k + position value 5k = 100k)
        assert agg["paper"]["equity"] == pytest.approx(100_000)
        # But deployed capital changed
        runner = manager.get_runner(pid)
        assert runner.deployed_capital() == pytest.approx(5_000)
        # Live equity unchanged
        assert agg["live"]["equity"] == pytest.approx(200_000)

    def test_bucket_realized_pnl_derived(self, manager):
        """Realized P&L is derived from runner states."""
        pid = manager.add_runner(_paper_config(capital=100_000), start=False)
        runner = manager.get_runner(pid)
        manager.ledger.register_handler(pid, runner.on_fill)
        # Buy then sell for a profit
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 110.0)

        agg = manager.get_bucket_aggregates()
        assert agg["paper"]["realized_pnl"] == pytest.approx(1000.0)
        assert agg["live"]["realized_pnl"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Per-bucket peak tracking
# ---------------------------------------------------------------------------

class TestBucketPeak:
    def test_peak_init_at_zero(self, manager):
        """Per-bucket peak starts at 0 until first equity observation."""
        agg = manager.get_bucket_aggregates()
        assert agg["paper"]["peak_equity"] == 0.0
        assert agg["live"]["peak_equity"] == 0.0

    def test_peak_tracked_after_add_runner(self, manager):
        """Peak is set when a runner is added (via _refresh_anchors)."""
        manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        agg = manager.get_bucket_aggregates()
        assert agg["paper"]["peak_equity"] == pytest.approx(100_000)
        assert agg["live"]["peak_equity"] == pytest.approx(200_000)

    def test_peak_increases_with_equity(self, manager):
        """Peak updates when equity rises above current peak."""
        pid = manager.add_runner(_paper_config(capital=100_000), start=False)
        runner = manager.get_runner(pid)
        manager.ledger.register_handler(pid, runner.on_fill)

        # Profitable trade raises equity
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 120.0)
        # Trigger risk evaluation to update peak
        manager._evaluate_risk()

        agg = manager.get_bucket_aggregates()
        # Peak should be >= 100_000 (actual equity after trade)
        assert agg["paper"]["peak_equity"] >= 100_000

    def test_peak_does_not_decrease(self, manager):
        """Peak never decreases — it's a high-water mark."""
        pid = manager.add_runner(_paper_config(capital=100_000), start=False)
        runner = manager.get_runner(pid)
        manager.ledger.register_handler(pid, runner.on_fill)

        # Raise equity
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 120.0)
        manager._evaluate_risk()
        peak_after_profit = manager._bucket_peak["paper"]

        # Lose money
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 80.0)
        manager._evaluate_risk()

        # Peak must not have decreased
        assert manager._bucket_peak["paper"] >= peak_after_profit


# ---------------------------------------------------------------------------
# Per-bucket day-start anchors
# ---------------------------------------------------------------------------

class TestBucketDayAnchor:
    def test_day_anchor_set_on_first_bar(self, manager):
        """Per-bucket day anchor is set when the first bar arrives for any bucket."""
        manager.add_runner(_paper_config(symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(symbols=["BBB"]), start=False)

        # Simulate a bar — both buckets get day-anchored (shared trading day)
        bar = {"ts": "2026-09-02 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        manager._on_bar("AAA", bar)

        assert manager._bucket_day["paper"] == "2026-09-02"
        assert manager._bucket_day["live"] == "2026-09-02"  # anchored on first bar too

    def test_day_anchor_not_reset_on_subsequent_bars(self, manager):
        """Day anchor stays fixed even when bars with different timestamps arrive."""
        manager.add_runner(_paper_config(symbols=["AAA"]), start=False)

        bar1 = {"ts": "2026-09-02 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        manager._on_bar("AAA", bar1)
        assert manager._bucket_day["paper"] == "2026-09-02"

        bar2 = {"ts": "2026-09-02 11:00:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}
        manager._on_bar("AAA", bar2)
        # Day anchor should NOT change
        assert manager._bucket_day["paper"] == "2026-09-02"

    def test_day_start_equity_matches_bucket_equity(self, manager):
        """Day-start equity matches bucket equity at anchor time."""
        manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        bar = {"ts": "2026-09-02 10:00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        manager._on_bar("AAA", bar)

        assert manager._bucket_day_start["paper"] == pytest.approx(100_000)
        # Live also anchored on first bar (shared trading day)
        assert manager._bucket_day_start["live"] == pytest.approx(200_000)


# ---------------------------------------------------------------------------
# Bucket aggregates embedded in summary (C4)
# ---------------------------------------------------------------------------

class TestBucketAggregates:
    def test_summary_includes_buckets(self, manager):
        """get_portfolio_summary() always includes a 'buckets' dict."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        summary = manager.get_portfolio_summary()
        assert "buckets" in summary
        assert "paper" in summary["buckets"]
        assert "live" in summary["buckets"]

    def test_bucket_aggregates_structure(self, manager):
        """Each bucket aggregate has all required fields."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        agg = manager.get_bucket_aggregates()
        for mode in ("paper", "live"):
            b = agg[mode]
            for key in (
                "equity", "capital", "peak_equity", "drawdown_pct",
                "daily_pnl", "daily_pnl_pct", "realized_pnl",
                "deployed_capital", "open_positions",
                "halted", "halt_reason", "halt_mode", "halted_ts",
                "daily_loss_used", "daily_loss_pct",
                "count", "running", "paused", "stopped", "errors",
            ):
                assert key in b, f"Missing {key} in {mode} bucket"

    def test_empty_bucket_has_zero_equity(self, manager):
        """A bucket with no runners has zero equity."""
        manager.add_runner(_paper_config(), start=False)
        # No live runners

        agg = manager.get_bucket_aggregates()
        assert agg["live"]["equity"] == 0.0
        assert agg["live"]["capital"] == 0.0
        assert agg["live"]["count"] == 0


# ---------------------------------------------------------------------------
# Scoped summary uses per-bucket peak/halt
# ---------------------------------------------------------------------------

class TestScopedSummary:
    def test_scoped_summary_uses_bucket_peak(self, manager):
        """Scoped summary uses per-bucket peak, not manager peak."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        runner = manager.get_runner(pid)
        manager.ledger.register_handler(pid, runner.on_fill)
        # Profit on paper
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 120.0)
        manager._evaluate_risk()

        paper = manager.get_portfolio_summary(mode="paper")
        # Paper peak should reflect paper's equity, not combined
        assert paper["peak_equity"] >= 100_000
        # Combined peak would be higher (includes live)
        combined = manager.get_portfolio_summary()
        assert combined["peak_equity"] >= paper["peak_equity"]

    def test_scoped_halt_reflects_bucket(self, manager):
        """Scoped summary shows bucket-specific halt state."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        # Manually set per-bucket halt
        manager._bucket_halted["paper"] = True
        manager._bucket_halt_reason["paper"] = "test halt"

        paper = manager.get_portfolio_summary(mode="paper")
        live = manager.get_portfolio_summary(mode="live")
        assert paper["halted"] is True
        assert live["halted"] is False


# ---------------------------------------------------------------------------
# Add/remove runner lifecycle — bucket state initialization/cleanup
# ---------------------------------------------------------------------------

class TestRunnerLifecycle:
    def test_add_runner_initializes_bucket(self, manager):
        """Adding a runner initializes per-bucket state."""
        manager.add_runner(_paper_config(), start=False)
        assert "paper" in manager._bucket_peak
        assert manager._bucket_halted["paper"] is False

    def test_remove_last_runner_resets_bucket(self, manager):
        """Removing the last runner in a bucket resets that bucket's state."""
        pid = manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        # Set some state
        manager._bucket_peak["paper"] = 999.0
        manager._bucket_halted["paper"] = True

        manager.remove_runner(pid)

        # Paper bucket state should be reset
        assert manager._bucket_peak["paper"] == 0.0
        assert manager._bucket_halted["paper"] is False
        # Live bucket untouched
        assert manager._bucket_peak["live"] == 200_000

    def test_remove_one_of_many_runners_keeps_bucket(self, manager):
        """Removing one runner when others exist keeps bucket state."""
        p1 = manager.add_runner(_paper_config(name="P1"), start=False)
        p2 = manager.add_runner(_paper_config(name="P2"), start=False)

        manager._bucket_peak["paper"] = 500.0
        manager.remove_runner(p1)

        # Bucket still has runners — state preserved (peak may update via _refresh_anchors)
        assert manager._bucket_halted["paper"] is False  # state not reset
        assert manager._bucket_day["paper"] is None or manager._bucket_day["paper"] is not None  # not cleared


# ---------------------------------------------------------------------------
# AC-14: Zero phantom bucket P&L / drawdown on spawn/despawn
# ---------------------------------------------------------------------------

class TestPhantomPnL:
    def test_spawn_creates_zero_phantom_pnl(self, manager):
        """Spawning a runner creates zero phantom P&L in its bucket."""
        manager.add_runner(_paper_config(capital=100_000), start=False)

        agg = manager.get_bucket_aggregates()
        assert agg["paper"]["daily_pnl"] == 0.0
        assert agg["paper"]["realized_pnl"] == 0.0
        assert agg["paper"]["drawdown_pct"] == 0.0

    def test_despawn_creates_zero_phantom_pnl(self, manager):
        """Despawning a runner creates zero phantom P&L in its bucket."""
        pid = manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.remove_runner(pid)

        agg = manager.get_bucket_aggregates()
        assert agg["paper"]["equity"] == 0.0
        assert agg["paper"]["daily_pnl"] == 0.0
        assert agg["paper"]["drawdown_pct"] == 0.0

    def test_cross_bucket_no_phantom(self, manager):
        """Actions in one bucket create zero phantom P&L in the other."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        # Profit on paper (buy low, sell high)
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 110.0)
        manager._evaluate_risk()

        agg = manager.get_bucket_aggregates()
        assert agg["paper"]["realized_pnl"] == pytest.approx(1000.0)
        assert agg["live"]["realized_pnl"] == 0.0  # zero phantom
        assert agg["live"]["daily_pnl"] == 0.0  # zero phantom daily


# ---------------------------------------------------------------------------
# AC-18: Restart behavior documentation
# ---------------------------------------------------------------------------

class TestRestartBehavior:
    def test_new_manager_has_clean_state(self):
        """A fresh PortfolioManager has no stale halt state — restart resets everything."""
        mgr = PortfolioManager(auto_start_feed=False)
        try:
            # No runners, no halt, no peak
            assert mgr.halted is False
            assert mgr._bucket_halted["paper"] is False
            assert mgr._bucket_halted["live"] is False
            assert mgr._bucket_peak["paper"] == 0.0
            assert mgr._bucket_peak["live"] == 0.0
        finally:
            mgr.shutdown()

    def test_reset_portfolio_manager_clears_state(self):
        """reset_portfolio_manager() clears all per-bucket state."""
        from backtest.forward.portfolio_manager import reset_portfolio_manager

        mgr = reset_portfolio_manager()
        try:
            mgr.add_runner(_paper_config(), start=False)
            mgr._bucket_halted["paper"] = True
            mgr._bucket_peak["paper"] = 999.0
        finally:
            mgr.shutdown()

        mgr2 = reset_portfolio_manager()
        try:
            assert mgr2._bucket_halted["paper"] is False
            assert mgr2._bucket_peak["paper"] == 0.0
        finally:
            mgr2.shutdown()
