"""DhanBroker unit tests — auth contract compliance + security invariants.

Covers the two-step auth contract (BrokerAuthBase) with the Dhan
``generateAccessToken`` endpoint mocked at the HTTP boundary:

* step 1 (login) validates client-id/PIN shape and never calls the network;
* step 2 (verify_totp) posts client-id + PIN + TOTP and stores the token;
* the PIN and the access token never leak into any return value;
* status machine (unauthenticated/authenticated/expiring_soon/expired);
* missing DHAN_API_KEY blocks login with a clear message.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
from backtest.brokers.dhan import DhanBroker


def _ok_response(payload: dict[str, Any]) -> Any:
    class _Resp:
        status_code = 200
        ok = True

        def json(self) -> dict[str, Any]:
            return payload

        @property
        def text(self) -> str:  # pragma: no cover — unused
            return ""

    return _Resp()


def _err_response(status_code: int, payload: dict[str, Any] | None) -> Any:
    class _Resp:
        ok = False

        def json(self) -> dict[str, Any] | None:
            return payload

        @property
        def text(self) -> str:  # pragma: no cover — unused
            return ""

    setattr(_Resp, "status_code", status_code)
    return _Resp()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> DhanBroker:
    monkeypatch.setenv("DHAN_API_KEY", "test-api-key")
    return DhanBroker()


# ---------------------------------------------------------------------------
# Step 1 — login (credentials parked, no network)
# ---------------------------------------------------------------------------


def test_login_success_sets_temp_context_and_requires_totp(broker: DhanBroker) -> None:
    with patch("backtest.brokers.dhan.requests.post") as post:
        result = broker.login("DD1234", "1234")
    post.assert_not_called()  # step 1 must not hit the network
    assert result == {
        "success": True,
        "message": result["message"],
        "requires_totp": True,
    }
    assert result["success"] is True


def test_login_missing_fields_rejected(broker: DhanBroker) -> None:
    for username, password in (("", "1234"), ("DD1234", ""), ("", "")):
        result = broker.login(username, password)
        assert result["success"] is False
        assert result["requires_totp"] is False


def test_login_bad_pin_shape_rejected(broker: DhanBroker) -> None:
    result = broker.login("DD1234", "12ab")
    assert result["success"] is False
    assert "PIN" in result["message"]


def test_login_without_api_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DHAN_API_KEY", raising=False)
    broker = DhanBroker()
    result = broker.login("DD1234", "1234")
    assert result["success"] is False
    assert "DHAN_API_KEY" in result["message"]


# ---------------------------------------------------------------------------
# Step 2 — verify_totp (the actual token call)
# ---------------------------------------------------------------------------


def test_verify_totp_without_login_rejected(broker: DhanBroker) -> None:
    result = broker.verify_totp("123456")
    assert result["success"] is False
    assert "Log in" in result["message"]


def test_verify_totp_bad_shape_rejected(broker: DhanBroker) -> None:
    broker.login("DD1234", "1234")
    result = broker.verify_totp("12ab")
    assert result["success"] is False


def test_verify_totp_success_stores_token_and_clears_pin(
    broker: DhanBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker.login("DD1234", "1234")
    captured: dict[str, Any] = {}

    def _fake_post(url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        captured["url"] = url
        captured["params"] = params
        return _ok_response({"accessToken": "tok-abc", "expiryTime": ""})

    monkeypatch.setattr("backtest.brokers.dhan.requests.post", _fake_post)
    result = broker.verify_totp("654321")

    assert result["success"] is True
    assert "tok-abc" not in str(result)  # token never leaves the backend
    assert captured["params"]["dhanClientId"] == "DD1234"
    assert captured["params"]["pin"] == "1234"
    assert captured["params"]["totp"] == "654321"
    assert "auth.dhan.co" in captured["url"]
    # PIN discarded after the call
    assert broker._temp_pin is None
    assert broker._temp_username is None
    # Session live
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED
    assert broker.get_session_token() == "tok-abc"


def test_verify_totp_rejected_keeps_temp_context(broker: DhanBroker) -> None:
    broker.login("DD1234", "1234")

    def _fake_post(url: str, **kw: Any) -> Any:
        return _err_response(401, {"errorMessage": "Invalid TOTP"})

    with patch("backtest.brokers.dhan.requests.post", _fake_post):
        result = broker.verify_totp("000000")

    assert result["success"] is False
    assert result["message"] == "Invalid TOTP"
    assert broker.get_session_status()["status"] == STATUS_UNAUTHENTICATED
    # temp context kept so the user can retry without re-entering the PIN
    assert broker._temp_pin is not None


def test_verify_totp_network_error_user_facing(broker: DhanBroker) -> None:
    broker.login("DD1234", "1234")

    def _fake_post(url: str, **kw: Any) -> Any:
        raise _requests.ConnectionError("boom")

    with patch("backtest.brokers.dhan.requests.post", _fake_post):
        result = broker.verify_totp("123456")
    assert result["success"] is False
    assert "Could not reach Dhan" in result["message"]


# ---------------------------------------------------------------------------
# Status machine + expiry parsing
# ---------------------------------------------------------------------------


def test_status_unauthenticated_initially(broker: DhanBroker) -> None:
    status = broker.get_session_status()
    assert status["status"] == STATUS_UNAUTHENTICATED
    assert status["broker"] == "dhan"
    assert status["expires_at"] is None


def test_expiry_from_server_expiry_time(broker: DhanBroker) -> None:
    broker.login("DD1234", "1234")
    future = (datetime.now() + timedelta(hours=20)).replace(microsecond=0)
    payload = {"accessToken": "tok", "expiryTime": future.isoformat() + "Z"}
    with patch(
        "backtest.brokers.dhan.requests.post",
        lambda url, **kw: _ok_response(payload),
    ):
        result = broker.verify_totp("123456")
    assert result["success"] is True
    status = broker.get_session_status()
    assert status["status"] == STATUS_AUTHENTICATED
    assert status["expires_at"] is not None


def test_expiry_fallback_ttl_when_no_hint(broker: DhanBroker) -> None:
    broker.login("DD1234", "1234")
    with patch(
        "backtest.brokers.dhan.requests.post",
        lambda url, **kw: _ok_response({"accessToken": "tok"}),
    ):
        broker.verify_totp("123456")
    remaining = broker._expires_at - datetime.now()  # type: ignore[operator]
    assert timedelta(minutes=389) < remaining <= timedelta(minutes=391)


def test_status_expired_after_expiry_passes(broker: DhanBroker) -> None:
    broker.restore_session("tok", datetime.now() - timedelta(minutes=1))
    assert broker.get_session_status()["status"] == STATUS_EXPIRED
    assert broker.get_session_token() is None


def test_status_expiring_soon_within_30_minutes(broker: DhanBroker) -> None:
    broker.restore_session("tok", datetime.now() + timedelta(minutes=15))
    assert broker.get_session_status()["status"] == STATUS_EXPIRING_SOON


# ---------------------------------------------------------------------------
# Logout / restore
# ---------------------------------------------------------------------------


def test_logout_clears_everything(broker: DhanBroker) -> None:
    broker.login("DD1234", "1234")
    with patch(
        "backtest.brokers.dhan.requests.post",
        lambda url, **kw: _ok_response({"accessToken": "tok"}),
    ):
        broker.verify_totp("123456")
    broker.logout()
    assert broker.get_session_status()["status"] == STATUS_UNAUTHENTICATED
    assert broker.get_session_token() is None
    assert broker._temp_pin is None


def test_restore_session_seeds_token(broker: DhanBroker) -> None:
    broker.restore_session("tok-xyz", datetime.now() + timedelta(hours=1))
    assert broker.get_session_token() == "tok-xyz"
    assert broker.get_session_status()["status"] == STATUS_AUTHENTICATED
