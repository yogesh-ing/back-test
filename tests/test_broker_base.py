"""Broker contract tests.

* Task 1.1 — ``BrokerAuthBase``: abstract methods cannot be skipped by a
  subclass without raising ``TypeError``.
* Ticket P3.1 — ``BrokerOrderBase``: same enforcement for the five-method
  order surface; the fake broker in the tests implements all five.
"""

from __future__ import annotations

from typing import Any

import pytest

from backtest.brokers import (
    ORDER_STATUSES,
    SESSION_STATUSES,
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
    BrokerOrder,
    BrokerOrderBase,
    BrokerOrderId,
    MarginInfo,
)

_ABSTRACT_METHODS = ("login", "verify_totp", "get_session_status", "logout")


class _StubBroker(BrokerAuthBase):
    """Minimal concrete broker honouring the documented return shapes."""

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "credentials verified", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        return {
            "success": True,
            "message": "session established",
            "expires_at": "2026-08-25T15:45:00",
        }

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": STATUS_AUTHENTICATED,
            "expires_at": "2026-08-25T15:45:00",
            "broker": self.broker_name,
        }

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


# ---------------------------------------------------------------------------
# Ticket P3.1 — BrokerOrderBase contract: the ABC enforces the order surface
# ---------------------------------------------------------------------------

_ORDER_METHODS = (
    "place_order",
    "modify_order",
    "cancel_order",
    "get_order_book",
    "calculate_order_margin",
)


class _OrderStubBroker(BrokerOrderBase):
    """Fake broker implementing ALL FIVE order methods (P3.1 acceptance)."""

    broker_name = "stub-orders"

    def __init__(self) -> None:
        self.placed: list[BrokerOrder] = []
        self.modified: list[BrokerOrder] = []
        self.cancelled: list[BrokerOrder] = []

    def place_order(self, order: BrokerOrder) -> BrokerOrderId:
        self.placed.append(order)
        return BrokerOrderId("B-12345")

    def modify_order(self, order: BrokerOrder) -> None:
        self.modified.append(order)

    def cancel_order(self, order: BrokerOrder) -> None:
        self.cancelled.append(order)

    def get_order_book(self) -> list[BrokerOrder]:
        return list(self.placed)

    def calculate_order_margin(self, order: BrokerOrder) -> MarginInfo:
        return MarginInfo(initial_margin=1000.0, maintenance_margin=500.0)


def _partial_order_broker(drop: str) -> type[BrokerOrderBase]:
    """Build an order broker that implements everything except ``drop``."""
    body: dict[str, Any] = {
        "place_order": lambda self, order: BrokerOrderId("B-1"),
        "modify_order": lambda self, order: None,
        "cancel_order": lambda self, order: None,
        "get_order_book": lambda self: [],
        "calculate_order_margin": lambda self, order: MarginInfo(0.0, 0.0),
    }
    body.pop(drop)
    return type("PartialOrderBroker", (BrokerOrderBase,), body)


def test_order_base_class_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BrokerOrderBase()  # type: ignore[abstract]


@pytest.mark.parametrize("missing", _ORDER_METHODS)
def test_order_subclass_missing_abstract_method_raises_type_error(missing):
    with pytest.raises(TypeError):
        _partial_order_broker(missing)()


def test_all_five_order_methods_are_abstract():
    assert set(BrokerOrderBase.__abstractmethods__) == set(_ORDER_METHODS)


def test_complete_order_broker_instantiates():
    """The ticket's acceptance criterion: a fake broker implementing all
    five order methods can be instantiated."""
    broker = _OrderStubBroker()
    assert isinstance(broker, BrokerOrderBase)


def test_order_method_behaviour_contract():
    broker = _OrderStubBroker()
    order = BrokerOrder(symbol="DEMO", side="BUY", quantity=100)

    order_id = broker.place_order(order)
    assert isinstance(order_id, BrokerOrderId)
    assert broker.get_order_book() == [order]

    broker.modify_order(order)
    broker.cancel_order(order)
    assert broker.modified == [order]
    assert broker.cancelled == [order]

    margin = broker.calculate_order_margin(order)
    assert isinstance(margin, MarginInfo)
    assert margin.initial_margin == 1000.0


def test_auth_and_order_contracts_compose():
    """A live broker implements BOTH ABCs (the P3.2 MStockBroker shape):
    missing any of the nine methods must raise."""

    class _Combined(_StubBroker, BrokerOrderBase):  # auth-only stub, no orders

        pass

    with pytest.raises(TypeError):
        _Combined()  # type: ignore[abstract]

    class _FullBroker(_StubBroker, _OrderStubBroker):
        pass

    assert _FullBroker()  # all nine implemented → instantiable


# ---------------------------------------------------------------------------
# P3.1 value types
# ---------------------------------------------------------------------------


def test_broker_order_id_wraps_raw_string():
    oid = BrokerOrderId("550123")
    assert str(oid) == "550123"  # URL-safe in f"/orders/{oid}"
    assert oid == BrokerOrderId("550123")
    assert oid != BrokerOrderId("550124")
    assert hash(oid) == hash(BrokerOrderId("550123"))


def test_broker_order_defaults_and_fields():
    order = BrokerOrder(symbol="DEMO", quantity=10)
    assert order.side == "BUY"
    assert order.order_type == "MARKET"
    assert order.status == "OPEN"
    assert order.broker_order_id is None
    assert order.limit_price is None
    assert order.tag == {}
    order.tag["run"] = "x"  # per-instance, not shared across orders
    assert BrokerOrder(symbol="DEMO", quantity=10).tag == {}


def test_order_status_vocabulary():
    assert {"OPEN", "FILLED", "CANCELLED"} <= ORDER_STATUSES
    assert BrokerOrder(symbol="X", status="PARTIAL").status in ORDER_STATUSES


def test_margin_info_fields():
    info = MarginInfo(initial_margin=1000.0, maintenance_margin=500.0)
    assert info.available_margin is None
    assert info.is_funded is True
    assert MarginInfo(1.0, 0.5, 2.0, False).is_funded is False
