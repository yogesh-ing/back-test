"""T1.2 — Scoped summary tests: flow semantics (C3) + capability flag (C6).

Covers:
* C3: Flow-aware P&L — scoped daily P&L uses per-bucket day-start anchor
* C3: Flow-aware drawdown — scoped drawdown uses per-bucket peak
* C3: Cross-bucket isolation — paper P&L does not leak into live summary
* C6: Capability flag — broker_connected, live_banner, paper_banner
* C6: Capability flag — graceful fallback when session manager unavailable
* AC-15: Live-page numbers === Overview's live-card numbers (single source)
"""

from __future__ import annotations

import pytest

from backtest.forward.paper_runner import (
    SIDE_BUY,
    SIDE_SELL,
    TARGET_SINGLE,
    RunnerConfig,
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


def _first_bar(symbol="AAA"):
    return {
        "ts": "2026-09-02 10:00:00",
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
    }


@pytest.fixture
def manager():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# C3: Flow-aware P&L — scoped daily P&L uses per-bucket day-start anchor
# ---------------------------------------------------------------------------

class TestFlowSemantics:
    def test_scoped_daily_pnl_uses_bucket_anchor(self, manager):
        """Scoped daily P&L = equity - bucket day-start, not combined day-start."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        # Set day anchors
        manager._on_bar("AAA", _first_bar())

        # Profit on paper
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 110.0)

        paper = manager.get_portfolio_summary(mode="paper")
        live = manager.get_portfolio_summary(mode="live")

        # Paper daily P&L should reflect ONLY paper's activity
        assert paper["daily_pnl"] == pytest.approx(1000.0)
        # Live daily P&L should be zero (no activity)
        assert live["daily_pnl"] == pytest.approx(0.0)

    def test_combined_daily_pnl_sums_buckets(self, manager):
        """Combined daily P&L = paper daily + live daily."""
        pid_p = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        pid_l = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager._on_bar("AAA", _first_bar())

        # Profit on both
        manager.broker.submit_market(pid_p, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid_p, "AAA", SIDE_SELL, 100, 110.0)
        manager.broker.submit_market(pid_l, "BBB", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid_l, "BBB", SIDE_SELL, 100, 105.0)

        paper = manager.get_portfolio_summary(mode="paper")
        live = manager.get_portfolio_summary(mode="live")
        combined = manager.get_portfolio_summary()

        assert paper["daily_pnl"] == pytest.approx(1000.0)
        assert live["daily_pnl"] == pytest.approx(500.0)
        assert combined["daily_pnl"] == pytest.approx(1500.0)

    def test_scoped_drawdown_uses_bucket_peak(self, manager):
        """Scoped drawdown uses per-bucket peak, not manager peak."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        manager._on_bar("AAA", _first_bar())

        # Raise paper equity, then lose some
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 120.0)
        manager._evaluate_risk()
        # Now paper equity is ~102000, peak is ~102000
        # Lose money
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 90.0)
        manager._evaluate_risk()

        paper = manager.get_portfolio_summary(mode="paper")
        # Paper drawdown should reflect paper's peak, not combined peak
        assert paper["drawdown_pct"] > 0  # some drawdown from paper peak
        assert paper["peak_equity"] >= 100_000  # peak is paper's high-water mark

    def test_cross_bucket_isolation(self, manager):
        """Paper P&L does not leak into live summary and vice versa."""
        pid_p = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        pid_l = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager._on_bar("AAA", _first_bar())

        # Only trade on paper
        manager.broker.submit_market(pid_p, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid_p, "AAA", SIDE_SELL, 100, 120.0)

        paper = manager.get_portfolio_summary(mode="paper")
        live = manager.get_portfolio_summary(mode="live")

        # Paper has realized P&L
        assert paper["realized_pnl"] == pytest.approx(2000.0)
        # Live has ZERO realized P&L (no phantom)
        assert live["realized_pnl"] == 0.0
        assert live["daily_pnl"] == 0.0


