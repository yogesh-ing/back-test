"""Ticket P3.2 — mStock order HTTP calls, fully mocked.

Every test asserts the correct HTTP method + URL + auth header + payload
for each of the five order-contract methods, response parsing, and that
non-200 / error payloads raise :class:`MStockOrderError`. **Never against
a real account in CI — all HTTP is mocked.**
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from backtest.brokers import (
    MStockBroker,
    MStockOrderError,
    BrokerOrder,
    BrokerOrderId,
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


@pytest.fixture()
def live_broker(broker) -> MStockBroker:
    """A broker with an in-memory session (white-box: token + expiry set)."""
    broker._session_token = FAKE_TOKEN
    broker._expires_at = datetime.now() + timedelta(minutes=300)
    return broker


def _mock_http(monkeypatch, outcome: _FakeResponse | Exception, calls: list | None = None):
    """Patch all four verbs with ONE canned outcome; record every call.

    ``calls`` entries: (method, url, data, json, headers).
    """
    if calls is None:
        calls = []

    def _make(method: str):
        def fake(url, data=None, json=None, headers=None, timeout=None):
            if calls is not None:
                calls.append((method, url, dict(data or {}), json, dict(headers or {})))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return fake

    monkeypatch.setattr("requests.get", _make("GET"))
    monkeypatch.setattr("requests.post", _make("POST"))
    monkeypatch.setattr("requests.put", _make("PUT"))
    monkeypatch.setattr("requests.delete", _make("DELETE"))
    return calls if calls is not None else []


def _assert_auth_headers(headers: dict, token: str = FAKE_TOKEN) -> None:
    assert headers["Authorization"] == f"token test-api-key:{token}"
    assert headers["X-Mirae-Version"] == "1"


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


def test_place_order_hits_regular_endpoint_with_auth(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"order_id": "550123"}))
    order = BrokerOrder(client_order_id="PRT-x1", symbol="reliance", quantity=10)

    result = live_broker.place_order(order)

    assert result == BrokerOrderId("550123")
    method, url, data, json_body, headers = calls[0]
    assert method == "POST"
    assert url == "https://api.mstock.test/openapi/typea/orders/regular"
    assert json_body is None  # form packet
    assert data == {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "transaction_type": "BUY",
        "order_type": "MARKET",
        "quantity": 10,
        "product": "INTRADAY",
        "validity": "DAY",
        "price": 0,
        "trigger_price": 0,
        "disclosed_quantity": 0,
        "tag": "",
    }
    _assert_auth_headers(headers)


def test_place_order_limit_price_mapped(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"data": {"order_id": 77}}))
    order = BrokerOrder(symbol="DEMO", side="SELL", quantity=5,
                        order_type="LIMIT", limit_price=999.5, product="DELIVERY")

    result = live_broker.place_order(order)

    # int order ids from the data-wrapped shape parse too
    assert result == BrokerOrderId("77")
    data = calls[0][2]
    assert data["transaction_type"] == "SELL"
    assert data["order_type"] == "LIMIT"
    assert data["price"] == 999.5
    assert data["product"] == "DELIVERY"


def test_place_order_unauthenticated_raises_without_http(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"order_id": "1"}))
    with pytest.raises(MStockOrderError, match="no active mStock session"):
        broker.place_order(BrokerOrder(symbol="DEMO", quantity=1))
    assert calls == []


def test_place_order_non_200_raises(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(400, {"error_message": "invalid quantity"}))
    with pytest.raises(MStockOrderError, match="invalid quantity"):
        live_broker.place_order(BrokerOrder(symbol="DEMO", quantity=0))


def test_place_order_error_payload_with_200_raises(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"status": "error", "message": "halted"}))
    with pytest.raises(MStockOrderError, match="halted"):
        live_broker.place_order(BrokerOrder(symbol="DEMO", quantity=1))


def test_place_order_missing_id_raises(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"status": "success"}))
    with pytest.raises(MStockOrderError, match="order id"):
        live_broker.place_order(BrokerOrder(symbol="DEMO", quantity=1))


def test_expired_session_cannot_place_orders(broker, monkeypatch):
    broker._session_token = FAKE_TOKEN
    broker._expires_at = datetime.now() - timedelta(minutes=1)  # already expired
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"order_id": "1"}))
    with pytest.raises(MStockOrderError, match="no active mStock session"):
        broker.place_order(BrokerOrder(symbol="DEMO", quantity=1))
    assert calls == []


# ---------------------------------------------------------------------------
# modify_order / cancel_order
# ---------------------------------------------------------------------------


def test_modify_order_hits_put_with_broker_id(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"status": "success"}))
    order = BrokerOrder(broker_order_id=BrokerOrderId("550123"), symbol="DEMO",
                        quantity=20, order_type="LIMIT", limit_price=101.0)

    assert live_broker.modify_order(order) is None

    method, url, data, json_body, headers = calls[0]
    assert method == "PUT"
    assert url == "https://api.mstock.test/openapi/typea/orders/regular/550123"
    assert data["quantity"] == 20 and data["price"] == 101.0
    _assert_auth_headers(headers)


def test_cancel_order_hits_delete_with_broker_id(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"status": "success"}))
    order = BrokerOrder(broker_order_id=BrokerOrderId("550123"), symbol="DEMO", quantity=10)

    assert live_broker.cancel_order(order) is None

    method, url, data, _, headers = calls[0]
    assert method == "DELETE"
    assert url == "https://api.mstock.test/openapi/typea/orders/regular/550123"
    assert data == {}  # cancel carries no body
    _assert_auth_headers(headers)


def test_modify_cancel_without_broker_id_raises_without_http(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {}))
    bare = BrokerOrder(symbol="DEMO", quantity=1)
    with pytest.raises(MStockOrderError, match="broker_order_id"):
        live_broker.modify_order(bare)
    with pytest.raises(MStockOrderError, match="broker_order_id"):
        live_broker.cancel_order(bare)
    assert calls == []


def test_cancel_order_rejected_by_broker_raises(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(409, {"error_message": "order already executed"}))
    order = BrokerOrder(broker_order_id=BrokerOrderId("550123"), symbol="DEMO", quantity=1)
    with pytest.raises(MStockOrderError, match="already executed"):
        live_broker.cancel_order(order)


# ---------------------------------------------------------------------------
# get_order_book
# ---------------------------------------------------------------------------

_BOOK_ROW = {
    "order_id": "550123",
    "tradingsymbol": "RELIANCE",
    "transaction_type": "BUY",
    "quantity": 10,
    "order_type": "MARKET",
    "status": "COMPLETE",
    "filled_quantity": 10,
    "average_price": 1010.25,
    "exchange": "NSE",
    "product": "INTRADAY",
}


def test_get_order_book_hits_get_and_maps_rows(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, [_BOOK_ROW]))

    book = live_broker.get_order_book()

    method, url, _, _, headers = calls[0]
    assert method == "GET"
    assert url == "https://api.mstock.test/openapi/typea/orders"
    _assert_auth_headers(headers)
    assert len(book) == 1
    row = book[0]
    assert isinstance(row, BrokerOrder)
    assert row.broker_order_id == BrokerOrderId("550123")
    assert row.symbol == "RELIANCE"
    assert row.side == "BUY"
    assert row.quantity == 10
    assert row.status == "COMPLETE"
    assert row.average_fill_price == 1010.25


def test_get_order_book_data_wrapped_shape(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"data": [_BOOK_ROW]}))
    book = live_broker.get_order_book()
    assert [b.broker_order_id for b in book] == [BrokerOrderId("550123")]


def test_get_order_book_empty(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, []))
    assert live_broker.get_order_book() == []


# ---------------------------------------------------------------------------
# calculate_order_margin
# ---------------------------------------------------------------------------


def test_calculate_order_margin_hits_json_endpoint(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {
        "initial_margin": 50100.0, "maintenance_margin": 25050.0,
        "available_margin": 100000.0,
    }))
    order = BrokerOrder(symbol="RELIANCE", quantity=10)

    margin = live_broker.calculate_order_margin(order)

    method, url, data, json_body, headers = calls[0]
    assert method == "POST"
    assert url == "https://api.mstock.test/openapi/typea/margins/orders"
    assert data == {}  # JSON packet, not a form
    assert json_body["tradingsymbol"] == "RELIANCE"
    assert headers["Content-Type"] == "application/json"
    _assert_auth_headers(headers)
    assert margin.initial_margin == 50100.0
    assert margin.maintenance_margin == 25050.0
    assert margin.available_margin == 100000.0
    assert margin.is_funded is True


def test_calculate_order_margin_unfunded(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {
        "data": {"initial_margin": 50100.0, "available_margin": 1000.0},
    }))
    margin = live_broker.calculate_order_margin(BrokerOrder(symbol="DEMO", quantity=10))
    assert margin.is_funded is False
    assert margin.maintenance_margin == 50100.0  # falls back to initial


def test_calculate_order_margin_without_amount_raises(live_broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"status": "success"}))
    with pytest.raises(MStockOrderError, match="no margin amount"):
        live_broker.calculate_order_margin(BrokerOrder(symbol="DEMO", quantity=1))


# ---------------------------------------------------------------------------
# The session from the auth flow is the one the orders reuse
# ---------------------------------------------------------------------------


def test_orders_reuse_session_from_login_totp(broker, monkeypatch):
    """Full flow: login → TOTP (token from the server) → place_order sends
    exactly that token in the Authorization header."""
    calls: list = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(("POST", url, dict(data or {}), None, dict(headers or {})))
        if "connect/login" in url:
            return _FakeResponse(200, {"status": "success"})
        if "verifytotp" in url:
            return _FakeResponse(200, {"status": "success", "access_token": "sess-from-server"})
        if "orders/regular" in url:
            return _FakeResponse(200, {"order_id": "9"})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr("requests.post", fake_post)

    assert broker.login("user", "pass")["success"]  # noqa: S106 - test fixture
    assert broker.verify_totp("123456")["success"]

    result = broker.place_order(BrokerOrder(symbol="DEMO", quantity=1))

    assert result == BrokerOrderId("9")
    order_calls = [c for c in calls if "orders/regular" in c[1]]
    assert len(order_calls) == 1
    assert order_calls[0][4]["Authorization"] == "token test-api-key:sess-from-server"


def test_logout_disables_orders(live_broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"order_id": "1"}))
    live_broker.logout()
    with pytest.raises(MStockOrderError, match="no active mStock session"):
        live_broker.get_order_book()
    assert calls == []


def test_mstock_broker_is_composable_contract_member(live_broker):
    from backtest.brokers import BrokerAuthBase, BrokerOrderBase

    assert isinstance(live_broker, BrokerAuthBase)
    assert isinstance(live_broker, BrokerOrderBase)
