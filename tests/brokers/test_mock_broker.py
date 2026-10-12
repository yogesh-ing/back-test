"""Gap-PRD P5 — MockBroker + literal ``--source mock_broker`` mode.

Acceptance (PRD G4.3):
* ``--source mock_broker`` → no authentication required;
* option chain returns synthetic NIFTY/BANKNIFTY contracts;
* orders are logged but never submitted to a real broker.

Plus the safety property the PRD sketch did not spell out: the mock is
OPT-IN — it must never appear in the default broker registry, or a dry-run
venue could silently show up in a production login UI.
"""

from __future__ import annotations

import logging

from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    BrokerAuthBase,
    BrokerOrder,
    BrokerOrderBase,
)
from backtest.brokers.mock import MockBroker
from backtest.brokers import session_manager as sm


def _cleanup_registry():
    sm._BROKER_REGISTRY.pop("mock", None)


class TestAuthContract:
    def test_no_credentials_required(self):
        broker = MockBroker()
        assert broker.is_authenticated() is True  # authenticated from birth
        out = broker.login("", "")
        assert out["success"] is True and out["requires_totp"] is False
        assert broker.verify_totp("")["success"] is True

    def test_session_status_shape(self):
        status = MockBroker().get_session_status()
        assert status["status"] == STATUS_AUTHENTICATED
        assert status["broker"] == "mock"
        assert status["expires_at"]  # far future, never nags the expiry monitor

    def test_logout_clears_and_login_rearms(self):
        broker = MockBroker()
        broker.logout()
        assert broker.is_authenticated() is False
        broker.login("anything", "anything")
        assert broker.is_authenticated() is True

    def test_implements_both_broker_abcs(self):
        broker = MockBroker()
        assert isinstance(broker, BrokerAuthBase)
        assert isinstance(broker, BrokerOrderBase)


class TestMarketData:
    def test_chain_returns_synthetic_contracts_both_sides(self):
        contracts = MockBroker().get_option_chain("NIFTY")
        assert contracts
        types = {str(getattr(c.option_type, "value", c.option_type)) for c in contracts}
        assert {"CE", "PE"} <= types
        assert all(c.underlying == "NIFTY" for c in contracts)

    def test_quote_for_a_chain_token_is_bs_priced(self):
        broker = MockBroker()
        contract = broker.get_option_chain("NIFTY")[0]
        quote = broker.get_option_quote(contract.instrument_token)
        assert quote["ltp"] > 0
        assert quote["bid"] < quote["ask"]
        assert quote["mock"] is True

    def test_unknown_token_gets_the_flat_default(self):
        assert MockBroker().get_option_quote("NO-SUCH-TOKEN")["ltp"] == 150.0


class TestDryRunOrders:
    def test_order_is_logged_and_filed_open_never_submitted(self, caplog):
        broker = MockBroker()
        order = BrokerOrder(symbol="NIFTYTEST24800CE", side="BUY", quantity=75)
        with caplog.at_level(logging.INFO, logger="backtest.brokers.mock"):
            order_id = broker.place_order(order)
        assert str(order_id).startswith("MOCK")
        assert any("DRY-RUN" in rec.message for rec in caplog.records)
        book = broker.get_order_book()
        assert len(book) == 1 and book[0].status == "OPEN"

    def test_order_ids_are_deterministic_not_random(self):
        broker = MockBroker()
        first = broker.place_order(BrokerOrder(symbol="A", side="BUY", quantity=1))
        second = broker.place_order(BrokerOrder(symbol="B", side="SELL", quantity=1))
        assert (str(first), str(second)) == ("MOCK000001", "MOCK000002")

    def test_prd_sketch_dict_payload_still_works(self):
        broker = MockBroker()
        order_id = broker.place_order({"symbol": "NIFTY", "side": "BUY", "quantity": 75})
        assert str(order_id).startswith("MOCK")

    def test_poll_fill_never_invents_a_fill(self):
        broker = MockBroker()
        order_id = broker.place_order(BrokerOrder(symbol="A", side="BUY", quantity=1))
        assert broker.poll_fill(order_id) is None

    def test_cancel_and_modify_touch_only_the_local_book(self):
        broker = MockBroker()
        order = BrokerOrder(symbol="A", side="BUY", quantity=1)
        broker.place_order(order)
        order.quantity = 2
        broker.modify_order(order)
        broker.cancel_order(order)
        assert broker.get_order_book()[0].status == "CANCELLED"

    def test_margin_check_is_always_funded(self):
        info = MockBroker().calculate_order_margin(
            BrokerOrder(symbol="A", side="BUY", quantity=1)
        )
        assert info.is_funded is True and info.initial_margin == 0.0


class TestOptInRegistration:
    def test_mock_is_NOT_in_the_default_registry(self):
        _cleanup_registry()
        names = [b["name"] for b in sm.available_brokers()]
        assert "mock" not in names  # production login UI stays clean

    def test_enable_mock_broker_registers_it(self):
        try:
            sm.enable_mock_broker()
            names = [b["name"] for b in sm.available_brokers()]
            assert "mock" in names
            display = {b["name"]: b["display_name"] for b in sm.available_brokers()}
            assert display["mock"] == "Mock (dry-run)"
            broker = sm._BROKER_REGISTRY["mock"]()
            assert isinstance(broker, MockBroker)
        finally:
            _cleanup_registry()

    def test_enable_is_idempotent(self):
        try:
            sm.enable_mock_broker()
            sm.enable_mock_broker()
            assert [b["name"] for b in sm.available_brokers()].count("mock") == 1
        finally:
            _cleanup_registry()


class TestAppSourceMode:
    def test_create_app_mock_broker_boots_without_credentials(self):
        from backtest.web.app import create_app

        try:
            app = create_app(source="mock_broker")
            client = app.test_client()
            assert client.get("/health").status_code == 200
            # Data pipeline runs synthetic (known source tags downstream).
            assert app.config["BACKTEST_SOURCE"] == "synthetic"
            # The dry-run venue is now offered by the registry…
            brokers = client.get("/api/broker/list").get_json()
            names = [b["name"] for b in brokers.get("brokers", brokers)]
            assert "mock" in names
        finally:
            _cleanup_registry()

    def test_default_app_does_not_offer_the_mock(self):
        from backtest.web.app import create_app

        _cleanup_registry()
        app = create_app(source="synthetic")
        client = app.test_client()
        brokers = client.get("/api/broker/list").get_json()
        names = [b["name"] for b in brokers.get("brokers", brokers)]
        assert "mock" not in names