# ---------------------------------------------------------------------------
# C6: Capability flag
# ---------------------------------------------------------------------------

class TestCapabilityFlag:
    def test_summary_includes_capability(self, manager):
        """Summary always includes a 'capability' dict."""
        manager.add_runner(_paper_config(), start=False)
        summary = manager.get_portfolio_summary()
        assert "capability" in summary
        assert "broker_connected" in summary["capability"]
        assert "live_banner" in summary["capability"]
        assert "paper_banner" in summary["capability"]

    def test_broker_not_connected_by_default(self, manager):
        """Without a broker session, broker_connected is False."""
        manager.add_runner(_paper_config(), start=False)
        summary = manager.get_portfolio_summary()
        assert summary["capability"]["broker_connected"] is False
        assert summary["capability"]["live_banner"] == "Simulated fills"

    def test_paper_banner_always_simulated(self, manager):
        """Paper banner is always 'Simulated fills' regardless of broker status."""
        manager.add_runner(_paper_config(), start=False)
        summary = manager.get_portfolio_summary()
        assert summary["capability"]["paper_banner"] == "Simulated fills"

    def test_capability_in_scoped_summary(self, manager):
        """Capability flag appears in scoped summaries too."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        paper = manager.get_portfolio_summary(mode="paper")
        live = manager.get_portfolio_summary(mode="live")

        assert "capability" in paper
        assert "capability" in live
        # Both share the same process-level broker status
        assert paper["capability"]["broker_connected"] == live["capability"]["broker_connected"]

    def test_capability_graceful_fallback(self, manager, monkeypatch):
        """Capability degrades gracefully if session manager import fails."""
        def broken_import(*args, **kwargs):
            raise ImportError("broker module not loaded")
        monkeypatch.setattr(
            "backtest.brokers.session_manager.get_session_manager",
            broken_import,
        )
        # The real _check_broker_connected catches ImportError and returns False
        summary = manager.get_portfolio_summary()
        assert summary["capability"]["broker_connected"] is False
        assert summary["capability"]["live_banner"] == "Simulated fills"


# ---------------------------------------------------------------------------
# AC-15: Single source of truth — live-page numbers === overview live-card
# ---------------------------------------------------------------------------

class TestSingleSourceOfTruth:
    def test_scoped_summary_matches_bucket_aggregate(self, manager):
        """Scoped summary metrics match the corresponding bucket aggregate (AC-15)."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager._on_bar("AAA", _first_bar())

        # Trade on paper
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 110.0)
        manager._evaluate_risk()

        # Scoped summary for paper
        paper_summary = manager.get_portfolio_summary(mode="paper")
        # Bucket aggregates
        buckets = manager.get_bucket_aggregates()

        # Key metrics must match between scoped summary and bucket aggregate
        assert paper_summary["total_equity"] == pytest.approx(buckets["paper"]["equity"])
        assert paper_summary["daily_pnl"] == pytest.approx(buckets["paper"]["daily_pnl"])
        assert paper_summary["realized_pnl"] == pytest.approx(buckets["paper"]["realized_pnl"])
        assert paper_summary["peak_equity"] == pytest.approx(buckets["paper"]["peak_equity"])
        assert paper_summary["halted"] == buckets["paper"]["halted"]

    def test_live_page_numbers_match_overview_live_card(self, manager):
        """Live-page summary metrics === overview's live-card bucket aggregate (AC-15)."""
        manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        # Overview (combined) includes live-card data from buckets
        overview = manager.get_portfolio_summary()
        live_card = overview["buckets"]["live"]

        # Live page (scoped) uses the same source
        live_page = manager.get_portfolio_summary(mode="live")

        # These must be identical
        assert live_page["total_equity"] == pytest.approx(live_card["equity"])
        assert live_page["total_capital"] == pytest.approx(live_card["capital"])
        assert live_page["daily_pnl"] == pytest.approx(live_card["daily_pnl"])
        assert live_page["peak_equity"] == pytest.approx(live_card["peak_equity"])
        assert live_page["halted"] == live_card["halted"]
