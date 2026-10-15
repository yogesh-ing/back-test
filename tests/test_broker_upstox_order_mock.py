"""Upstox v2 order HTTP calls, fully mocked.

Mirror of ``test_broker_dhan_order_mock.py`` for the Upstox order contract:
correct HTTP method + URL + ``Authorization: Bearer`` / ``Api-Version``
headers + JSON packet for each of the five order-contract methods plus
``poll_fill``; response parsing; instrument-key resolution; option
chain/quote surfaces; and fail-closed behaviour (no session / unmapped
symbol). **Never against a real account in CI — all HTTP is mocked.**
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.upstox import UpstoxBroker, UpstoxOrderError

FAKE_TOKEN = "upstox-jwt-token"
INSTRUMENT_KEYS = '{"RELIANCE": "NSE_EQ|INE002A01018", "NIFTY": "NSE_INDEX|Nifty 50"}'


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.ok = status_code < 400

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self) -> None:  # pragma: no cover — unused
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture()
def broker(monkeypatch) -> UpstoxBroker:
    monkeypatch.setenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:5000/api/broker/upstox/callback")
    monkeypatch.setenv("UPSTOX_API_BASE_URL", "https://api.upstox.test/v2")
    # Env map so no instrument-master download is ever attempted in CI.
    monkeypatch.setenv("UPSTOX_INSTRUMENT_KEYS", INSTRUMENT_KEYS)
    b = UpstoxBroker()
    b._access_token = FAKE_TOKEN
    b._expires_at = datetime.now() + timedelta(minutes=300)
    b._client_id = "api-key-123456"
    return b


def _mock_http(monkeypatch, outcome, calls: list | None = None):
    """Patch ``requests.request`` with one canned outcome; record every call.

    ``calls`` entries: (method, url, json_body, params, headers).
    """
    if calls is None:
        calls = []

    def fake(method, url, json=None, headers=None, params=None, timeout=None):
        calls.append((method, url, json, params, dict(headers or {})))
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
# fail-closed
# ---------------------------------------------------------------------------


def test_place_order_without_session_never_sends(monkeypatch) -> None:
    cold = UpstoxBroker()
    calls = _mock_http(monkeypatch, _FakeResponse(200, {}))
    with pytest.raises(UpstoxOrderError, match="No authenticated Upstox session"):
        cold.place_order(_order())
    assert calls == []  # nothing left the process


def test_place_order_unknown_symbol_fails_closed(broker, monkeypatch) -> None:
    calls = _mock_http(monkeypatch, _FakeResponse(200, {}))
    with pytest.raises(UpstoxOrderError, match="no Upstox instrument key"):
        broker.place_order(_order(symbol="UNKNOWN999"))
    assert calls == []


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


def test_place_order_packet_and_id(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"status": "success", "data": {"order_id": "261013000123456"}}))
    oid = broker.place_order(_order())
    assert isinstance(oid, BrokerOrderId) and str(oid) == "261013000123456"
    method, url, body, params, headers = calls[0]
    assert method == "POST"
    assert url == "https://api.upstox.test/v2/order/place"
    assert body == {
        "quantity": 10,
        "product": "I",
        "validity": "DAY",
        "price": 2500.0,
        "instrument_token": "NSE_EQ|INE002A01018",
        "order_type": "LIMIT",
        "transaction_type": "BUY",
        "disclosed_quantity": 0,
        "trigger_price": 0.0,
        "tag": "coid-123",
    }
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert headers["Api-Version"] == "2.0"


def test_place_order_market_price_forced_to_zero(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"data": {"order_id": "1"}}))
    broker.place_order(_order(order_type="MARKET", limit_price=None))
    assert calls[0][2]["price"] == 0.0
    assert calls[0][2]["order_type"] == "MARKET"


def test_place_order_maps_product_and_stop_types(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {"data": {"order_id": "1"}}))
    broker.place_order(_order(product="DELIVERY", order_type="STOP_LOSS", limit_price=2400.0))
    assert calls[0][2]["product"] == "D"
    assert calls[0][2]["order_type"] == "SL"

    broker.place_order(_order(order_type="STOP_LOSS_MARKET", limit_price=None))
    assert calls[1][2]["order_type"] == "SL-M"


def test_place_order_without_id_raises(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"status": "success"}))
    with pytest.raises(UpstoxOrderError, match="did not return an order id"):
        broker.place_order(_order())


def test_place_order_rejection_surfaces_reason(broker, monkeypatch):
    _mock_http(
        monkeypatch,
        _FakeResponse(400, {"status": "error", "errors": [{"message": "Invalid lot size"}]}),
    )
    with pytest.raises(UpstoxOrderError, match="Invalid lot size"):
        broker.place_order(_order())


def test_expired_session_maps_to_relogin(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(401, {"message": "unauthorized"}))
    with pytest.raises(UpstoxOrderError, match="log in again"):
        broker.place_order(_order())


# ---------------------------------------------------------------------------
# modify / cancel
# ---------------------------------------------------------------------------


def test_modify_order_packet(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {}))
    order = _order(broker_order_id=BrokerOrderId("261013000123456"))
    broker.modify_order(order)
    method, url, body, params, headers = calls[0]
    assert (method, url) == ("PUT", "https://api.upstox.test/v2/order/modify")
    assert body["order_id"] == "261013000123456"
    assert body["quantity"] == 10
    assert body["price"] == 2500.0
    assert body["order_type"] == "LIMIT"


def test_cancel_order_uses_query_param(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {}))
    broker.cancel_order(_order(broker_order_id=BrokerOrderId("261013000123456")))
    method, url, body, params, headers = calls[0]
    assert method == "DELETE"
    assert url == "https://api.upstox.test/v2/order/cancel"
    assert params == {"order_id": "261013000123456"}


def test_modify_without_broker_order_id_raises(broker, monkeypatch):
    calls = _mock_http(monkeypatch, _FakeResponse(200, {}))
    with pytest.raises(UpstoxOrderError, match="no broker_order_id"):
        broker.modify_order(_order())
    assert calls == []


# ---------------------------------------------------------------------------
# order book + poll_fill
# ---------------------------------------------------------------------------


def _book_row(**kw) -> dict[str, Any]:
    row = {
        "order_id": "261013000123456",
        "status": "open",
        "quantity": 10,
        "filled_quantity": 0,
        "price": 2500.0,
        "average_price": 0.0,
        "transaction_type": "BUY",
        "order_type": "LIMIT",
        "product": "I",
        "instrument_token": "NSE_EQ|INE002A01018",
        "tag": "coid-123",
        "created_at": "2026-10-13T09:20:00+05:30",
    }
    row.update(kw)
    return row


def test_get_order_book_maps_rows(broker, monkeypatch):
    _mock_http(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "status": "success",
                "data": [
                    _book_row(),
                    _book_row(
                        order_id="261013000999999",
                        status="complete",
                        filled_quantity=10,
                        average_price=2501.5,
                    ),
                    _book_row(order_id="261013000888888", status="cancelled"),
                ],
            },
        ),
    )
    orders = broker.get_order_book()
    assert [o.status for o in orders] == ["OPEN", "FILLED", "CANCELLED"]
    assert orders[1].filled_quantity == 10
    assert orders[1].average_fill_price == 2501.5
    assert orders[0].client_order_id == "coid-123"
    assert orders[0].symbol == "NSE_EQ|INE002A01018"


def test_get_order_book_partial_status(broker, monkeypatch):
    _mock_http(
        monkeypatch,
        _FakeResponse(200, {"data": [_book_row(status="open", filled_quantity=4)]}),
    )
    orders = broker.get_order_book()
    assert orders[0].status == "PARTIAL"


def test_poll_fill_open_returns_none(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"data": [_book_row()]}))
    assert broker.poll_fill("261013000123456") is None


def test_poll_fill_complete_returns_row(broker, monkeypatch):
    _mock_http(
        monkeypatch,
        _FakeResponse(
            200,
            {"data": [_book_row(status="complete", filled_quantity=10, average_price=2501.5)]},
        ),
    )
    fill = broker.poll_fill("261013000123456")
    assert fill is not None
    assert fill["order_id"] == "261013000123456"
    assert fill["filled_quantity"] == 10
    assert fill["price"] == 2501.5
    assert fill["transaction_type"] == "BUY"


def test_poll_fill_rejected_returns_none(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"data": [_book_row(status="rejected")]}))
    assert broker.poll_fill("261013000123456") is None


def test_poll_fill_unknown_id_returns_none(broker, monkeypatch):
    _mock_http(monkeypatch, _FakeResponse(200, {"data": [_book_row()]}))
    assert broker.poll_fill("does-not-exist") is None


# ---------------------------------------------------------------------------
# margin
# ---------------------------------------------------------------------------


def test_calculate_margin_packet_and_parse(broker, monkeypatch):
    calls: list = []

    def fake(method, url, json=None, headers=None, params=None, timeout=None):
        calls.append((method, url, json))
        if url.endswith("/charges/margin"):
            return _FakeResponse(200, {"data": {"required_margin": 52000.0, "final_margin": 52000.0}})
        return _FakeResponse(200, {"data": {"equity": {"available_margin": 90000.0}}})

    monkeypatch.setattr("requests.request", fake)
    info = broker.calculate_order_margin(_order())
    assert info.initial_margin == 52000.0
    assert info.maintenance_margin == 52000.0
    assert info.available_margin == 90000.0
    assert info.is_funded is True
    assert calls[0][1] == "https://api.upstox.test/v2/charges/margin"
    assert calls[0][2]["instruments"][0] == {
        "instrument_key": "NSE_EQ|INE002A01018",
        "quantity": 10,
        "transaction_type": "BUY",
        "product": "I",
        "price": 2500.0,
    }


def test_calculate_margin_unfunded(broker, monkeypatch):
    def fake(method, url, json=None, headers=None, params=None, timeout=None):
        if url.endswith("/charges/margin"):
            return _FakeResponse(200, {"data": {"required_margin": 52000.0}})
        return _FakeResponse(200, {"data": {"equity": {"available_margin": 100.0}}})

    monkeypatch.setattr("requests.request", fake)
    info = broker.calculate_order_margin(_order())
    assert info.is_funded is False


def test_calculate_margin_missing_amount_raises(broker, monkeypatch):
    def fake(method, url, json=None, headers=None, params=None, timeout=None):
        return _FakeResponse(200, {"data": {}})

    monkeypatch.setattr("requests.request", fake)
    with pytest.raises(UpstoxOrderError, match="no margin amount"):
        broker.calculate_order_margin(_order())


# ---------------------------------------------------------------------------
# option chain + option quotes (the LiveChainProvider contract)
# ---------------------------------------------------------------------------


def _option_row(**kw) -> dict[str, Any]:
    row = {
        "instrument_key": "NFO|NIFTY26OCT24500CE",
        "exchange": "NFO",
        "segment": "NFO",
        "trading_symbol": "NIFTY26OCT24500CE",
        "instrument_type": "CE",
        "strike_price": 24500,
        "expiry": "2026-10-29",
        "lot_size": 75,
        "tick_size": 0.05,
    }
    row.update(kw)
    return row


def test_get_option_chain_from_master_rows(broker, monkeypatch):
    import backtest.brokers.upstox as upstox_mod

    rows = [
        _option_row(),
        _option_row(
            instrument_key="NFO|NIFTY26OCT24600PE",
            trading_symbol="NIFTY26OCT24600PE",
            instrument_type="PE",
            strike_price=24600,
        ),
        # wrong underlying + non-option rows are skipped
        _option_row(trading_symbol="BANKNIFTY26OCT52000CE", strike_price=52000),
        _option_row(trading_symbol="RELIANCE", instrument_type="EQ"),
        # unparseable row (no strike anywhere) is dropped, not guessed
        _option_row(
            instrument_key="NFO|NIFTY26OCTCE",
            trading_symbol="NIFTY26OCTCE",
            strike_price=None,
            expiry=None,
        ),
    ]
    monkeypatch.setattr(upstox_mod, "_MASTER_ROWS_CACHE", rows)
    contracts = broker.get_option_chain("NIFTY")
    assert {c.trading_symbol for c in contracts} == {"NIFTY26OCT24500CE", "NIFTY26OCT24600PE"}
    for contract in contracts:
        assert contract.is_valid()
        assert contract.underlying == "NIFTY"
        assert contract.exchange == "NSE" and contract.segment == "NFO"
        assert contract.expiry == date(2026, 10, 29)
        assert contract.lot_size == 75
        assert contract.instrument_token.startswith("NFO|")


def test_get_option_chain_empty_master_returns_empty(broker, monkeypatch):
    import backtest.brokers.upstox as upstox_mod

    monkeypatch.setattr(upstox_mod, "_MASTER_ROWS_CACHE", [])
    assert broker.get_option_chain("NIFTY") == []


def test_get_option_quote_maps_depth(broker, monkeypatch):
    payload = {
        "status": "success",
        "data": {
            "NSE_EQ|INE002A01018": {
                "last_price": 2501.2,
                "volume": 123456,
                "oi": 0,
                "depth": {
                    "buy": [{"price": 2501.0, "quantity": 100}],
                    "sell": [{"price": 2501.5, "quantity": 50}],
                },
            }
        },
    }
    calls = _mock_http(monkeypatch, _FakeResponse(200, payload))
    quote = broker.get_option_quote("NSE_EQ|INE002A01018")
    assert quote == {
        "ltp": 2501.2,
        "bid": 2501.0,
        "ask": 2501.5,
        "volume": 123456.0,
        "oi": 0.0,
    }
    _, url, _, params, headers = calls[0]
    assert url == "https://api.upstox.test/v2/market-quote/quotes"
    assert params == {"instrument_key": "NSE_EQ|INE002A01018"}
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_get_option_quote_accepts_symbol_key(broker, monkeypatch):
    calls = _mock_http(
        monkeypatch,
        _FakeResponse(200, {"data": {"NSE_EQ|INE002A01018": {"last_price": 10.0}}}),
    )
    quote = broker.get_option_quote("RELIANCE")  # symbol → instrument key
    assert quote["ltp"] == 10.0
    assert calls[0][3] == {"instrument_key": "NSE_EQ|INE002A01018"}
