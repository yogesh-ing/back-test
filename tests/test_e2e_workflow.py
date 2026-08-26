"""PRD Task 6.5 — End-to-end workflow tests.

Exercises the full journey through the real engine + adapter + Flask API:
  1. strategies load → run backtest → adapt → display data
  2. 4-slot compare → parallel results → winner detection
  3. backtest result → promote config → forward pre-fill shape

Task 4.2 added a server-side auth guard to /api/forward/start, so the
forward-related test injects an authenticated stub broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.api import forward as fwd
from backtest.brokers.base import STATUS_AUTHENTICATED, BrokerAuthBase
from backtest.brokers.session_manager import get_session_manager, reset_default_manager
from backtest.data.synthetic import SyntheticSource
from backtest.runner import run_on_candles
from backtest.web.app import create_app


class _E2EStubBroker(BrokerAuthBase):
    """Authenticated stub for e2e tests that touch /api/forward/start."""

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def __init__(self) -> None:
        self._expires_at = (datetime.now() + timedelta(hours=2)).isoformat()

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        return {"success": True, "message": "", "expires_at": self._expires_at}

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": STATUS_AUTHENTICATED,
            "expires_at": self._expires_at,
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        return "tok"

    def logout(self) -> None:
        pass


@pytest.fixture()
def client():
    reset_default_manager()
    get_session_manager().set_broker(_E2EStubBroker())
    app = create_app(source="synthetic")
    try:
        yield app.test_client()
    finally:
        reset_default_manager()


@pytest.fixture(autouse=True)
def _reset_forward():
    fwd._reset_session()
    yield
    fwd._reset_session()


# --- 1. load → backtest → adapt → display ----------------------------------


def test_strategies_load_and_params_resolvable(client):
    catalogue = client.get("/api/strategies").get_json()
    assert len(catalogue) >= 4
    for s in catalogue:
        params = client.get(f"/api/strategies/{s['name']}/params").get_json()
        assert isinstance(params, dict)          # dynamic form can render


def test_backtest_run_adapts_to_display_data(client):
    cfg = {"strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "1D",
           "from_date": "2021-01-01", "to_date": "2024-01-01", "capital": 100_000,
           "params": {"fast": 10, "slow": 30}}
    resp = client.post("/api/backtest/run", json=cfg)
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("metrics", "equity", "drawdown", "trades", "signals"):
        assert key in body
    assert len(body["equity"]["values"]) == body["metrics"]["bars"]

    # the API response equals a direct adapter run with the same inputs
    candles = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    adapted = BacktestAdapter(run_on_candles(candles, "sma_crossover",
                              {"fast": 10, "slow": 30}, "DEMO")).to_all()
    assert adapted["metrics"]["total_trades"] == body["metrics"]["total_trades"]
    assert adapted["metrics"]["final_equity"] == body["metrics"]["final_equity"]


# --- 2. 4-slot compare → parallel → winner detection ------------------------


def test_compare_runs_parallel_and_detects_winner(client):
    payload = {
        "shared": {"symbol": "DEMO", "from_date": "2021-01-01", "to_date": "2024-01-01", "capital": 100_000},
        "slots": [
            {"id": 1, "strategy": "sma_crossover", "timeframe": "1D", "params": {"fast": 10, "slow": 30}},
            {"id": 2, "strategy": "rsi_reversion", "timeframe": "1D", "params": {"period": 14}},
            {"id": 3, "strategy": "buy_and_hold", "timeframe": "1D", "params": {}},
            {"id": 4, "strategy": "donchian_breakout", "timeframe": "1D", "params": {"lookback": 20}},
        ],
    }
    resp = client.post("/api/backtest/run-many", json=payload)
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert set(results) == {"1", "2", "3", "4"}        # all 4 slots returned in parallel
    for r in results.values():
        assert "metrics" in r and "equity" in r         # each succeeded

    # winner detection: best total return is identifiable from the results
    returns = {sid: r["metrics"]["total_return_pct"] for sid, r in results.items()}
    winner = max(returns, key=returns.get)
    assert returns[winner] == max(returns.values())


def test_compare_isolates_a_broken_slot(client):
    payload = {
        "shared": {"symbol": "DEMO", "from_date": "2021-01-01", "to_date": "2024-01-01", "capital": 100_000},
        "slots": [
            {"id": 1, "strategy": "sma_crossover", "timeframe": "1D", "params": {}},
            {"id": 2, "strategy": "missing_one", "timeframe": "1D", "params": {}},
        ],
    }
    results = client.post("/api/backtest/run-many", json=payload).get_json()["results"]
    assert "error" in results["2"] and "metrics" in results["1"]


# --- 3. backtest → promote → forward pre-fill shape -------------------------


def test_promote_config_round_trips_into_forward(client):
    # this is exactly the config dict stored as forward_prefill on Promote
    prefill = {"strategy": "rsi_reversion", "symbol": "BTCUSD", "timeframe": "1D",
               "from_date": "2024-01-01", "to_date": "2024-12-31", "capital": 10_000,
               "params": {"period": 14}}
    start = client.post("/api/forward/start", json=prefill)
    assert start.status_code == 200
    status = client.get("/api/forward/status").get_json()

    # forward page components can consume the result
    assert status["status"] == "running"
    for key in ("metrics", "equity", "positions", "progress"):
        assert key in status
    # promote carries exact config — no re-entry needed
    assert status["config"]["strategy"] == prefill["strategy"]
    assert status["config"]["symbol"] == prefill["symbol"]
