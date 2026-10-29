"""UpstoxBroker unit tests — auth contract compliance + security invariants.

Covers the two-step auth contract (BrokerAuthBase) with the Upstox OAuth
token endpoint mocked at the HTTP boundary:

* step 1 (login) validates the API key/secret shape + redirect-URI config
  and never calls the network;
* step 2 (verify_totp) exchanges an authorization code (parked by the OAuth
  callback, or pasted) for the access token and stores it;
* the API secret and the access token never leak into any return value;
* the callback park path is state-validated and single-use;
* status machine (unauthenticated/authenticated/expiring_soon/expired);
* 03:30 IST next-day session expiry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
import requests as _requests

from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
)
from backtest.brokers.upstox import UpstoxBroker

REDIRECT_URI = "http://127.0.0.1:5000/api/broker/upstox/callback"


def _ok_response(payload: dict[str, Any]) -> Any:
    class _Resp:
        status_code = 200
        ok = True
        content = b"{}"

        def json(self) -> dict[str, Any]:
            return payload

        @property
        def text(self) -> str:  # pragma: no cover — unused
            return ""

    return _Resp()


def _err_response(status_code: int, payload: dict[str, Any] | None) -> Any:
    class _Resp:
        ok = False
        content = b"{}"

        def json(self) -> dict[str, Any] | None:
            return payload

        @property
        def text(self) -> str:  # pragma: no cover — unused
            return ""

    setattr(_Resp, "status_code", status_code)
    return _Resp()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> UpstoxBroker:
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", REDIRECT_URI)
    return UpstoxBroker()


# ---------------------------------------------------------------------------
# Step 1 — login (app credentials parked, no network)
# ---------------------------------------------------------------------------


def test_login_success_parks_credentials_and_returns_auth_url(
    broker: UpstoxBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    with patch("backtest.brokers.upstox.requests.post") as post:
        result = broker.login("api-key-123456", "api-secret-123456")
    post.assert_not_called()  # step 1 must not hit the network
    assert result["success"] is True
    assert result["requires_totp"] is True
    auth_url = result["auth_url"]
    assert auth_url.startswith("https://api.upstox.com/v2/login/authorization/dialog?")
    assert "client_id=api-key-123456" in auth_url
    assert "response_type=code" in auth_url
    assert "state=" in auth_url
    # The secret must not appear anywhere in the response payload.
    assert "api-secret-123456" not in str(result)


def test_login_missing_fields_rejected(broker: UpstoxBroker) -> None:
    for username, password in (("api-key-123456", ""), ("", "api-secret-123456"), ("", "")):
        result = broker.login(username, password)
        assert result["success"] is False
        assert result["requires_totp"] is False


def test_login_whitespace_rejected(broker: UpstoxBroker) -> None:
    result = broker.login("api key 123456", "api-secret-123456")
    assert result["success"] is False
    assert "space" in result["message"]


def test_login_without_redirect_uri_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UPSTOX_REDIRECT_URI", raising=False)
    broker = UpstoxBroker()
    result = broker.login("api-key-123456", "api-secret-123456")
    assert result["success"] is False
    assert "UPSTOX_REDIRECT_URI" in result["message"]


# ---------------------------------------------------------------------------
# Step 2 — verify_totp (the actual token exchange)
# ---------------------------------------------------------------------------


def test_verify_totp_without_login_rejected(broker: UpstoxBroker) -> None:
    result = broker.verify_totp("some-auth-code-123")
    assert result["success"] is False
    assert "Log in" in result["message"]


def test_verify_totp_six_digit_totp_rejected(broker: UpstoxBroker) -> None:
    """Upstox has no TOTP endpoint — a 6-digit code is never valid here."""
    broker.login("api-key-123456", "api-secret-123456")
    result = broker.verify_totp("123456")
    assert result["success"] is False
    assert "authorization code" in result["message"]


def test_verify_totp_success_stores_token_and_clears_secret(
    broker: UpstoxBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker.login("api-key-123456", "api-secret-123456")
    captured: dict[str, Any] = {}

    def _fake_post(url: str, data: dict | None = None, headers: dict | None = None, **kw: Any) -> Any:
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers or {}
        return _ok_response({"access_token": "upstox-jwt-token", "expires_in": 3600})

    monkeypatch.setattr("backtest.brokers.upstox.requests.post", _fake_post)
    result = broker.verify_totp("auth-code-from-redirect-123456")

    assert result["success"] is True
    assert "upstox-jwt-token" not in str(result)  # token never leaves the backend
    assert captured["data"]["code"] == "auth-code-from-redirect-123456"
    assert captured["data"]["client_id"] == "api-key-123456"
    assert captured["data"]["client_secret"] == "api-secret-123456"
    assert captured["data"]["redirect_uri"] == REDIRECT_URI
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["url"].endswith("/v2/login/authorization/token")
    assert captured["headers"]["Api-Version"] == "2.0"
    # Secret discarded after the call; client id (not a secret) is kept.
    assert broker._temp_api_secret is None
    assert broker._temp_api_key is None
    assert broker._temp_auth_context is None
    # Session live
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED
    assert broker.get_session_token() == "upstox-jwt-token"


def test_verify_totp_rejected_keeps_context_for_retry(broker: UpstoxBroker) -> None:
    broker.login("api-key-123456", "api-secret-123456")

    def _fake_post(url: str, **kw: Any) -> Any:
        return _err_response(401, {"errors": [{"message": "Invalid authorization code"}]})

    with patch("backtest.brokers.upstox.requests.post", _fake_post):
        result = broker.verify_totp("dead-auth-code-123456")

    assert result["success"] is False
    assert result["message"] == "Invalid authorization code"
    assert broker.get_session_status()["status"] == STATUS_UNAUTHENTICATED
    # temp context kept so the user can redo the browser round trip
    assert broker._temp_api_secret is not None
    # the rejected code is single-use upstream — dropped, not retried
    assert broker._parked_auth_code is None


def test_verify_totp_network_error_user_facing(broker: UpstoxBroker) -> None:
    broker.login("api-key-123456", "api-secret-123456")

    def _fake_post(url: str, **kw: Any) -> Any:
        raise _requests.ConnectionError("boom")

    with patch("backtest.brokers.upstox.requests.post", _fake_post):
        result = broker.verify_totp("auth-code-from-redirect-123456")
    assert result["success"] is False
    assert "Could not reach Upstox" in result["message"]


# ---------------------------------------------------------------------------
# OAuth callback parking (state-validated, single-use)
# ---------------------------------------------------------------------------


def test_park_auth_code_requires_pending_login() -> None:
    ok, message = UpstoxBroker().park_auth_code("auth-code-123456", "state")
    assert ok is False
    assert "No Upstox login" in message


def test_park_auth_code_state_mismatch_rejected(broker: UpstoxBroker) -> None:
    broker.login("api-key-123456", "api-secret-123456")
    ok, message = broker.park_auth_code("auth-code-123456", "wrong-state")
    assert ok is False
    assert "State mismatch" in message
    assert broker._parked_auth_code is None


def test_park_auth_code_happy_path_parks_then_consumes(
    broker: UpstoxBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = broker.login("api-key-123456", "api-secret-123456")
    state = broker._temp_auth_context["state"]  # type: ignore[index]
    ok, _ = broker.park_auth_code("auth-code-from-redirect-123456", state)
    assert ok is True
    assert broker._parked_auth_code == "auth-code-from-redirect-123456"

    monkeypatch.setattr(
        "backtest.brokers.upstox.requests.post",
        lambda url, **kw: _ok_response({"access_token": "tok"}),
    )
    result = broker.verify_totp("anything-typed")  # parked code wins
    assert result["success"] is True
    assert broker._parked_auth_code is None  # single use
    # the authorize URL carried the state that the callback then matched
    assert f"state={state}" in login["auth_url"]


def test_park_auth_code_bad_shape_rejected(broker: UpstoxBroker) -> None:
    broker.login("api-key-123456", "api-secret-123456")
    state = broker._temp_auth_context["state"]  # type: ignore[index]
    ok, _ = broker.park_auth_code("short", state)
    assert ok is False


# ---------------------------------------------------------------------------
# Status machine + expiry (Upstox sessions end 03:30 IST next day)
# ---------------------------------------------------------------------------


def test_status_unauthenticated_initially(broker: UpstoxBroker) -> None:
    status = broker.get_session_status()
    assert status["status"] == STATUS_UNAUTHENTICATED
    assert status["broker"] == "upstox"
    assert status["expires_at"] is None


def test_expiry_defaults_to_0330_ist_next_day(broker: UpstoxBroker) -> None:
    broker.login("api-key-123456", "api-secret-123456")
    with patch(
        "backtest.brokers.upstox.requests.post",
        lambda url, **kw: _ok_response({"access_token": "tok"}),
    ):
        broker.verify_totp("auth-code-from-redirect-123456")

    expires_at = broker._expires_at
    assert expires_at is not None
    assert expires_at.tzinfo is None  # naive-local, the module's convention
    # Read the naive-local wall clock as local time and convert to IST: the
    # session must die at 03:30 IST (the Upstox rule), whatever the machine tz.
    as_ist = expires_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert (as_ist.hour, as_ist.minute) == (3, 30)
    assert expires_at > datetime.now()


def test_expiry_honors_server_expires_in(broker: UpstoxBroker) -> None:
    broker.login("api-key-123456", "api-secret-123456")
    with patch(
        "backtest.brokers.upstox.requests.post",
        lambda url, **kw: _ok_response({"access_token": "tok", "expires_in": 600}),
    ):
        broker.verify_totp("auth-code-from-redirect-123456")
    remaining = broker._expires_at - datetime.now()  # type: ignore[operator]
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)


def test_status_expired_after_expiry_passes(broker: UpstoxBroker) -> None:
    broker.restore_session("tok", datetime.now() - timedelta(minutes=1))
    assert broker.get_session_status()["status"] == STATUS_EXPIRED
    assert broker.get_session_token() is None


def test_status_expiring_soon_within_30_minutes(broker: UpstoxBroker) -> None:
    broker.restore_session("tok", datetime.now() + timedelta(minutes=15))
    assert broker.get_session_status()["status"] == STATUS_EXPIRING_SOON


# ---------------------------------------------------------------------------
# Logout / restore
# ---------------------------------------------------------------------------


def test_logout_clears_everything(broker: UpstoxBroker) -> None:
    broker.login("api-key-123456", "api-secret-123456")
    with patch(
        "backtest.brokers.upstox.requests.post",
        lambda url, **kw: _ok_response({"access_token": "tok"}),
    ):
        broker.verify_totp("auth-code-from-redirect-123456")
    broker.logout()
    assert broker.get_session_status()["status"] == STATUS_UNAUTHENTICATED
    assert broker.get_session_token() is None
    assert broker._temp_api_secret is None


def test_restore_session_seeds_token(broker: UpstoxBroker) -> None:
    broker.restore_session("tok-xyz", datetime.now() + timedelta(hours=1))
    assert broker.get_session_token() == "tok-xyz"
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED


def test_restore_session_normalizes_aware_expiry(broker: UpstoxBroker) -> None:
    """Remember-session returns AWARE expiries; the broker clock is naive-local."""
    aware_future = datetime.now().astimezone() + timedelta(hours=2)
    broker.restore_session("tok-xyz", aware_future)
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED

    aware_past = datetime.now(timezone.utc) - timedelta(minutes=1)
    broker.restore_session("tok-xyz", aware_past)
    assert broker.get_session_status()["status"] == STATUS_EXPIRED
    assert broker.get_session_token() is None

    broker.restore_session("tok-xyz", aware_future.isoformat())
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED
