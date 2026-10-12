"""Phase C (closes F-12) — Dhan v2 order HTTP calls, fully mocked.

Mirror of ``test_broker_mstock_order_mock.py`` for the Dhan order contract:
correct HTTP method + URL + `access-token` header + JSON packet for each of
the five order-contract methods plus ``poll_fill``; response parsing; and
fail-closed behaviour (no session / expired session / unmapped symbol).
**Never against a real account in CI — all HTTP is mocked.**
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.dhan import DhanBroker, DhanOrderError

FAKE_TOKEN = "dhan-jwt-token"
CLIENT_ID = "1000000001"


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture()
def broker(monkeypatch) -> DhanBroker:
    monkeypatch.setenv("DHAN_API_BASE_URL", "https://api.dhan.test/v2")
    # Env map so no scrip-master download is ever attempted in CI.
    monkeypatch.setenv("DHAN_SECURITY_IDS", '{"RELIANCE": "2885", "TCS": "11536"}')
    b = DhanBroker()
    b._access_token = FAKE_TOKEN
    b._expires_at = datetime.now() + timedelta(minutes=300)
    b._client_id = CLIENT_ID
    return b


def _mock_http(monkeypatch, outcome, calls: list | None = None):
    """Patch ``requests.request`` with one canned outcome; record every call.

    ``calls`` entries: (method, url, json_body, headers).
    """
    if calls is None:
        calls = []

    def fake(method, url, json=None, headers=None, timeout=None):
        calls.append((method, url, json, dict(headers or {})))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("requests.request", fake)
    return calls


def _order(**kw) -> BrokerOrder:
    base = dict(
        symbol="RELIANCE",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        limit_price=2500.0,
        client_order_id="coid-123",
    )
    base.update(kw)
    return BrokerOrder(**base)


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


def test_place_order_packet_and_id(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"orderId": "112111182045"}))
    oid = broker.place_order(_order())
    assert isinstance(oid, BrokerOrderId) and str(oid) == "112111182045"
    method, url, body, headers = calls[0]
    assert method == "POST"
    assert url == "https://api.dhan.test/v2/orders"
    assert headers["access-token"] == FAKE_TOKEN
    assert body["dhanClientId"] == CLIENT_ID
    assert body["transactionType"] == "BUY"
    assert body["exchangeSegment"] == "NSE_EQ"
    assert body["productType"] == "INTRADAY"
    assert body["orderType"] == "LIMIT"
    assert body["securityId"] == "2885"
    assert body["quantity"] == 10
    assert body["price"] == 2500.0
    assert body["correlationId"] == "coid-123"


def test_place_order_market_price_zero(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"orderId": "1"}))
    broker.place_order(_order(order_type="MARKET", limit_price=None))
    body = calls[0][2]
    assert body["orderType"] == "MARKET"
    assert body["price"] == 0.0


def test_place_order_no_id_raises(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"orderStatus": "TRANSIT"}))
    with pytest.raises(DhanOrderError, match="order id"):
        broker.place_order(_order())


def test_place_order_rejection_raises(broker, monkeypatch):
    _mock_http(
        monkeypatch,
        _FakeResponse(400, {"errorMessage": "Insufficient funds"}),
    )
    with pytest.raises(DhanOrderError):
        broker.place_order(_order())


def test_place_order_expired_session_raises(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(401, {"errorMessage": "invalid token"}))
    with pytest.raises(DhanOrderError, match="expired|unauthorized"):
        broker.place_order(_order())


def test_order_call_without_session_never_sends(broker, monkeypatch):
    broker._access_token = None
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"orderId": "1"}))
    with pytest.raises(DhanOrderError, match="fail-closed"):
        broker.place_order(_order())
    assert calls == []  # no bytes left the process


def test_unmapped_symbol_fails_closed(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"orderId": "1"}))
    monkeypatch.setattr("backtest.brokers.dhan._load_scrip_master", lambda *a, **k: {})
    with pytest.raises(DhanOrderError, match="security id"):
        broker.place_order(_order(symbol="UNKNOWN"))
    assert calls == []


# ---------------------------------------------------------------------------
# modify / cancel
# ---------------------------------------------------------------------------


def test_modify_order(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"orderId": "42", "orderStatus": "TRANSIT"}))
    broker.modify_order(_order(broker_order_id=BrokerOrderId("42"), quantity=5, limit_price=2450))
    method, url, body, _ = calls[0]
    assert (method, url) == ("PUT", "https://api.dhan.test/v2/orders/42")
    assert body["orderId"] == "42"
    assert body["quantity"] == 5
    assert body["price"] == 2450.0


def test_cancel_order(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"orderStatus": "CANCELLED"}))
    broker.cancel_order(_order(broker_order_id=BrokerOrderId("42")))
    assert (calls[0][0], calls[0][1]) == ("DELETE", "https://api.dhan.test/v2/orders/42")


def test_modify_without_id_raises(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {}))
    with pytest.raises(DhanOrderError, match="broker_order_id"):
        broker.modify_order(_order())
    assert calls == []


# ---------------------------------------------------------------------------
# order book + poll_fill
# ---------------------------------------------------------------------------

_BOOK = [
    {
        "orderId": "111",
        "orderStatus": "TRADED",
        "tradingSymbol": "RELIANCE",
        "transactionType": "BUY",
        "quantity": 10,
        "filledQty": 10,
        "price": 2500.0,
        "averageTradedPrice": 2499.5,
        "orderType": "LIMIT",
        "productType": "INTRADAY",
        "exchangeSegment": "NSE_EQ",
        "correlationId": "coid-123",
    },
    {
        "orderId": "222",
        "orderStatus": "PENDING",
        "tradingSymbol": "TCS",
        "transactionType": "SELL",
        "quantity": 4,
        "filledQty": 0,
        "price": 3900.0,
    },
]


def test_get_order_book(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, _BOOK))
    orders = broker.get_order_book()
    assert (calls[0][0], calls[0][1]) == ("GET", "https://api.dhan.test/v2/orders")
    assert len(orders) == 2
    assert str(orders[0].broker_order_id) == "111"
    assert orders[0].status == "TRADED"
    assert orders[0].filled_quantity == 10
    assert orders[0].average_fill_price == 2499.5
    assert orders[1].status == "PENDING"


def test_poll_fill_traded(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, _BOOK))
    row = broker.poll_fill("111")
    assert row is not None
    assert row["tradingsymbol"] == "RELIANCE"
    assert row["transaction_type"] == "BUY"
    assert row["filled_quantity"] == 10
    assert row["price"] == 2499.5  # average, not requested


def test_poll_fill_pending_and_unknown(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, _BOOK))
    assert broker.poll_fill("222") is None  # still open
    assert broker.poll_fill("999") is None  # unknown id → not-yet-filled


# ---------------------------------------------------------------------------
# margin
# ---------------------------------------------------------------------------


def test_calculate_order_margin(broker, monkeypatch):
    calls = _mock_http(
        monkeypatch,
        _FakeResponse(
            200,
            {"totalMargin": 24500.0, "availableBalance": 100000.0, "insufficientBalance": 0.0},
        ),
    )
    info = broker.calculate_order_margin(_order())
    method, url, body, _ = calls[0]
    assert (method, url) == ("POST", "https://api.dhan.test/v2/margincalculator")
    assert body["securityId"] == "2885"
    assert info.initial_margin == 24500.0
    assert info.available_margin == 100000.0
    assert info.is_funded is True


def test_margin_insufficient(broker, monkeypatch):
    _mock_http(
        monkeypatch,
        _FakeResponse(
            200,
            {"totalMargin": 24500.0, "availableBalance": 1000.0, "insufficientBalance": 23500.0},
        ),
    )
    assert broker.calculate_order_margin(_order()).is_funded is False


def test_margin_no_amount_raises(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"status": "ok"}))
    with pytest.raises(DhanOrderError, match="margin"):
        broker.calculate_order_margin(_order())
