"""Broker auth epic, Task 1.2 — ``MStockBroker`` tests (mocked mStock API).

PRD verification: login → verify_totp → get_session_status returns
``authenticated``. All HTTP is mocked; no test touches the real API.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
import requests

from backtest.brokers import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    MStockBroker,
)

FAKE_TOKEN = "tok-abcdef0123456789"


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture()
def broker(monkeypatch) -> MStockBroker:
    monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")
    monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")
    return MStockBroker()


def _mock_post(monkeypatch, responses: dict[str, Any] | None = None, calls: list | None = None):
    """Patch requests.post with per-endpoint canned responses.

    ``responses`` maps a URL substring (``login``/``verifytotp``) to a
    _FakeResponse or an Exception instance to raise. Every call is appended
    to ``calls`` as (url, data, headers).
    """
    responses = responses or {}

    def fake_post(url, data=None, headers=None, timeout=None):
        if calls is not None:
            calls.append((url, dict(data or {}), dict(headers or {})))
        for fragment, outcome in responses.items():
            if fragment in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return _FakeResponse(200, {"status": "success"})

    monkeypatch.setattr("requests.post", fake_post)


def _login_ok(monkeypatch, calls: list | None = None):
    _mock_post(monkeypatch, {"login": _FakeResponse(200, {"status": "success"})}, calls)


def _verify_ok(monkeypatch, calls: list | None = None):
    _mock_post(
        monkeypatch,
        {"verifytotp": _FakeResponse(200, {"access_token": FAKE_TOKEN})},
        calls,
    )


# ---------------------------------------------------------------------------
# PRD verification: login → verify_totp → authenticated
# ---------------------------------------------------------------------------


def test_prd_verification_flow_login_totp_authenticated(broker, monkeypatch):
    calls: list = []
    _login_ok(monkeypatch, calls)
    _verify_ok(monkeypatch, calls)

    assert broker.login("trader", "s3cret")["success"] is True
    assert broker.verify_totp("123456")["success"] is True

    status = broker.get_session_status()
    assert status["status"] == STATUS_AUTHENTICATED
    assert status["broker"] == "mstock"
    assert status["expires_at"]


# ---------------------------------------------------------------------------
# login()
# ---------------------------------------------------------------------------


def test_login_uses_typea_contract(broker, monkeypatch):
    calls: list = []
    _login_ok(monkeypatch, calls)

    result = broker.login("trader", "s3cret")
    assert result == {
        "success": True,
        "message": "Credentials verified — enter the code from your authenticator app",
        "requires_totp": True,
    }

    url, data, headers = calls[0]
    assert url == "https://api.mstock.test/openapi/typea/connect/login"
    assert data == {"Username": "trader", "Password": "s3cret"}
    assert headers["X-Mirae-Version"] == "1"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_login_never_stores_username_or_password(broker, monkeypatch):
    _login_ok(monkeypatch)
    broker.login("secret-user-42", "secret-pass-42")

    dumped = " ".join(repr(v) for v in vars(broker).values())
    assert "secret-user-42" not in dumped
    assert "secret-pass-42" not in dumped


def test_login_rejected_credentials(broker, monkeypatch):
    _mock_post(
        monkeypatch,
        {"login": _FakeResponse(401, {"error": "Invalid credentials"})},
    )
    result = broker.login("trader", "wrong")
    assert result["success"] is False
    assert result["requires_totp"] is False
    assert result["message"] == "Invalid credentials"
    assert broker._temp_auth_context is None


def test_login_error_payload_with_200(broker, monkeypatch):
    _mock_post(monkeypatch, {"login": _FakeResponse(200, {"status": "error"})})
    result = broker.login("trader", "pass")
    assert result["success"] is False
    assert broker._temp_auth_context is None


def test_login_network_error_returns_generic_message(broker, monkeypatch):
    _mock_post(monkeypatch, {"login": requests.ConnectionError("boom [internal dns trace]")})
    result = broker.login("trader", "pass")
    assert result["success"] is False
    assert "reach mStock" in result["message"]
    assert "boom" not in result["message"]  # no internals leaked to the browser


@pytest.mark.parametrize("username,password", [("", "p"), ("u", ""), (None, None)])
def test_login_requires_both_fields(broker, monkeypatch, username, password):
    calls: list = []
    _login_ok(monkeypatch, calls)
    result = broker.login(username, password)
    assert result["success"] is False
    assert calls == []  # never hits the API


def test_login_without_api_key_configured(broker, monkeypatch):
    monkeypatch.delenv("MSTOCK_API_KEY")
    result = broker.login("trader", "pass")
    assert result["success"] is False
    assert "MSTOCK_API_KEY" in result["message"]


# ---------------------------------------------------------------------------
# verify_totp()
# ---------------------------------------------------------------------------


def test_verify_totp_uses_typea_contract(broker, monkeypatch):
    calls: list = []
    _login_ok(monkeypatch, calls)
    _verify_ok(monkeypatch, calls)
    broker.login("trader", "s3cret")
    broker.verify_totp("654321")

    url, data, headers = calls[1]
    assert url == "https://api.mstock.test/openapi/typea/session/verifytotp"
    assert data == {"api_key": "test-api-key", "totp": "654321"}
    assert headers["X-Mirae-Version"] == "1"


def test_verify_totp_reads_nested_access_token(broker, monkeypatch):
    _login_ok(monkeypatch)
    _mock_post(
        monkeypatch,
        {"verifytotp": _FakeResponse(200, {"data": {"access_token": FAKE_TOKEN}})},
    )
    broker.login("trader", "s3cret")
    assert broker.verify_totp("123456")["success"] is True


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "12ab56", "abcdef"])
def test_verify_totp_rejects_bad_format_without_http_call(broker, monkeypatch, bad):
    calls: list = []
    _mock_post(monkeypatch, {}, calls)
    result = broker.verify_totp(bad)
    assert result["success"] is False
    assert result["expires_at"] == ""
    assert calls == []


def test_verify_totp_requires_prior_login(broker, monkeypatch):
    calls: list = []
    _verify_ok(monkeypatch, calls)
    result = broker.verify_totp("123456")
    assert result["success"] is False
    assert "Log in" in result["message"]
    assert calls == []


def test_verify_totp_wrong_code_keeps_temp_context_for_retry(broker, monkeypatch):
    _login_ok(monkeypatch)
    broker.login("trader", "s3cret")

    _mock_post(monkeypatch, {"verifytotp": _FakeResponse(401, {"error": "Invalid TOTP"})})
    result = broker.verify_totp("000000")
    assert result["success"] is False
    assert result["message"] == "Invalid TOTP"
    assert broker._temp_auth_context is not None  # retry allowed

    _verify_ok(monkeypatch)
    assert broker.verify_totp("111111")["success"] is True  # retry succeeds


def test_verify_totp_success_clears_temp_context_and_sets_expiry(broker, monkeypatch):
    _login_ok(monkeypatch)
    broker.login("trader", "s3cret")
    before = datetime.now()

    _verify_ok(monkeypatch)
    result = broker.verify_totp("123456")
    assert result["success"] is True

    assert broker._temp_auth_context is None
    assert broker._session_token == FAKE_TOKEN
    expires_at = datetime.fromisoformat(result["expires_at"])
    # Default TTL is 390 minutes (trading session), give or take test runtime.
    delta_minutes = (expires_at - before).total_seconds() / 60
    assert 385 <= delta_minutes <= 391


def test_verify_totp_prefers_server_provided_lifetime(broker, monkeypatch):
    _login_ok(monkeypatch)
    broker.login("trader", "s3cret")
    before = datetime.now()
    _mock_post(
        monkeypatch,
        {"verifytotp": _FakeResponse(200, {"access_token": FAKE_TOKEN, "expires_in": 600})},
    )
    result = broker.verify_totp("123456")
    delta_minutes = (datetime.fromisoformat(result["expires_at"]) - before).total_seconds() / 60
    assert 9 <= delta_minutes <= 11  # 600 s, not the 390 min default


def test_verify_totp_missing_token_in_response(broker, monkeypatch):
    _login_ok(monkeypatch)
    broker.login("trader", "s3cret")
    _mock_post(monkeypatch, {"verifytotp": _FakeResponse(200, {"status": "success"})})
    result = broker.verify_totp("123456")
    assert result["success"] is False
    assert "session token" in result["message"]


# ---------------------------------------------------------------------------
# get_session_status() lifecycle
# ---------------------------------------------------------------------------


def test_status_unauthenticated_before_any_login(broker):
    assert broker.get_session_status() == {
        "status": STATUS_UNAUTHENTICATED,
        "expires_at": None,
        "broker": "mstock",
    }


def test_status_full_lifecycle_transitions(broker, monkeypatch):
    _login_ok(monkeypatch)
    _verify_ok(monkeypatch)
    broker.login("trader", "s3cret")
    broker.verify_totp("123456")

    now = datetime.now()
    # Freshly authenticated → authenticated (expiry far in the future).
    broker._expires_at = now + timedelta(hours=2)
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED

    # 25 minutes left → expiring_soon (inside the 30-minute window).
    broker._expires_at = now + timedelta(minutes=25)
    assert broker.get_session_status()["status"] == STATUS_EXPIRING_SOON

    # Past expiry → expired.
    broker._expires_at = now - timedelta(minutes=1)
    assert broker.get_session_status()["status"] == STATUS_EXPIRED


def test_status_window_boundaries(broker, monkeypatch):
    _login_ok(monkeypatch)
    _verify_ok(monkeypatch)
    broker.login("trader", "s3cret")
    broker.verify_totp("123456")

    now = datetime.now()
    broker._expires_at = now + timedelta(minutes=30)  # exactly at the window edge
    assert broker.get_session_status()["status"] == STATUS_EXPIRING_SOON
    broker._expires_at = now + timedelta(minutes=31)
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED
    broker._expires_at = now  # exactly at expiry
    assert broker.get_session_status()["status"] == STATUS_EXPIRED


# ---------------------------------------------------------------------------
# Security: token / credential hygiene
# ---------------------------------------------------------------------------


def test_session_token_never_leaks_into_contract_responses(broker, monkeypatch):
    _login_ok(monkeypatch)
    _verify_ok(monkeypatch)
    broker.login("trader", "s3cret")
    verify = broker.verify_totp("123456")
    status = broker.get_session_status()

    for payload in (verify, status):
        assert FAKE_TOKEN not in json.dumps(payload)
    assert set(status) == {"status", "expires_at", "broker"}
    assert broker.get_session_token() == FAKE_TOKEN  # backend accessor only


def test_get_session_token_none_when_expired(broker, monkeypatch):
    _login_ok(monkeypatch)
    _verify_ok(monkeypatch)
    broker.login("trader", "s3cret")
    broker.verify_totp("123456")
    broker._expires_at = datetime.now() - timedelta(minutes=1)
    assert broker.get_session_token() is None


# ---------------------------------------------------------------------------
# logout()
# ---------------------------------------------------------------------------


def test_logout_clears_all_in_memory_state(broker, monkeypatch):
    _login_ok(monkeypatch)
    _verify_ok(monkeypatch)
    broker.login("trader", "s3cret")
    broker.verify_totp("123456")
    assert broker.get_session_token() == FAKE_TOKEN

    assert broker.logout() is None
    assert broker._session_token is None
    assert broker._expires_at is None
    assert broker._temp_auth_context is None
    assert broker.get_session_status()["status"] == STATUS_UNAUTHENTICATED
    assert broker.get_session_token() is None
