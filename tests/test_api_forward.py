"""PRD Task 4.3 — Forward API endpoint tests."""

import pytest

from backtest.api import forward as fwd
from backtest.web.app import create_app


@pytest.fixture()
def client():
    app = create_app(source="synthetic")
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_forward_session():
    fwd._reset_session()
    yield
    fwd._reset_session()


_CFG = {
    "strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "1D",
    "from_date": "2024-01-01", "to_date": "2024-12-31",
    "capital": 10_000, "params": {"fast": 10, "slow": 30},
}


def test_status_idle_before_start(client):
    assert client.get("/api/forward/status").get_json()["status"] == "idle"


def test_start_valid_returns_running(client):
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "running"
    assert body["total"] > 50 and body["revealed"] <= body["total"]


def test_start_unknown_strategy_returns_400(client):
    resp = client.post("/api/forward/start", json={**_CFG, "strategy": "nope"})
    assert resp.status_code == 400 and "error" in resp.get_json()


def test_start_missing_dates_returns_400(client):
    body = {k: v for k, v in _CFG.items() if k not in ("from_date", "to_date")}
    assert client.post("/api/forward/start", json=body).status_code == 400


def test_status_shape_matches_adapter_plus_live_fields(client):
    client.post("/api/forward/start", json=_CFG)
    body = client.get("/api/forward/status").get_json()
    # adapter shape (reusable components) + forward-specific fields
    for key in ("metrics", "equity", "drawdown", "trades", "signals", "config",
                "positions", "progress", "status"):
        assert key in body
    assert body["status"] == "running"
    assert 0 <= body["progress"]["pct"] <= 100
    assert isinstance(body["positions"], list)
    assert {"total_pnl", "win_rate_pct", "sharpe", "total_trades"} <= set(body["metrics"])
    # config metadata is carried onto the live snapshot (used by the dashboard)
    assert body["config"]["strategy"] == "sma_crossover"
    assert body["config"]["symbol"] == "DEMO"


def test_status_advances_progress_then_completes(client):
    client.post("/api/forward/start", json=_CFG)
    seen = [client.get("/api/forward/status").get_json()["progress"]["pct"] for _ in range(80)]
    assert seen[-1] == 100.0                      # replay ran to completion
    assert seen[0] < seen[-1]                     # progress advanced
    final = client.get("/api/forward/status").get_json()
    assert final["status"] == "stopped"           # auto-stops at 100%


def test_stop_halts_progress(client):
    client.post("/api/forward/start", json=_CFG)
    first = client.get("/api/forward/status").get_json()["progress"]["pct"]
    assert client.post("/api/forward/stop").get_json()["status"] == "stopped"
    after = client.get("/api/forward/status").get_json()
    assert after["status"] == "stopped"
    # progress is frozen at the point of stopping (no further advance)
    assert client.get("/api/forward/status").get_json()["progress"]["pct"] == after["progress"]["pct"]
    assert after["progress"]["pct"] >= first


def test_status_survives_page_refresh(client):
    """Server-side state persists across requests (refresh-safe)."""
    client.post("/api/forward/start", json=_CFG)
    a = client.get("/api/forward/status").get_json()["progress"]["revealed"]
    client.get("/api/forward/status")  # simulate another poll (refresh)
    b = client.get("/api/forward/status").get_json()["progress"]["revealed"]
    assert b >= a > 0
