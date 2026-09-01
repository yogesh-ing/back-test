"""API tests for the Portfolio Command Center (PRD Phase 5).

Covers REST endpoints + SSE stream using a Flask test client. The process-wide
portfolio manager is reset before each test so runs are independent.
"""

from __future__ import annotations

import json

import pytest

from backtest.forward.risk_supervisor import GlobalRiskConfig


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.web.app import create_app

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app(source="synthetic")
    app.config["PORTFOLIO_SSE_INTERVAL"] = 0.05
    with app.test_client() as c:
        yield c
    from backtest.forward.portfolio_manager import get_portfolio_manager

    get_portfolio_manager().shutdown()


# ---------------------------------------------------------------------------
# Summary / meta
# ---------------------------------------------------------------------------


def test_summary_empty(client):
    r = client.get("/api/portfolio/summary")
    assert r.status_code == 200
    p = r.get_json()["portfolio"]
    assert p["runner_count"] == 0
    assert p["halted"] is False
    assert "runners" in p


def test_universes_listed(client):
    r = client.get("/api/portfolio/universes")
    assert r.status_code == 200
    ids = [u["id"] for u in r.get_json()["universes"]]
    assert "NIFTY_50" in ids and "TOP_10_CRYPTO" in ids


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def test_create_single_runner(client):
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": "rsi_reversion",
            "target_type": "SINGLE_SYMBOL",
            "symbol": "BTC/USD",
            "timeframe": "1hour",
            "allocated_capital": 1_000_000,
        },
    )
    assert r.status_code == 201
    runner = r.get_json()["runner"]
    assert runner["status"] == "RUNNING"
    assert runner["target_type"] == "SINGLE_SYMBOL"
    assert runner["allocated_capital"] == 1_000_000


def test_create_pool_runner_via_universe(client):
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "name": "Swing",
            "strategy": "donchian_breakout",
            "target_type": "SYMBOL_UNIVERSE",
            "universe_id": "NIFTY_50",
            "allocated_capital": 2_500_000,
            "max_pool_positions": 5,
        },
    )
    assert r.status_code == 201
    runner = r.get_json()["runner"]
    assert runner["target_type"] == "SYMBOL_UNIVERSE"
    assert runner["symbol_count"] == 50


def test_create_pool_runner_accepts_universe_as_target(client):
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": "rsi_reversion",
            "target": "TOP_10_CRYPTO",
            "allocated_capital": 500_000,
        },
    )
    assert r.status_code == 201
    assert r.get_json()["runner"]["symbol_count"] == 10


def test_create_missing_strategy_400(client):
    r = client.post("/api/portfolio/runner/create", json={"symbol": "BTC/USD"})
    assert r.status_code == 400


def test_create_unknown_universe_400(client):
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": "rsi_reversion",
            "target_type": "SYMBOL_UNIVERSE",
            "universe_id": "MARS_INDEX",
            "allocated_capital": 100_000,
        },
    )
    assert r.status_code == 400


def test_create_bad_capital_400(client):
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": "rsi_reversion",
            "symbol": "BTC/USD",
            "allocated_capital": -100,
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


def _spawn(client, **overrides):
    body = {
        "strategy": "rsi_reversion",
        "target_type": "SINGLE_SYMBOL",
        "symbol": "BTC/USD",
        "allocated_capital": 100_000,
        "auto_start": False,
    }
    body.update(overrides)
    return client.post("/api/portfolio/runner/create", json=body).get_json()["instance_id"]


def test_pause_resume_stop_lifecycle(client):
    rid = _spawn(client, auto_start=True)
    r = client.post(f"/api/portfolio/runner/{rid}/control", json={"action": "pause"})
    assert r.get_json()["runner"]["status"] == "PAUSED"
    r = client.post(f"/api/portfolio/runner/{rid}/control", json={"action": "resume"})
    assert r.get_json()["runner"]["status"] == "RUNNING"
    r = client.post(f"/api/portfolio/runner/{rid}/control", json={"action": "stop"})
    assert r.get_json()["runner"]["status"] == "STOPPED"


