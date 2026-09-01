"""Broker auth epic, Task 2.1 — auth API endpoint tests.

Each endpoint is tested independently against a stub broker injected into
the session-manager singleton. PRD verification: confirm the session token
is absent from all response payloads (and the password from every response).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from backtest.brokers.base import STATUS_AUTHENTICATED, STATUS_UNAUTHENTICATED, BrokerAuthBase
from backtest.brokers.session_manager import get_session_manager, reset_default_manager
from backtest.web.app import create_app

SECRET_TOKEN = "tok-super-secret-9876"


class _ApiStubBroker(BrokerAuthBase):
    """Scriptable broker standing in for MStockBroker at the API layer."""

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def __init__(self) -> None:
        self._status = STATUS_UNAUTHENTICATED
        self._expires_at: str | None = None
        self.received_credentials: tuple[str, str] | None = None
        self.received_totp: list[str] = []
        self.logout_calls = 0
        self.login_result: dict[str, Any] = {
            "success": True,
            "message": "Credentials verified",
            "requires_totp": True,
        }
        self.verify_result: dict[str, Any] | None = None
        self.raise_on_login = False

    def login(self, username: str, password: str) -> dict[str, Any]:
        if self.raise_on_login:
            raise RuntimeError("boom: secret internals user=nope pass=nope")
        self.received_credentials = (username, password)
        return dict(self.login_result)

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self.received_totp.append(totp_code)
        if self.verify_result is not None:
            return dict(self.verify_result)
        self._status = STATUS_AUTHENTICATED
        self._expires_at = (datetime.now() + timedelta(hours=2)).isoformat()
        return {
            "success": True,
            "message": "session established",
            "expires_at": self._expires_at,
        }

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "expires_at": self._expires_at,
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        return SECRET_TOKEN if self._status == STATUS_AUTHENTICATED else None

    def logout(self) -> None:
        self.logout_calls += 1
        self._status = STATUS_UNAUTHENTICATED
        self._expires_at = None


@pytest.fixture()
def stub() -> _ApiStubBroker:
    return _ApiStubBroker()


@pytest.fixture()
def api(stub):
    """Fresh app + client with the stub broker as the active session broker."""
    reset_default_manager()
    get_session_manager().set_broker(stub)
    app = create_app(source="synthetic")
    try:
        yield app.test_client(), stub
    finally:
        reset_default_manager()


# ---------------------------------------------------------------------------
# GET /api/broker/status
# ---------------------------------------------------------------------------


def test_status_unauthenticated_shape(api):
    client, _ = api
    resp = client.get("/api/broker/status")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "status": "unauthenticated",
        "broker": "stub",
        "broker_display_name": "Stub Broker",
        "expires_at": None,
    }


def test_status_authenticated_after_full_flow(api):
    client, _ = api
    assert (
        client.post("/api/broker/login", json={"username": "u", "password": "p"}).status_code == 200
    )
    assert client.post("/api/broker/verify-totp", json={"totp_code": "123456"}).status_code == 200

    body = client.get("/api/broker/status").get_json()
    assert body["status"] == "authenticated"
    assert body["broker"] == "stub"
    assert body["expires_at"]


def test_status_endpoint_fails_closed_on_error(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(
        "backtest.brokers.session_manager.BrokerSessionManager.get_status",
        lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    body = client.get("/api/broker/status").get_json()
    assert body["status"] == "unauthenticated"  # start button stays disabled
    assert "boom" not in str(body)


# ---------------------------------------------------------------------------
# POST /api/broker/login
# ---------------------------------------------------------------------------


def test_login_passes_credentials_through_and_returns_contract(api):
    client, stub = api
    resp = client.post("/api/broker/login", json={"username": "trader", "password": "s3cret!"})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "success": True,
        "message": "Credentials verified",
        "requires_totp": True,
    }
    assert stub.received_credentials == ("trader", "s3cret!")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"username": "u"},
        {"password": "p"},
        {"username": "  ", "password": "p"},
        {"username": "u", "password": ""},
        {"username": 42, "password": "p"},
        None,
    ],
)
def test_login_malformed_body_returns_400(api, body):
    client, stub = api
    resp = client.post("/api/broker/login", json=body)
    assert resp.status_code == 400
    result = resp.get_json()
    assert result["success"] is False
    assert result["requires_totp"] is False
    assert stub.received_credentials is None  # broker never called


def test_login_rejected_credentials_propagated_as_flow_failure(api, stub):
    client, _ = api
    stub.login_result = {
        "success": False,
        "message": "Invalid username or password",
        "requires_totp": False,
    }
    resp = client.post("/api/broker/login", json={"username": "u", "password": "wrong"})
    assert resp.status_code == 200  # flow-level failure, not a transport error
    assert resp.get_json()["success"] is False


def test_login_unexpected_error_returns_generic_500(api, stub):
    client, _ = api
    stub.raise_on_login = True
    resp = client.post("/api/broker/login", json={"username": "u", "password": "p"})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["success"] is False
    assert body["message"] == "Internal server error"
    text = resp.get_data(as_text=True)
    assert "boom" not in text and "Traceback" not in text  # no internals to browser


# ---------------------------------------------------------------------------
# POST /api/broker/verify-totp
# ---------------------------------------------------------------------------


def test_verify_totp_returns_contract_and_passes_code(api):
    client, stub = api
    client.post("/api/broker/login", json={"username": "u", "password": "p"})
    resp = client.post("/api/broker/verify-totp", json={"totp_code": " 654321 "})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"success", "message", "expires_at"}
    assert body["success"] is True
    assert body["expires_at"]
    assert stub.received_totp == ["654321"]  # trimmed


@pytest.mark.parametrize("body", [{}, {"totp_code": ""}, {"totp_code": 123456}, None])
def test_verify_totp_malformed_body_returns_400(api, body):
    client, _ = api
    resp = client.post("/api/broker/verify-totp", json=body)
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_verify_totp_invalid_code_propagated(api, stub):
    client, _ = api
    stub.verify_result = {"success": False, "message": "Invalid TOTP code", "expires_at": ""}
    resp = client.post("/api/broker/verify-totp", json={"totp_code": "000000"})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is False


# ---------------------------------------------------------------------------
# POST /api/broker/logout
# ---------------------------------------------------------------------------


def test_logout_clears_session(api):
    client, stub = api
    client.post("/api/broker/login", json={"username": "u", "password": "p"})
    client.post("/api/broker/verify-totp", json={"totp_code": "123456"})

    resp = client.post("/api/broker/logout")
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert stub.logout_calls == 1
    assert client.get("/api/broker/status").get_json()["status"] == "unauthenticated"


# ---------------------------------------------------------------------------
# PRD security verification — token & password hygiene
# ---------------------------------------------------------------------------


def test_session_token_absent_from_every_response(api):
    client, _ = api
    responses = [
        client.get("/api/broker/status"),
        client.post("/api/broker/login", json={"username": "u", "password": "p"}),
        client.post("/api/broker/verify-totp", json={"totp_code": "123456"}),
        client.get("/api/broker/status"),
        client.post("/api/broker/logout"),
        client.get("/api/broker/status"),
    ]
    assert all(r.status_code == 200 for r in responses)
    for resp in responses:
        assert SECRET_TOKEN not in resp.get_data(as_text=True)


def test_password_never_returned_in_any_response(api):
    client, _ = api
    resp = client.post("/api/broker/login", json={"username": "trader", "password": "s3cret-pw-42"})
    assert resp.status_code == 200
    assert "s3cret-pw-42" not in resp.get_data(as_text=True)
    assert "s3cret-pw-42" not in client.get("/api/broker/status").get_data(as_text=True)


# ---------------------------------------------------------------------------
# App wiring (blueprint registration + Task 2.2 monitor autostart)
# ---------------------------------------------------------------------------


def test_create_app_starts_session_monitor(api):
    # Fixture already built the app; the singleton manager must be monitoring.
    manager = get_session_manager()
    assert manager._monitor_thread is not None
    assert manager._monitor_thread.is_alive()
    # reset_default_manager() in the fixture teardown stops it.


def test_broker_routes_registered_on_app():
    app = create_app(source="synthetic")
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert {
        "/api/broker/login",
        "/api/broker/verify-totp",
        "/api/broker/status",
        "/api/broker/logout",
    } <= rules
    reset_default_manager()
