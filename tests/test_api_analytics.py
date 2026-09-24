"""Unit and integration tests for Strategy Performance Analytics (/analytics)."""

import pytest

from backtest.api.analytics_service import AnalyticsService, compute_metrics_from_trades, get_health_rating
from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager
from backtest.web.app import create_app


@pytest.fixture()
def client():
    reset_portfolio_manager()
    return create_app(source="synthetic").test_client()


def test_compute_metrics_empty():
    res = compute_metrics_from_trades([], allocated_capital=100000.0)
    assert res["total_trades"] == 0
    assert res["total_pnl"] == 0.0
    assert res["sharpe_ratio"] == 0.0
    assert res["win_rate"] == 0.0


def test_compute_metrics_with_trades():
    sample_trades = [
        {"pnl": 1500.0, "exit_ts": "2026-09-01T10:00:00Z"},
        {"pnl": -500.0, "exit_ts": "2026-09-02T10:00:00Z"},
        {"pnl": 2000.0, "exit_ts": "2026-09-03T10:00:00Z"},
        {"pnl": -800.0, "exit_ts": "2026-09-04T10:00:00Z"},
        {"pnl": 1200.0, "exit_ts": "2026-09-05T10:00:00Z"},
    ]
    res = compute_metrics_from_trades(sample_trades, allocated_capital=100000.0)
    assert res["total_trades"] == 5
    assert res["total_pnl"] == 3400.0
    assert res["total_return_pct"] == 3.4
    assert res["winning_trades"] == 3
    assert res["losing_trades"] == 2
    assert res["win_rate"] == 60.0
    assert res["profit_factor"] == round(4700.0 / 1300.0, 2)
    assert res["streaks"]["max_win_streak"] >= 1


def test_health_rating():
    green = get_health_rating(sharpe=1.8, max_dd_pct=5.0, win_rate=55.0)
    assert green["status"] == "green"

    yellow = get_health_rating(sharpe=1.2, max_dd_pct=12.0, win_rate=48.0)
    assert yellow["status"] == "yellow"

    red = get_health_rating(sharpe=0.6, max_dd_pct=22.0, win_rate=40.0)
    assert red["status"] == "red"


def test_analytics_overview_empty_runners(client):
    resp = client.get("/api/analytics/overview")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "portfolio_metrics" in data
    assert "strategy_cards" in data
    assert data["total_runners"] == 0


def test_analytics_overview_and_strategy_detail(client):
    mgr = get_portfolio_manager()
    cfg = RunnerConfig(
        name="Test Scalper",
        strategy_name="rsi_reversion",
        symbols=["NIFTY"],
        allocated_capital=100000.0,
        mode="paper",
        source="synthetic",
    )
    inst_id = mgr.add_runner(cfg, start=True)

    # Check overview API
    resp = client.get("/api/analytics/overview")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["total_runners"] >= 1
    card = next(c for c in data["strategy_cards"] if c["instance_id"] == inst_id)
    assert card["name"] == "Test Scalper"
    assert card["strategy_name"] == "rsi_reversion"

    # Check strategy detail API
    detail_resp = client.get(f"/api/analytics/strategy/{inst_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.get_json()
    assert detail["success"] is True
    assert detail["instance_id"] == inst_id
    assert "metrics" in detail
    assert "equity_curve" in detail
    assert "monthly_breakdown" in detail
    assert "trade_distribution" in detail


def test_analytics_strategy_detail_not_found(client):
    resp = client.get("/api/analytics/strategy/non_existent_id")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["success"] is False
    assert "not found" in data["error"]


def test_analytics_html_page_renders(client):
    resp = client.get("/analytics")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Strategy Performance Analytics" in html
    assert "Portfolio Overview" in html
    assert "Key Performance Ratios" in html