def test_control_unknown_runner_404(client):
    r = client.post("/api/portfolio/runner/nope/control", json={"action": "pause"})
    assert r.status_code == 404


def test_deep_dive_returns_detail(client):
    rid = _spawn(client, auto_start=False)
    r = client.post(f"/api/portfolio/runner/{rid}/control", json={"action": "deep_dive"})
    assert r.status_code == 200
    detail = r.get_json()["runner"]
    for key in (
        "positions",
        "trades",
        "signals",
        "equity_curve",
        "params",
        "universe_symbols",
        "cash",
    ):
        assert key in detail


def test_get_runner_detail_endpoint(client):
    rid = _spawn(client, auto_start=False)
    r = client.get(f"/api/portfolio/runner/{rid}")
    assert r.status_code == 200
    assert r.get_json()["runner"]["instance_id"] == rid


def test_bulk_pause_and_resume(client):
    _spawn(client, auto_start=True, symbol="BTC/USD")
    _spawn(client, auto_start=True, symbol="ETH/USD")
    r = client.post("/api/portfolio/control/pause_all", json={})
    assert r.get_json()["affected"] == 2
    assert r.get_json()["portfolio"]["paused"] == 2
    r = client.post("/api/portfolio/control/resume_all", json={})
    assert r.get_json()["affected"] == 2


def test_emergency_stop_flattens_and_halts(client):
    rid = _spawn(client, auto_start=False, symbol="BTC/USD")
    # open a position directly through the manager, with a mark price set so
    # flatten can sell at last-known price.
    from backtest.forward.portfolio_manager import get_portfolio_manager

    mgr = get_portfolio_manager()
    runner = mgr.get_runner(rid)
    runner.last_price["BTC/USD"] = 100.0
    mgr.broker.submit_market(rid, "BTC/USD", "BUY", 10, 100.0)
    assert len(runner.positions) == 1
    r = client.post("/api/portfolio/emergency_stop", json={"reason": "test"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["flattened_positions"] >= 1
    assert body["portfolio"]["halted"] is True


# ---------------------------------------------------------------------------
# Circuit-breaker test endpoint
# ---------------------------------------------------------------------------


def test_test_breach_endpoint_trips_breaker(client):
    from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=10_000, max_drawdown_pct=0.05),
        warmup_bars=12,
        auto_start_feed=False,
    )
    rid = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": "donchian_breakout",
            "target_type": "SYMBOL_UNIVERSE",
            "symbols": ["A", "B", "C", "D"],
            "allocated_capital": 1_000_000,
            "max_pool_positions": 3,
        },
    ).get_json()["instance_id"]
    mgr = get_portfolio_manager()
    mgr.feed.warmup()
    for _ in range(5):
        mgr.tick()
    runner = mgr.get_runner(rid)
    for sym in ("A", "B", "C"):
        runner.last_price[sym] = 500.0
        mgr.broker.submit_market(rid, sym, "BUY", 100, 500.0)

    r = client.post("/api/portfolio/test/breach", json={"crash_pct": 0.4})
    assert r.status_code == 200
    assert r.get_json()["portfolio"]["halted"] is True


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


def test_sse_stream_emits_portfolio_frames(client):
    _spawn(client, auto_start=False)
    r = client.get("/api/portfolio/stream")
    assert r.status_code == 200
    assert r.content_type.startswith("text/event-stream")
    # Read the first couple of frames from the generator.
    frames = []
    for i, chunk in enumerate(r.iter_encoded()):
        frames.append(chunk)
        if i >= 3:
            break
    text = b"".join(frames).decode()
    assert "event: portfolio" in text
    assert "total_capital" in text


def test_portfolio_page_renders(client):
    r = client.get("/portfolio")
    assert r.status_code == 200
    assert b"Portfolio Command Center" in r.data
    assert b"matrix-body" in r.data
