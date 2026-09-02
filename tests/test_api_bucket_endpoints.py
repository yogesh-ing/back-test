"""T1.4–T1.7 — API-level tests for bucket separation.

Covers:
* T1.4: SSE stream carries buckets + capability in every frame
* T1.5: Bulk control endpoints accept ?mode= for scoped operations
* T1.6: Emergency stop accepts mode= in request body for scoped flatten
* T1.7: /api/portfolio/buckets returns per-bucket aggregates
* AC-15: Scoped summary in API matches bucket aggregates
* AC-17: Master kill via API halts both buckets
"""

from __future__ import annotations

import json

import pytest

from backtest.forward.portfolio_manager import PortfolioManager, reset_portfolio_manager
from backtest.forward.paper_runner import (
    SIDE_BUY,
    TARGET_SINGLE,
    RunnerConfig,
)
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Create Flask test app with synthetic source."""
    application = create_app(
        source="synthetic",
        log_level="WARNING",
    )
    application.config["TESTING"] = True
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def manager(app):
    """Get the app's PortfolioManager and reset it after the test."""
    from backtest.api.portfolio import _manager
    mgr = _manager()
    mgr.shutdown()
    # Recreate clean
    mgr = reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


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


# ---------------------------------------------------------------------------
# T1.4: SSE stream carries buckets + capability
# ---------------------------------------------------------------------------

class TestSSEBuckets:
    def test_summary_api_includes_buckets(self, client, manager):
        """GET /api/portfolio/summary includes 'buckets' in response."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)

        resp = client.get("/api/portfolio/summary")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert "buckets" in data["portfolio"]
        assert "paper" in data["portfolio"]["buckets"]
        assert "live" in data["portfolio"]["buckets"]

    def test_summary_api_includes_capability(self, client, manager):
        """GET /api/portfolio/summary includes 'capability' in response."""
        manager.add_runner(_paper_config(), start=False)

        resp = client.get("/api/portfolio/summary")
        data = resp.get_json()
        assert "capability" in data["portfolio"]
        assert "broker_connected" in data["portfolio"]["capability"]
        assert "live_banner" in data["portfolio"]["capability"]

    def test_scoped_summary_matches_bucket_aggregate(self, client, manager):
        """Scoped summary metrics match bucket aggregates (AC-15)."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        manager._on_bar("AAA", _first_bar())
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 110.0)

        # Scoped summary
        resp_paper = client.get("/api/portfolio/summary?mode=paper")
        paper = resp_paper.get_json()["portfolio"]
        # Bucket aggregates
        resp_buckets = client.get("/api/portfolio/buckets")
        buckets = resp_buckets.get_json()["buckets"]

        assert paper["total_equity"] == pytest.approx(buckets["paper"]["equity"])
        assert paper["daily_pnl"] == pytest.approx(buckets["paper"]["daily_pnl"])


# ---------------------------------------------------------------------------
# T1.5: Scoped bulk control via API
# ---------------------------------------------------------------------------

