"""Multi-broker sessions v2 (PRD-001 Phase A, TEST-001).

Covers the four Phase A guarantees:

* multi-session lifecycle — login mStock, then Dhan; BOTH authenticated;
* cross-broker isolation — logout/login on one broker never touches another;
* expiry monitor iterates ALL sessions and flags per ``(broker, kind)``;
* remember-session persistence is per broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import backtest.brokers.session_manager as sm_module
from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)
from backtest.brokers.session_manager import BrokerSessionManager, UnknownBrokerError


class _ScriptBroker(BrokerAuthBase):
    """Scriptable in-memory broker for one name (mstock/dhan stand-in)."""

    def __init__(self, name: str, display: str) -> None:
        self.broker_name = name
        self.broker_display_name = display
        self._status = STATUS_UNAUTHENTICATED
        self._token: str | None = None
        self._expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=6)
        ).replace(tzinfo=None)
        self.logout_calls = 0

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "ok", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self._status = STATUS_AUTHENTICATED
        self._token = f"{self.broker_name}-token"
        return {"success": True, "message": "ok", "expires_at": self._expires_at.isoformat()}

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "expires_at": self._expires_at.isoformat() if self._token else None,
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        if self._status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
            return self._token
        return None

    def restore_session(self, token: str, expires_at: Any) -> None:
        self._token = token
        self._expires_at = expires_at
        self._status = STATUS_AUTHENTICATED

    def logout(self) -> None:
        self.logout_calls += 1
        self._status = STATUS_UNAUTHENTICATED
        self._token = None


@pytest.fixture()
def brokers(monkeypatch) -> dict[str, _ScriptBroker]:
    """Patch the registry so 'mstock' and 'dhan' map to script brokers."""
    pair = {
        "mstock": _ScriptBroker("mstock", "mStock"),
        "dhan": _ScriptBroker("dhan", "Dhan"),
    }
    monkeypatch.setattr(
        sm_module,
        "_BROKER_REGISTRY",
        {name: (lambda n=name: pair[n]) for name in pair},
    )
    return pair


@pytest.fixture()
def manager(brokers) -> BrokerSessionManager:
    mgr = BrokerSessionManager(broker_factory=lambda: brokers["mstock"])
    yield mgr
    mgr.shutdown()


def _full_login(manager: BrokerSessionManager, name: str) -> None:
    assert manager.login("user", "pass", broker_name=name)["success"] is True
    assert manager.verify_totp("123456", broker_name=name)["success"] is True


# ---------------------------------------------------------------------------
# Multi-session lifecycle
# ---------------------------------------------------------------------------


def test_multi_session_lifecycle(manager, brokers):
    """Login mStock, then Dhan; both should be authenticated."""
    _full_login(manager, "mstock")
    _full_login(manager, "dhan")

    assert manager.is_authenticated("mstock") is True
    assert manager.is_authenticated("dhan") is True
    assert manager.get_session_token("mstock") == "mstock-token"
    assert manager.get_session_token("dhan") == "dhan-token"

    status = manager.get_all_status()
    assert status["mstock"]["authenticated"] is True
    assert status["dhan"]["authenticated"] is True


def test_cross_broker_isolation(manager, brokers):
    """Logout mStock should not affect the Dhan session (and vice versa)."""
    _full_login(manager, "mstock")
    _full_login(manager, "dhan")

    manager.logout("mstock")
    assert manager.is_authenticated("mstock") is False
    assert manager.is_authenticated("dhan") is True
    assert brokers["dhan"].logout_calls == 0

    # Logging back into mstock must not touch dhan either.
    _full_login(manager, "mstock")
    assert manager.is_authenticated("dhan") is True
    assert manager.get_session_token("dhan") == "dhan-token"


def test_login_broker_b_does_not_touch_broker_a(manager, brokers):
    """PRD invariant: login into B MUST NOT touch A's session."""
    _full_login(manager, "mstock")
    token_before = manager.get_session_token("mstock")

    manager.login("u2", "p2", broker_name="dhan")  # step 1 only
    assert manager.get_session_token("mstock") == token_before
    assert brokers["mstock"].logout_calls == 0


