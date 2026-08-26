"""Broker auth epic — ``BrokerSessionManager`` tests (Tasks 1.3 + 2.2).

Task 1.3 verification: the Forward Engine retrieves the session token
through the manager alone, with no direct dependency on ``MStockBroker``.

Task 2.2 verification: a mock session set to expire in 25 minutes
transitions to ``expiring_soon`` (flag fired exactly once).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import pytest

import backtest.brokers.session_manager as sm_module
from backtest.brokers import MStockBroker
from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)
from backtest.brokers.session_manager import (
    BrokerSessionManager,
    get_session_manager,
    reset_default_manager,
)


class _StubBroker(BrokerAuthBase):
    """Scriptable broker standing in for any future implementation."""

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def __init__(self, status: str = STATUS_AUTHENTICATED, token: str = "stub-token-123"):
        self._status = status
        self._token = token
        self._expires_at = "2026-08-25T15:45:00"
        self.login_calls = 0
        self.verify_calls = 0
        self.logout_calls = 0
        self._raise_on_status = False

    def login(self, username: str, password: str) -> dict[str, Any]:
        self.login_calls += 1
        return {"success": True, "message": "ok", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self.verify_calls += 1
        self._status = STATUS_AUTHENTICATED
        return {"success": True, "message": "ok", "expires_at": self._expires_at}

    def get_session_status(self) -> dict[str, Any]:
        if self._raise_on_status:
            self._raise_on_status = False
            raise RuntimeError("boom")
        return {"status": self._status, "expires_at": self._expires_at, "broker": self.broker_name}

    def get_session_token(self) -> str | None:
        if self._status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
            return self._token
        return None

    def logout(self) -> None:
        self.logout_calls += 1
        self._status = STATUS_UNAUTHENTICATED
        self._token = None


@pytest.fixture()
def stub() -> _StubBroker:
    return _StubBroker()


@pytest.fixture()
def manager(stub: _StubBroker) -> BrokerSessionManager:
    return BrokerSessionManager(broker_factory=lambda: stub)


# ---------------------------------------------------------------------------
# Task 1.3 — registry, delegation, status, token access
# ---------------------------------------------------------------------------


def test_forward_engine_retrieves_token_via_manager_only(manager, stub):
    """PRD verification: engine has no direct dependency on MStockBroker."""
    # The manager module keeps MStockBroker out of its namespace (lazy
    # factory import) — engine code touching only this module never sees it.
    assert "MStockBroker" not in dir(sm_module)
    assert manager.get_active_session_token() == "stub-token-123"
    assert isinstance(stub, BrokerAuthBase)  # coupling is to the ABC only


def test_manager_module_public_api():
    assert sm_module.__all__ == [
        "BrokerSessionManager",
        "get_session_manager",
        "reset_default_manager",
    ]


def test_get_session_manager_is_singleton():
    try:
        assert get_session_manager() is get_session_manager()
        assert isinstance(get_session_manager(), BrokerSessionManager)
    finally:
        reset_default_manager()


def test_reset_default_manager_drops_singleton():
    try:
        first = get_session_manager()
        reset_default_manager()
        assert get_session_manager() is not first
    finally:
        reset_default_manager()


def test_default_broker_is_mstock():
    manager = BrokerSessionManager()  # default factory
    try:
        broker = manager.get_active_broker()
        assert broker.broker_name == "mstock"
        assert isinstance(broker, MStockBroker)
        assert manager.get_status()["broker_display_name"] == "mStock"
    finally:
        manager.shutdown()


def test_set_broker_swaps_active_instance(manager, stub):
    replacement = _StubBroker(status=STATUS_EXPIRING_SOON, token="other-token")
    manager.set_broker(replacement)
    assert manager.get_active_broker() is replacement
    assert manager.get_active_session_token() == "other-token"


def test_get_status_shape_and_no_token_leak(manager):
    status = manager.get_status()
    assert status == {
        "status": STATUS_AUTHENTICATED,
        "broker": "stub",
        "broker_display_name": "Stub Broker",
        "expires_at": "2026-08-25T15:45:00",
    }
    assert "stub-token-123" not in repr(status)


def test_auth_flow_delegates_to_broker(manager, stub):
    assert manager.login("user", "pass") == {
        "success": True,
        "message": "ok",
        "requires_totp": True,
    }
    assert stub.login_calls == 1
    assert manager.verify_totp("123456")["success"] is True
    assert stub.verify_calls == 1
    manager.logout()
    assert stub.logout_calls == 1
    assert manager.get_status()["status"] == STATUS_UNAUTHENTICATED


def test_verify_totp_success_resets_notification_cycle(manager, stub):
    manager._expiring_soon_flag = True
    manager._expired_flag = True
    manager.verify_totp("123456")
    assert manager.consume_expiring_soon_notification() is False
    assert manager.consume_expired_notification() is False


@pytest.mark.parametrize(
    ("status", "has_token", "authenticated"),
    [
        (STATUS_AUTHENTICATED, True, True),
        (STATUS_EXPIRING_SOON, True, True),  # still a valid session
        (STATUS_EXPIRED, False, False),
        (STATUS_UNAUTHENTICATED, False, False),
    ],
)
def test_token_and_gate_by_status(manager, stub, status, has_token, authenticated):
    stub._status = status
    token = manager.get_active_session_token()
    assert (token == "stub-token-123") is has_token
    assert manager.is_authenticated() is authenticated


# ---------------------------------------------------------------------------
# Task 2.2 — expiry monitor (poll logic + thread)
# ---------------------------------------------------------------------------


def test_poll_once_flags_expiring_soon_exactly_once(manager, stub):
    """PRD verification: session expiring in 25 min → expiring_soon transition."""
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    assert manager.consume_expiring_soon_notification() is True  # fires once
    assert manager.consume_expiring_soon_notification() is False

    manager._poll_once()  # still expiring_soon — no repeat notification
    assert manager.consume_expiring_soon_notification() is False


def test_poll_once_expired_clears_token_and_never_auto_renews(manager, stub):
    stub._status = STATUS_EXPIRED
    manager._poll_once()

    assert stub.logout_calls == 1  # token cleared per PRD
    assert manager.get_active_session_token() is None
    assert manager.consume_expired_notification() is True
    assert manager.consume_expired_notification() is False
    assert stub.login_calls == 0  # no auto-renew
    assert manager.get_status()["status"] == STATUS_UNAUTHENTICATED


def test_poll_once_survives_broker_exceptions(manager, stub):
    stub._raise_on_status = True
    manager._poll_once()  # must not raise

    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    assert manager.consume_expiring_soon_notification() is True


def test_monitor_thread_runs_and_is_idempotent(manager, stub):
    stub._status = STATUS_EXPIRING_SOON
    try:
        assert manager.start_monitor(interval_seconds=0.02) is True
        assert manager.start_monitor(interval_seconds=0.02) is False  # already running

        deadline = time.monotonic() + 2.0
        while not manager.consume_expiring_soon_notification():
            if time.monotonic() > deadline:
                pytest.fail("monitor did not flag expiring_soon within 2s")
            time.sleep(0.01)
    finally:
        manager.stop_monitor()
    assert manager._monitor_thread is None


def test_stop_monitor_terminates_thread(manager):
    manager.start_monitor(interval_seconds=0.02)
    thread = manager._monitor_thread
    manager.stop_monitor(timeout=2.0)
    assert not thread.is_alive()


def test_shutdown_drops_all_state(manager, stub):
    manager.shutdown()
    assert manager._monitor_thread is None
    assert manager._broker is None
    assert manager.consume_expiring_soon_notification() is False


# ---------------------------------------------------------------------------
# Tasks 1.2 + 2.2 integration: real MStockBroker, mocked HTTP, 25-min expiry
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    @property
    def ok(self) -> bool:
        return True

    def json(self) -> dict:
        return self._payload


def test_prd_2_2_mstock_session_expiring_in_25_minutes(monkeypatch):
    """PRD Task 2.2 verification against the real broker object.

    Full mocked flow: mStock login → TOTP → session established, then the
    clock is moved so ~25 minutes remain → monitor flags ``expiring_soon``.
    """
    monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")
    monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **kw: _FakeResponse({"access_token": "tok-integration-99"}),
    )

    broker = MStockBroker()
    assert broker.login("trader", "s3cret")["success"] is True
    assert broker.verify_totp("123456")["success"] is True

    # Mock: session now expires in 25 minutes (inside the 30-min window).
    broker._expires_at = datetime.now() + timedelta(minutes=25)
    assert broker.get_session_status()["status"] == STATUS_EXPIRING_SOON

    manager = BrokerSessionManager(broker_factory=lambda: broker)
    try:
        assert manager.is_authenticated() is True  # expiring_soon still valid
        manager._poll_once()
        assert manager.consume_expiring_soon_notification() is True

        # Expiry passes → monitor clears the token, engine loses access.
        broker._expires_at = datetime.now() - timedelta(minutes=1)
        manager._poll_once()
        assert manager.get_active_session_token() is None
        assert manager.get_status()["status"] == STATUS_UNAUTHENTICATED
    finally:
        manager.shutdown()
