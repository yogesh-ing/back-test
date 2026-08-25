"""Broker auth epic, Task 1.1 — ``BrokerAuthBase`` contract tests.

Verification required by the PRD: abstract methods cannot be skipped by a
subclass without raising ``TypeError``.
"""

from __future__ import annotations

from typing import Any

import pytest

from backtest.brokers import (
    SESSION_STATUSES,
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)

_ABSTRACT_METHODS = ("login", "verify_totp", "get_session_status", "logout")


class _StubBroker(BrokerAuthBase):
    """Minimal concrete broker honouring the documented return shapes."""

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "credentials verified", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        return {"success": True, "message": "session established", "expires_at": "2026-08-25T15:45:00"}

    def get_session_status(self) -> dict[str, Any]:
        return {"status": STATUS_AUTHENTICATED, "expires_at": "2026-08-25T15:45:00", "broker": self.broker_name}

    def logout(self) -> None:
        return None


def _partial_broker(drop: str) -> type[BrokerAuthBase]:
    """Build a broker subclass that implements everything except ``drop``."""
    body: dict[str, Any] = {
        "login": lambda self, username, password: {},
        "verify_totp": lambda self, totp_code: {},
        "get_session_status": lambda self: {},
        "logout": lambda self: None,
    }
    body.pop(drop)
    return type("PartialBroker", (BrokerAuthBase,), body)


# ---------------------------------------------------------------------------
# PRD verification: abstract methods cannot be skipped
# ---------------------------------------------------------------------------


def test_base_class_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BrokerAuthBase()  # type: ignore[abstract]


@pytest.mark.parametrize("missing", _ABSTRACT_METHODS)
def test_subclass_missing_abstract_method_raises_type_error(missing):
    with pytest.raises(TypeError):
        _partial_broker(missing)()


def test_all_four_methods_are_abstract():
    assert set(BrokerAuthBase.__abstractmethods__) == set(_ABSTRACT_METHODS)


# ---------------------------------------------------------------------------
# Concrete subclass works and honours the documented contracts
# ---------------------------------------------------------------------------


def test_complete_subclass_instantiates():
    broker = _StubBroker()
    assert isinstance(broker, BrokerAuthBase)


def test_defaults_and_overrides_for_broker_metadata():
    class UnnamedBroker(_StubBroker):
        broker_name = BrokerAuthBase.broker_name
        broker_display_name = BrokerAuthBase.broker_display_name

    assert UnnamedBroker.broker_name == "unnamed"
    assert UnnamedBroker.broker_display_name == "Unknown Broker"
    assert _StubBroker.broker_name == "stub"
    assert _StubBroker.broker_display_name == "Stub Broker"


def test_login_return_contract():
    result = _StubBroker().login("user", "pass")  # noqa: S106 - test fixture
    assert set(result) == {"success", "message", "requires_totp"}
    assert isinstance(result["success"], bool)
    assert isinstance(result["requires_totp"], bool)


def test_verify_totp_return_contract():
    result = _StubBroker().verify_totp("123456")
    assert set(result) == {"success", "message", "expires_at"}


def test_get_session_status_return_contract():
    result = _StubBroker().get_session_status()
    assert set(result) == {"status", "expires_at", "broker"}
    assert result["status"] in SESSION_STATUSES
    assert result["broker"] == "stub"


def test_logout_returns_none():
    assert _StubBroker().logout() is None


# ---------------------------------------------------------------------------
# Status vocabulary matches the PRD session state machine
# ---------------------------------------------------------------------------


def test_session_status_vocabulary():
    assert SESSION_STATUSES == {
        STATUS_UNAUTHENTICATED,
        STATUS_AUTHENTICATED,
        STATUS_EXPIRING_SOON,
        STATUS_EXPIRED,
    }
    assert STATUS_UNAUTHENTICATED == "unauthenticated"
    assert STATUS_AUTHENTICATED == "authenticated"
    assert STATUS_EXPIRING_SOON == "expiring_soon"
    assert STATUS_EXPIRED == "expired"
