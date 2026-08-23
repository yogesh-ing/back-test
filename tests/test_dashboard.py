"""Tests for Step 19: Real-Time Dashboard (backend logic)."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from backtest.simulator.portfolio import Portfolio, EquityPoint
from backtest.dashboard.data_provider import DashboardDataProvider
from backtest.dashboard.app import create_dashboard_app


def make_portfolio_with_data():
    portfolio = Portfolio(name="dashboard_test", initial_capital=500000)

    # Equity history
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i, eq in enumerate([100000, 101000, 102000, 101500, 103000]):
        ts = base + timedelta(days=i)
        point = EquityPoint(ts=ts, total_equity=Decimal(str(eq)), cash=Decimal(str(eq)), position_value=Decimal("0"))
        portfolio.equity_history.append(point)

    # Open positions – use prices that fit within capital
    portfolio.open_position("INFY", 100, 100)
    portfolio.open_position("TCS", 50, 200)
    portfolio.update_prices({"INFY": 102, "TCS": 210})

    # Closed positions as trades
    pos = portfolio.open_position("RELIANCE", 100, 150)
    pos.opened_at = base
    portfolio.reduce_position("RELIANCE", 100, 160)
    if portfolio.closed_positions:
        portfolio.closed_positions[-1].closed_at = base + timedelta(days=1)
        portfolio.closed_positions[-1].exit_reason = "signal"

    # Pending orders
    from backtest.simulator.order import Order

    order = Order(symbol="INFY", side="buy", quantity=10, order_type="limit", limit_price=90)
    order.submit()
    portfolio.add_order(order)

    return portfolio


def test_portfolio_overview():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    overview = provider.get_portfolio_overview()

    assert "total_equity" in overview
    assert "cash" in overview
    assert "position_value" in overview
    assert "total_pnl" in overview
    assert overview["total_equity"] > 0


def test_open_positions():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    positions = provider.get_open_positions()

    assert len(positions) == 2
    assert positions[0]["symbol"] in ["INFY", "TCS"]
    assert "unrealized_pnl" in positions[0]
    assert "age" in positions[0]


def test_recent_trades():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    trades = provider.get_recent_trades(limit=10)

    assert len(trades) == 1
    assert trades[0]["symbol"] == "RELIANCE"
    assert "is_winner" in trades[0]


def test_equity_curve():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    curve = provider.get_equity_curve(limit=10)

    assert "timestamps" in curve
    assert "equity" in curve
    assert len(curve["timestamps"]) == len(curve["equity"])
    assert len(curve["equity"]) == 5


def test_daily_pnl():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    daily = provider.get_daily_pnl(limit=10)

    assert "dates" in daily
    assert "pnl" in daily
    assert len(daily["dates"]) == len(daily["pnl"])


def test_drawdown_chart():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    dd = provider.get_drawdown_chart(limit=10)

    assert "timestamps" in dd
    assert "drawdown_pct" in dd


def test_win_loss_ratio():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    ratio = provider.get_win_loss_ratio()

    assert "winning" in ratio
    assert "losing" in ratio
    assert "win_rate" in ratio


def test_active_orders():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    orders = provider.get_active_orders()

    assert len(orders) == 1
    assert orders[0]["symbol"] == "INFY"
    assert orders[0]["status"] == "pending"


def test_key_metrics():
    portfolio = make_portfolio_with_data()

    from backtest.simulator.performance import PerformanceCalculator

    perf = PerformanceCalculator(portfolio=portfolio)
    perf.update_equity_curve()

    provider = DashboardDataProvider(portfolio=portfolio, performance=perf)

    metrics = provider.get_key_metrics()

    assert "total_trades_today" in metrics
    assert "win_rate" in metrics
    assert "sharpe_ratio" in metrics
    assert "max_drawdown" in metrics


def test_system_status():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    status = provider.get_system_status()

    assert "market_data_connected" in status
    assert "strategy_status" in status
    assert "system_health" in status


def test_all_dashboard_data():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    all_data = provider.get_all_dashboard_data()

    assert "portfolio_overview" in all_data
    assert "open_positions" in all_data
    assert "recent_trades" in all_data
    assert "equity_curve" in all_data
    assert "daily_pnl" in all_data
    assert "drawdown_chart" in all_data
    assert "win_loss_ratio" in all_data
    assert "active_orders" in all_data
    assert "key_metrics" in all_data
    assert "system_status" in all_data


def test_dashboard_app_creation():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    app = create_dashboard_app(provider=provider)

    assert app is not None

    # Test client
    client = app.test_client()

    # Index
    res = client.get("/")
    assert res.status_code == 200
    assert b"Forward Testing Dashboard" in res.data

    # API endpoints
    res = client.get("/api/portfolio")
    assert res.status_code == 200
    data = res.get_json()
    assert "total_equity" in data

    res = client.get("/api/positions")
    assert res.status_code == 200
    assert isinstance(res.get_json(), list)

    res = client.get("/api/trades")
    assert res.status_code == 200

    res = client.get("/api/orders")
    assert res.status_code == 200

    res = client.get("/api/metrics")
    assert res.status_code == 200

    res = client.get("/api/equity_curve")
    assert res.status_code == 200

    res = client.get("/api/all")
    assert res.status_code == 200
    all_data = res.get_json()
    assert "portfolio_overview" in all_data


def test_dashboard_control_endpoints():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    app = create_dashboard_app(provider=provider)
    client = app.test_client()

    # Control endpoints without engine should return no_engine
    res = client.post("/api/pause")
    assert res.status_code == 200
    data = res.get_json()
    assert "status" in data

    res = client.post("/api/resume")
    assert res.status_code == 200

    res = client.post("/api/stop")
    assert res.status_code == 200


def test_manual_order_endpoint():
    portfolio = make_portfolio_with_data()
    provider = DashboardDataProvider(portfolio=portfolio)

    app = create_dashboard_app(provider=provider)
    client = app.test_client()

    # Valid manual order
    res = client.post("/api/manual_order", json={"symbol": "INFY", "side": "buy", "quantity": 10, "order_type": "market"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "created"
    assert "order_id" in data

    # Invalid – missing symbol
    res = client.post("/api/manual_order", json={"quantity": 10})
    assert res.status_code == 400

    # Close position
    res = client.post("/api/close_position", json={"symbol": "INFY"})
    assert res.status_code == 200

    # Cancel order – need order_id
    orders = provider.get_active_orders()
    if orders:
        order_id = orders[0]["order_id"]
        res = client.post("/api/cancel_order", json={"order_id": order_id})
        assert res.status_code == 200


def test_empty_provider():
    provider = DashboardDataProvider()

    assert provider.get_portfolio_overview()["total_equity"] == 0
    assert provider.get_open_positions() == []
    assert provider.get_recent_trades() == []
    assert provider.get_active_orders() == []
