"""Tests for remember-session-today (2026-09-24).

Covers the toggle file lifecycle (default OFF, ON saves, OFF deletes the
token file immediately), the save/load round-trip including expiry cleanup,
and the BrokerSessionManager integration (restore-on-boot, save-on-TOTP,
delete-on-logout). Filesystem effects land in a tmp path via the
``BROKER_REMEMBER_SESSION_PATH`` env var, so the real project root is never
touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def rs_env(tmp_path, monkeypatch):
    """Point the remember-session store at a tmp dir; reload the module."""
    monkeypatch.setenv("BROKER_REMEMBER_SESSION_PATH", str(tmp_path))
    monkeypatch.delenv("BROKER_REMEMBER_SESSION", raising=False)
    import importlib

    from backtest.brokers import remember_session

    importlib.reload(remember_session)
    return remember_session


def _expiry(hours: float = 3.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------


def test_toggle_defaults_off(rs_env):
    assert rs_env.get_toggle() is False


def test_toggle_on_then_off_deletes_saved_file(rs_env):
    rs_env.set_toggle(True)
    assert rs_env.save_session("tok", _expiry()) is True
    assert rs_env.has_saved_session() is True

    result = rs_env.set_toggle(False)
    assert result["deleted"] is True
    assert rs_env.has_saved_session() is False
    assert rs_env.get_toggle() is False


def test_toggle_persists_across_reloads(rs_env, monkeypatch):
    rs_env.set_toggle(True)
    import importlib

    importlib.reload(rs_env)
    assert rs_env.get_toggle() is True
    rs_env.set_toggle(False)


def test_env_var_overrides_toggle_file(rs_env, monkeypatch):
    rs_env.set_toggle(True)
    monkeypatch.setenv("BROKER_REMEMBER_SESSION", "0")
    assert rs_env.get_toggle() is False
    monkeypatch.setenv("BROKER_REMEMBER_SESSION", "1")
    assert rs_env.get_toggle() is True


# ---------------------------------------------------------------------------
# Saved-session file
# ---------------------------------------------------------------------------


def test_save_and_load_round_trip(rs_env):
    rs_env.set_toggle(True)
    expires = _expiry(hours=2)
    assert rs_env.save_session("tok-abc", expires, broker="mstock") is True

    saved = rs_env.load_session()
    assert saved is not None
    assert saved["token"] == "tok-abc"
    assert saved["broker"] == "mstock"
    assert saved["expires_at"].replace(microsecond=0) == expires.replace(microsecond=0)


def test_expired_session_is_removed_on_load(rs_env):
    rs_env.set_toggle(True)
    rs_env.save_session("stale", _expiry(hours=-1))
    assert rs_env.load_session() is None
    assert rs_env.store_path().exists() is False


def test_corrupt_file_is_removed_on_load(rs_env):
    rs_env.set_toggle(True)
    rs_env.store_path().write_text("{not json", encoding="utf-8")
    assert rs_env.load_session() is None
    assert rs_env.store_path().exists() is False


def test_bad_expiry_is_removed_on_load(rs_env):
    rs_env.set_toggle(True)
    rs_env.store_path().write_text(
        json.dumps({"broker": "mstock", "token": "t", "expires_at": "not-a-date"}),
        encoding="utf-8",
    )
    assert rs_env.load_session() is None
    assert rs_env.store_path().exists() is False


def test_save_refuses_empty_token(rs_env):
    rs_env.set_toggle(True)
    assert rs_env.save_session("", _expiry()) is False


# ---------------------------------------------------------------------------
# BrokerSessionManager integration
# ---------------------------------------------------------------------------


class _FakeBroker:
    """Duck-typed broker: records restore_session calls, holds a session."""

    broker_name = "stub"

    def __init__(self) -> None:
        self.token: str | None = None
        self.expires_at = None
        self.restored: tuple | None = None
        self.logged_out = False

    def restore_session(self, token, expires_at) -> None:
        self.token = token
        self.expires_at = expires_at
        self.restored = (token, expires_at)

    def get_session_status(self) -> dict:
        if not self.token or not self.expires_at:
            return {"status": "unauthenticated", "expires_at": None}
        if datetime.now(timezone.utc) >= self.expires_at:
            return {"status": "expired", "expires_at": self.expires_at.isoformat()}
        return {"status": "authenticated", "expires_at": self.expires_at.isoformat()}

    def get_session_token(self) -> str | None:
        if self.token and self.expires_at and datetime.now(timezone.utc) < self.expires_at:
            return self.token
        return None

    def logout(self) -> None:
        self.logged_out = True
        self.token = None
        self.expires_at = None

    def login(self, username, password) -> dict:
        self._pending = True
        return {"success": True, "requires_totp": True}

    def verify_totp(self, code) -> dict:
        self.token = "fresh-tok"
        self.expires_at = _expiry(hours=6)
        return {"success": True, "expires_at": self.expires_at.isoformat()}


@pytest.fixture()
def mgr(rs_env, monkeypatch):
    from backtest.brokers.session_manager import BrokerSessionManager

    broker = _FakeBroker()
    manager = BrokerSessionManager(broker_factory=lambda: broker)
    return manager, broker


def test_restore_on_boot_when_toggle_on(rs_env, mgr):
    manager, broker = mgr
    rs_env.set_toggle(True)
    rs_env.save_session("remembered", _expiry(hours=2))

    from backtest.brokers.session_manager import BrokerSessionManager

    fresh = BrokerSessionManager(broker_factory=lambda: broker)
    assert broker.restored is not None
    assert broker.token == "remembered"
    assert fresh.get_status()["status"] == "authenticated"


def test_no_restore_when_toggle_off(rs_env, mgr):
    manager, broker = mgr
    rs_env.set_toggle(False)
    rs_env.save_session("should-not-restore", _expiry(hours=2))

    from backtest.brokers.session_manager import BrokerSessionManager

    BrokerSessionManager(broker_factory=lambda: broker)
    assert broker.restored is None


def test_totp_success_saves_session_when_toggle_on(rs_env, mgr):
    manager, broker = mgr
    rs_env.set_toggle(True)
    assert manager.verify_totp("123456")["success"] is True
    saved = rs_env.load_session()
    assert saved is not None
    assert saved["token"] == "fresh-tok"


def test_totp_success_does_not_save_when_toggle_off(rs_env, mgr):
    manager, broker = mgr
    rs_env.set_toggle(False)
    assert manager.verify_totp("123456")["success"] is True
    assert rs_env.load_session() is None


def test_logout_deletes_remembered_session(rs_env, mgr):
    manager, broker = mgr
    rs_env.set_toggle(True)
    rs_env.save_session("tok", _expiry(hours=2))
    manager.logout()
    assert rs_env.has_saved_session() is False
    assert broker.logged_out is True
