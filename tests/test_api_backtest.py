"""PRD Task 6.3 — Backtest API endpoint tests."""

import pytest

from backtest.web.app import create_app


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


_VALID = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "timeframe": "1D",
    "from_date": "2021-01-01",
    "to_date": "2024-01-01",
    "capital": 100_000,
    "params": {"fast": 10, "slow": 30},
}


# --- single backtest --------------------------------------------------------


def test_run_valid_returns_full_shape(client):
    resp = client.post("/api/backtest/run", json=_VALID)
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"config", "metrics", "equity", "drawdown", "trades", "signals"}
    assert {"total_pnl", "win_rate_pct", "max_drawdown_pct", "sharpe",
            "total_trades"} <= set(body["metrics"])
    assert body["config"]["strategy"] == "sma_crossover"
    assert body["config"]["symbol"] == "DEMO"


def test_run_unknown_strategy_returns_400(client):
    body = dict(_VALID, strategy="nope")
    resp = client.post("/api/backtest/run", json=body)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_bad_dates_returns_400(client):
    body = dict(_VALID, from_date="2024-01-01", to_date="2021-01-01")
    resp = client.post("/api/backtest/run", json=body)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_missing_dates_returns_400(client):
    body = {k: v for k, v in _VALID.items() if k not in ("from_date", "to_date")}
    resp = client.post("/api/backtest/run", json=body)
    assert resp.status_code == 400


# --- parallel multi-slot ----------------------------------------------------


_MANY = {
    "shared": {"symbol": "DEMO", "from_date": "2021-01-01", "to_date": "2024-01-01",
               "capital": 100_000},
    "slots": [
        {"id": 1, "strategy": "sma_crossover", "timeframe": "1D", "params": {"fast": 10, "slow": 30}},
        {"id": 2, "strategy": "rsi_reversion", "timeframe": "1D", "params": {"period": 14}},
        {"id": 3, "strategy": "buy_and_hold", "timeframe": "1D", "params": {}},
        {"id": 4, "strategy": "donchian_breakout", "timeframe": "1D", "params": {"lookback": 20}},
    ],
}


def test_run_many_returns_all_slots(client):
    resp = client.post("/api/backtest/run-many", json=_MANY)
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert set(results) == {"1", "2", "3", "4"}
    for payload in results.values():
        assert "metrics" in payload and "equity" in payload


def test_run_many_broken_slot_isolated(client):
    body = dict(_MANY)
    body["slots"] = _MANY["slots"][:3] + [
        {"id": 4, "strategy": "broken_one", "timeframe": "1D", "params": {}}
    ]
    resp = client.post("/api/backtest/run-many", json=body)
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert "error" in results["4"]
    assert "metrics" in results["1"] and "metrics" in results["3"]


def test_run_many_too_many_slots_rejected(client):
    body = dict(_MANY)
    body["slots"] = [{"id": i, "strategy": "buy_and_hold", "timeframe": "1D"} for i in range(5)]
    resp = client.post("/api/backtest/run-many", json=body)
    assert resp.status_code == 400


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
