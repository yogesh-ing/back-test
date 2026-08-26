"""PRD Task 4.3 — Forward API endpoint tests.

Task 4.2 added a server-side auth guard to POST /api/forward/start:
without an authenticated broker session the endpoint returns 403.
The existing tests inject an authenticated stub broker so they keep passing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from backtest.api import forward as fwd
from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRING_SOON,
    STATUS_EXPIRED,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)
from backtest.brokers.session_manager import get_session_manager, reset_default_manager
from backtest.web.app import create_app


class _ForwardStubBroker(BrokerAuthBase):
    """Scriptable broker for forward-endpoint tests."""

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def __init__(self, status: str = STATUS_UNAUTHENTICATED) -> None:
        self._status = status
        self._expires_at: str | None = (
            (datetime.now() + timedelta(hours=2)).isoformat()
            if status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON)
            else None
        )

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self._status = STATUS_AUTHENTICATED
        self._expires_at = (datetime.now() + timedelta(hours=2)).isoformat()
        return {"success": True, "message": "", "expires_at": self._expires_at}

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "expires_at": self._expires_at,
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        return "tok" if self._status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON) else None

    def logout(self) -> None:
        self._status = STATUS_UNAUTHENTICATED
        self._expires_at = None

    # Helper for tests to change state
    def set_status(self, status: str) -> None:
        self._status = status
        if status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
            self._expires_at = (datetime.now() + timedelta(hours=2)).isoformat()
        elif status == STATUS_EXPIRED:
            self._expires_at = None
        else:
            self._expires_at = None


@pytest.fixture()
def stub_authenticated():
    """An authenticated stub broker (existing tests need this to pass the 403 guard)."""
    return _ForwardStubBroker(status=STATUS_AUTHENTICATED)


@pytest.fixture()
def client(stub_authenticated):
    """App client with an authenticated broker injected (for tests that call /start)."""
    reset_default_manager()
    get_session_manager().set_broker(stub_authenticated)
    app = create_app(source="synthetic")
    try:
        yield app.test_client()
    finally:
        reset_default_manager()


@pytest.fixture()
def client_unauthenticated():
    """App client with an UN-authenticated broker (for 403 guard tests)."""
    reset_default_manager()
    stub = _ForwardStubBroker(status=STATUS_UNAUTHENTICATED)
    get_session_manager().set_broker(stub)
    app = create_app(source="synthetic")
    try:
        yield app.test_client(), stub
    finally:
        reset_default_manager()


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


# ---------------------------------------------------------------------------
# Task 4.2 — server-side authentication guard
# ---------------------------------------------------------------------------


def test_start_without_auth_returns_403(client_unauthenticated):
    """No broker session → /start must return 403."""
    client, _ = client_unauthenticated
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "broker_not_authenticated"
    assert "Valid broker session required" in body["message"]


def test_start_with_expired_session_returns_403(client_unauthenticated):
    """Expired session → /start must return 403."""
    client, stub = client_unauthenticated
    stub.set_status(STATUS_EXPIRED)
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "broker_not_authenticated"


def test_start_with_authenticated_session_succeeds(client):
    """Authenticated session → /start should proceed normally."""
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "running"


def test_start_with_expiring_soon_session_succeeds(client_unauthenticated):
    """Expiring-soon session is still valid → /start should proceed."""
    client, stub = client_unauthenticated
    stub.set_status(STATUS_EXPIRING_SOON)
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "running"


def test_start_guard_runs_before_strategy_validation(client_unauthenticated):
    """Auth check happens first — even invalid strategy returns 403, not 400."""
    client, _ = client_unauthenticated
    bad_cfg = {**_CFG, "strategy": "nonexistent_strategy"}
    resp = client.post("/api/forward/start", json=bad_cfg)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "broker_not_authenticated"


def test_stop_does_not_require_auth(client_unauthenticated):
    """Stop endpoint does NOT require authentication (idempotent)."""
    client, _ = client_unauthenticated
    resp = client.post("/api/forward/stop")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "idle"


def test_status_does_not_require_auth(client_unauthenticated):
    """Status endpoint does NOT require authentication (polling)."""
    client, _ = client_unauthenticated
    resp = client.get("/api/forward/status")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "idle"