class TestScopedBulkControlAPI:
    def test_pause_all_scoped_via_api(self, client, manager):
        """POST /api/portfolio/control/pause_all?mode=paper pauses only paper."""
        p1 = manager.add_runner(_paper_config(name="P1"), start=False)
        l1 = manager.add_runner(_live_config(name="L1"), start=False)
        manager.get_runner(p1).start()
        manager.get_runner(l1).start()

        resp = client.post("/api/portfolio/control/pause_all?mode=paper")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["affected"] == 1  # only paper runner paused

        # Verify via API
        summary = client.get("/api/portfolio/summary").get_json()["portfolio"]
        # Live should still be running
        live_runners = [r for r in summary["runners"] if r["mode"] == "live"]
        assert all(r["status"] == "RUNNING" for r in live_runners)

    def test_resume_all_scoped_via_api(self, client, manager):
        """POST /api/portfolio/control/resume_all?mode=paper resumes only paper."""
        p1 = manager.add_runner(_paper_config(name="P1"), start=False)
        l1 = manager.add_runner(_live_config(name="L1"), start=False)
        manager.get_runner(p1).start()
        manager.get_runner(l1).start()
        manager.pause_all(mode="paper")

        resp = client.post("/api/portfolio/control/resume_all?mode=paper")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["affected"] == 1

    def test_stop_all_scoped_via_api(self, client, manager):
        """POST /api/portfolio/control/stop_all?mode=paper stops only paper."""
        p1 = manager.add_runner(_paper_config(name="P1"), start=False)
        l1 = manager.add_runner(_live_config(name="L1"), start=False)
        manager.get_runner(p1).start()
        manager.get_runner(l1).start()

        resp = client.post("/api/portfolio/control/stop_all?mode=paper")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["affected"] == 1

    def test_invalid_mode_rejected(self, client, manager):
        """POST /api/portfolio/control/pause_all?mode=bogus returns 400."""
        resp = client.post("/api/portfolio/control/pause_all?mode=bogus")
        assert resp.status_code == 400

    def test_master_kill_via_api_halts_both(self, client, manager):
        """POST /api/portfolio/control/emergency_flatten halts both buckets (AC-17)."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager.get_runner(pid).start()
        manager.get_runner(lid).start()
        manager._on_bar("AAA", _first_bar("AAA"))
        manager._on_bar("BBB", _first_bar("BBB"))
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(lid, "BBB", SIDE_BUY, 100, 100.0)

        resp = client.post("/api/portfolio/control/emergency_flatten")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True

        # Both buckets halted
        summary = client.get("/api/portfolio/summary").get_json()["portfolio"]
        assert summary["halted"] is True
        assert summary["buckets"]["paper"]["halted"] is True
        assert summary["buckets"]["live"]["halted"] is True


# ---------------------------------------------------------------------------
# T1.6: Scoped emergency via API
# ---------------------------------------------------------------------------

class TestScopedEmergencyAPI:
    def test_emergency_stop_scoped_via_api(self, client, manager):
        """POST /api/portfolio/emergency_stop with mode=paper halts only paper."""
        pid = manager.add_runner(_paper_config(capital=100_000, symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(capital=200_000, symbols=["BBB"]), start=False)

        manager.get_runner(pid).start()
        manager.get_runner(lid).start()
        manager._on_bar("AAA", _first_bar("AAA"))
        manager._on_bar("BBB", _first_bar("BBB"))
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(lid, "BBB", SIDE_BUY, 100, 100.0)

        resp = client.post(
            "/api/portfolio/emergency_stop",
            json={"reason": "test", "mode": "paper"},
        )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["flattened_positions"] >= 1

        # Paper halted, live NOT halted
        summary = client.get("/api/portfolio/summary").get_json()["portfolio"]
        assert summary["buckets"]["paper"]["halted"] is True
        assert summary["buckets"]["live"]["halted"] is False

    def test_emergency_stop_invalid_mode(self, client, manager):
        """POST /api/portfolio/emergency_stop with invalid mode returns 400."""
        resp = client.post(
            "/api/portfolio/emergency_stop",
            json={"reason": "test", "mode": "bogus"},
        )
        assert resp.status_code == 400

    def test_reset_breaker_scoped_via_api(self, client, manager):
        """POST /api/portfolio/control/reset_breaker?mode=paper resets only paper."""
        manager.add_runner(_paper_config(), start=False)
        manager.add_runner(_live_config(), start=False)
        manager._bucket_halted["paper"] = True
        manager._bucket_halted["live"] = True
        manager.halted = True

        resp = client.post("/api/portfolio/control/reset_breaker?mode=paper")
        data = resp.get_json()
        assert resp.status_code == 200

        summary = client.get("/api/portfolio/summary").get_json()["portfolio"]
        assert summary["buckets"]["paper"]["halted"] is False
        assert summary["buckets"]["live"]["halted"] is True  # untouched


# ---------------------------------------------------------------------------
# T1.7: /api/portfolio/buckets endpoint
# ---------------------------------------------------------------------------

class TestBucketsEndpoint:
    def test_buckets_endpoint_returns_aggregates(self, client, manager):
        """GET /api/portfolio/buckets returns per-bucket aggregates."""
        manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        resp = client.get("/api/portfolio/buckets")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert "paper" in data["buckets"]
        assert "live" in data["buckets"]

        # Verify structure
        for mode in ("paper", "live"):
            b = data["buckets"][mode]
            assert "equity" in b
            assert "capital" in b
            assert "halted" in b
            assert "daily_pnl" in b
            assert "peak_equity" in b

    def test_buckets_matches_summary_buckets(self, client, manager):
        """GET /api/portfolio/buckets matches /api/portfolio/summary's buckets."""
        manager.add_runner(_paper_config(capital=100_000), start=False)
        manager.add_runner(_live_config(capital=200_000), start=False)

        resp_buckets = client.get("/api/portfolio/buckets").get_json()
        resp_summary = client.get("/api/portfolio/summary").get_json()

        for mode in ("paper", "live"):
            for key in ("equity", "capital", "halted", "daily_pnl", "peak_equity"):
                assert resp_buckets["buckets"][mode][key] == pytest.approx(
                    resp_summary["portfolio"]["buckets"][mode][key]
                )

    def test_buckets_empty_when_no_runners(self, client, manager):
        """GET /api/portfolio/buckets returns zero equity for empty buckets."""
        resp = client.get("/api/portfolio/buckets")
        data = resp.get_json()
        assert data["buckets"]["paper"]["equity"] == 0.0
        assert data["buckets"]["live"]["equity"] == 0.0