def test_switch_broker_is_deprecated_and_drops_nothing(manager, brokers):
    _full_login(manager, "mstock")
    result = manager.switch_broker("dhan")
    assert result["success"] is True
    assert result["deprecated"] is True
    # The mStock session survived the "switch".
    assert manager.is_authenticated("mstock") is True
    assert manager.get_ui_active() == "dhan"


def test_unknown_broker_refused(manager):
    with pytest.raises(UnknownBrokerError):
        manager.login("u", "p", broker_name="zerodha")
    assert manager.set_ui_active("zerodha")["success"] is False
    assert manager.is_authenticated("zerodha") is False
    assert manager.get_session_token("zerodha") is None


def test_get_authenticated_broker_fail_closed(manager, brokers):
    assert manager.get_authenticated_broker("dhan") is None  # not logged in
    _full_login(manager, "dhan")
    assert manager.get_authenticated_broker("dhan") is brokers["dhan"]
    brokers["dhan"]._status = STATUS_EXPIRED
    assert manager.get_authenticated_broker("dhan") is None  # expired → refuse


# ---------------------------------------------------------------------------
# Expiry monitor across sessions
# ---------------------------------------------------------------------------


def test_expiry_monitor_all_sessions(manager, brokers):
    """Monitor checks all broker sessions; flags are (broker, kind) events."""
    _full_login(manager, "mstock")
    _full_login(manager, "dhan")

    brokers["mstock"]._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    events = manager.consume_expiry_events()
    assert events == [{"broker": "mstock", "kind": "expiring_soon"}]
    assert manager.consume_expiry_events() == []  # consumed exactly once

    # Dhan expires — only dhan's token is cleared; mstock stays live.
    brokers["dhan"]._status = STATUS_EXPIRED
    manager._poll_once()
    events = manager.consume_expiry_events()
    assert {"broker": "dhan", "kind": "expired"} in events
    assert brokers["dhan"].logout_calls >= 1
    assert manager.is_authenticated("mstock") is True


def test_expired_broker_does_not_flag_other_broker(manager, brokers):
    _full_login(manager, "dhan")
    brokers["dhan"]._status = STATUS_EXPIRED
    manager._poll_once()
    events = manager.consume_expiry_events()
    assert all(e["broker"] == "dhan" for e in events)


# ---------------------------------------------------------------------------
# Remember-session per broker
# ---------------------------------------------------------------------------


@pytest.fixture()
def rs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_REMEMBER_SESSION_PATH", str(tmp_path))
    monkeypatch.delenv("BROKER_REMEMBER_SESSION", raising=False)
    import backtest.brokers.remember_session as rs

    return rs


def test_remember_session_per_broker(rs_env, brokers, monkeypatch):
    """Session persistence per broker, not global (Phase A test plan)."""
    rs_env.set_toggle(True)
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    assert rs_env.save_session("tok-m", expiry, broker="mstock") is True
    assert rs_env.save_session("tok-d", expiry, broker="dhan") is True

    saved = rs_env.load_sessions()
    assert saved["mstock"]["token"] == "tok-m"
    assert saved["dhan"]["token"] == "tok-d"

    # Deleting one broker's entry preserves the other.
    assert rs_env.delete_saved_session("mstock") is True
    saved = rs_env.load_sessions()
    assert "mstock" not in saved
    assert saved["dhan"]["token"] == "tok-d"


def test_boot_restores_every_saved_broker(rs_env, brokers):
    rs_env.set_toggle(True)
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    rs_env.save_session("tok-m", expiry, broker="mstock")
    rs_env.save_session("tok-d", expiry, broker="dhan")

    mgr = BrokerSessionManager()  # default factory → per-broker restore path
    try:
        assert mgr.is_authenticated("mstock") is True
        assert mgr.is_authenticated("dhan") is True
        assert mgr.get_session_token("mstock") == "tok-m"
        assert mgr.get_session_token("dhan") == "tok-d"
    finally:
        mgr.shutdown()


def test_logout_forgets_only_that_brokers_entry(rs_env, brokers, manager):
    rs_env.set_toggle(True)
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    rs_env.save_session("tok-m", expiry, broker="mstock")
    rs_env.save_session("tok-d", expiry, broker="dhan")

    _full_login(manager, "mstock")
    manager.logout("mstock")

    assert rs_env.has_saved_session("mstock") is False
    assert rs_env.has_saved_session("dhan") is True
