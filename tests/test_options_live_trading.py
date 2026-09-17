"""Tests for live option trading (Phase 4).

Covers:
- T4.1-T4.2: Order payload construction for options
- T4.3: Multi-leg orchestration (sequential submission)
- T4.4: Auto-cancel/rollback if a leg fails
- T4.5: Order status polling
- T4.6: Fill reconciliation
- T4.7: Handle partial fills, rejections
- T4.8: Retry logic with exponential backoff
- T4.9: Dry-run mode
- T4.11: Error logging
- T4.12: Integration test (dry-run)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.mstock import MStockBroker, MStockOrderError
from backtest.strategy.intent import (
    Direction,
    MarketView,
    OptionLeg,
    TradeIntent,
)
from backtest.options.live_trading import (
    LegResult,
    LegStatus,
    LiveOptionTrader,
    RetryConfig,
    StructureExecStatus,
    StructureResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_view() -> MarketView:
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
    )


def _make_long_call_intent() -> TradeIntent:
    leg = OptionLeg(
        instrument_token="T24800",
        trading_symbol="NIFTY26SEP24800CE",
        side="BUY",
        quantity=1,
        lot_size=25,
    )
    return TradeIntent(
        view=_make_view(),
        structure_type="long_call",
        legs=(leg,),
        expiry=date(2026, 9, 24),
        strategy_name="test_strategy",
        metadata={"option_type": "CE", "strike": "24800"},
    )


def _make_bull_call_spread_intent() -> TradeIntent:
    long_leg = OptionLeg(
        instrument_token="T24700",
        trading_symbol="NIFTY26SEP24700CE",
        side="BUY",
        quantity=1,
        lot_size=25,
    )
    short_leg = OptionLeg(
        instrument_token="T25000",
        trading_symbol="NIFTY26SEP25000CE",
        side="SELL",
        quantity=1,
        lot_size=25,
    )
    return TradeIntent(
        view=_make_view(),
        structure_type="bull_call_spread",
        legs=(long_leg, short_leg),
        expiry=date(2026, 9, 24),
        strategy_name="test_strategy",
        metadata={"option_type": "CE"},
    )


def _make_mock_broker() -> MStockBroker:
    """Create a mock MStockBroker for testing."""
    broker = MagicMock(spec=MStockBroker)
    broker._session_token = "test_token"
    broker._expires_at = datetime(2099, 1, 1)
    return broker


def _make_filled_order(order_id: str = "ORD123") -> MagicMock:
    """Create a mock filled order."""
    order = MagicMock()
    order.broker_order_id = order_id
    order.status = "EXECUTED"
    order.average_fill_price = 100.0
    order.filled_qty = 25
    return order


def _make_pending_order(order_id: str = "ORD123") -> MagicMock:
    """Create a mock pending order."""
    order = MagicMock()
    order.broker_order_id = order_id
    order.status = "OPEN"
    order.average_fill_price = None
    order.filled_qty = 0
    return order


# ---------------------------------------------------------------------------
# RetryConfig tests
# ---------------------------------------------------------------------------

class TestRetryConfig:
    def test_default_config(self):
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.base_delay_seconds == 1.0

    def test_delay_backoff(self):
        config = RetryConfig(base_delay_seconds=1.0, backoff_factor=2.0)
        assert config.delay_for_attempt(0) == 1.0
        assert config.delay_for_attempt(1) == 2.0
        assert config.delay_for_attempt(2) == 4.0

    def test_delay_max_cap(self):
        config = RetryConfig(
            base_delay_seconds=1.0,
            backoff_factor=10.0,
            max_delay_seconds=5.0,
        )
        assert config.delay_for_attempt(5) == 5.0  # capped


# ---------------------------------------------------------------------------
# StructureResult tests
# ---------------------------------------------------------------------------

class TestStructureResult:
    def test_all_filled(self):
        r = StructureResult(
            structure_id="S1",
            structure_type="long_call",
            legs=[
                LegResult(leg=MagicMock(), status=LegStatus.FILLED),
                LegResult(leg=MagicMock(), status=LegStatus.FILLED),
            ],
        )
        assert r.all_filled

    def test_not_all_filled(self):
        r = StructureResult(
            structure_id="S1",
            structure_type="long_call",
            legs=[
                LegResult(leg=MagicMock(), status=LegStatus.FILLED),
                LegResult(leg=MagicMock(), status=LegStatus.SUBMITTED),
            ],
        )
        assert not r.all_filled

    def test_any_rejected(self):
        r = StructureResult(
            structure_id="S1",
            structure_type="long_call",
            legs=[
                LegResult(leg=MagicMock(), status=LegStatus.FILLED),
                LegResult(leg=MagicMock(), status=LegStatus.REJECTED),
            ],
        )
        assert r.any_rejected

    def test_fill_count(self):
        r = StructureResult(
            structure_id="S1",
            structure_type="long_call",
            legs=[
                LegResult(leg=MagicMock(), status=LegStatus.FILLED),
                LegResult(leg=MagicMock(), status=LegStatus.SUBMITTED),
                LegResult(leg=MagicMock(), status=LegStatus.FILLED),
            ],
        )
        assert r.fill_count == 2


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_does_not_call_broker(self):
        broker = _make_mock_broker()
        trader = LiveOptionTrader(broker=broker, dry_run=True)

        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.FILLED
        assert result.all_filled
        broker.place_order.assert_not_called()

    def test_dry_run_records_leg_results(self):
        broker = _make_mock_broker()
        trader = LiveOptionTrader(broker=broker, dry_run=True)

        intent = _make_bull_call_spread_intent()
        result = trader.execute_structure(intent)

        assert len(result.legs) == 2
        assert all(leg.status == LegStatus.FILLED for leg in result.legs)
        assert all(leg.broker_order_id.startswith("DRY-") for leg in result.legs)

    def test_dry_run_logs_structure(self):
        broker = _make_mock_broker()
        trader = LiveOptionTrader(broker=broker, dry_run=True)

        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.structure_type == "long_call"
        assert result.completed_at is not None




# ---------------------------------------------------------------------------
# Fail-closed arming gate (architect review 2026-09-17 §3.2)
# ---------------------------------------------------------------------------


class TestFailClosedGate:
    """Live orders are impossible unless every gate is cleared explicitly."""

    def test_default_is_dry_run(self):
        broker = _make_mock_broker()
        trader = LiveOptionTrader(broker=broker)
        assert trader.dry_run is True
        result = trader.execute_structure(_make_long_call_intent())
        assert result.status == StructureExecStatus.FILLED
        broker.place_order.assert_not_called()

    def test_live_without_confirm_raises_before_any_broker_call(self):
        broker = _make_mock_broker()
        with pytest.raises(ValueError, match="confirm_live"):
            LiveOptionTrader(broker=broker, dry_run=False)
        broker.place_order.assert_not_called()

    def test_confirm_without_env_kill_switch_raises(self, monkeypatch):
        monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
        broker = _make_mock_broker()
        with pytest.raises(ValueError, match="ALLOW_LIVE_ORDERS"):
            LiveOptionTrader(broker=broker, dry_run=False, confirm_live=True)
        broker.place_order.assert_not_called()

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_env_kill_switch_off_values_raise(self, monkeypatch, value):
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", value)
        broker = _make_mock_broker()
        with pytest.raises(ValueError, match="ALLOW_LIVE_ORDERS"):
            LiveOptionTrader(broker=broker, dry_run=False, confirm_live=True)

    @pytest.mark.parametrize("value", ["1", "true", "YES"])
    def test_env_kill_switch_on_values_arm(self, monkeypatch, value):
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", value)
        trader = LiveOptionTrader(
            broker=_make_mock_broker(), dry_run=False, confirm_live=True
        )
        assert trader.dry_run is False

    def test_all_three_gates_arms_live_and_logs(self, monkeypatch, caplog):
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
        broker = _make_mock_broker()
        broker.place_order.return_value = BrokerOrderId("ORD001")
        broker.get_order_book.return_value = [_make_filled_order("ORD001")]
        with caplog.at_level(logging.WARNING, logger="backtest.options.live_trading"):
            trader = LiveOptionTrader(
                broker=broker,
                dry_run=False,
                confirm_live=True,
                poll_timeout_seconds=1.0,
            )
        assert trader.dry_run is False
        assert any("LIVE MODE ARMED" in rec.getMessage() for rec in caplog.records)

        result = trader.execute_structure(_make_long_call_intent())
        assert result.status == StructureExecStatus.FILLED
        broker.place_order.assert_called()

    def test_arming_one_instance_does_not_arm_the_next(self, monkeypatch):
        """The gate is per-instance — no process-wide leak."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
        armed = LiveOptionTrader(broker=_make_mock_broker(), dry_run=False, confirm_live=True)
        assert armed.dry_run is False
        safe = LiveOptionTrader(broker=_make_mock_broker())
        assert safe.dry_run is True
        assert safe.confirm_live is False


# ---------------------------------------------------------------------------
# Live execution — success path
# ---------------------------------------------------------------------------

class TestLiveExecutionSuccess:

    @pytest.fixture(autouse=True)
    def _allow_live_orders(self, monkeypatch):
        """These classes exercise the LIVE path against a mock broker."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    def test_single_leg_fills(self):
        broker = _make_mock_broker()
        broker.place_order.return_value = BrokerOrderId("ORD001")
        broker.get_order_book.return_value = [_make_filled_order("ORD001")]

        trader = LiveOptionTrader(broker=broker, dry_run=False, confirm_live=True, poll_timeout_seconds=1.0)
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.FILLED
        assert result.all_filled
        assert result.legs[0].broker_order_id == "ORD001"
        assert result.legs[0].fill_price == 100.0

    def test_multi_leg_fills(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = [BrokerOrderId("ORD001"), BrokerOrderId("ORD002")]
        broker.get_order_book.side_effect = [
            [_make_filled_order("ORD001")],
            [_make_filled_order("ORD002")],
        ]

        trader = LiveOptionTrader(broker=broker, dry_run=False, confirm_live=True, poll_timeout_seconds=1.0)
        intent = _make_bull_call_spread_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.FILLED
        assert len(result.legs) == 2
        assert result.all_filled


# ---------------------------------------------------------------------------
# Live execution — failure + rollback
# ---------------------------------------------------------------------------

class TestLiveExecutionRollback:

    @pytest.fixture(autouse=True)
    def _allow_live_orders(self, monkeypatch):
        """These classes exercise the LIVE path against a mock broker."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    def test_second_leg_rejects_rollback_first(self):
        broker = _make_mock_broker()
        # leg 1: submit + poll → filled
        # leg 2: submit fails with MStockOrderError (retry 0)
        # leg 2: retry fails again (retry 1)
        # rollback: flatten leg 1
        broker.place_order.side_effect = [
            BrokerOrderId("ORD001"),  # leg 1 submit
            MStockOrderError("Rejected: insufficient margin"),  # leg 2 attempt 0
            MStockOrderError("Rejected: insufficient margin"),  # leg 2 attempt 1
            BrokerOrderId("ROLL001"),  # rollback
        ]
        broker.get_order_book.side_effect = [
            [_make_filled_order("ORD001")],  # leg 1 poll → filled
        ]

        trader = LiveOptionTrader(
            broker=broker, dry_run=False, confirm_live=True,
            retry_config=RetryConfig(max_retries=1, base_delay_seconds=0.01),
            poll_timeout_seconds=1.0,
        )
        intent = _make_bull_call_spread_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.ROLLED_BACK
        assert result.any_rejected
        assert "Leg 2 failed" in result.error
        # Rollback was attempted
        assert broker.place_order.call_count == 4  # leg1 + leg2(×2 retries) + rollback

    def test_first_leg_rejects_no_rollback_needed(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = [
            MStockOrderError("Rejected: session expired"),
            MStockOrderError("Rejected: session expired"),  # retry 1
        ]
        broker.get_order_book.return_value = []

        trader = LiveOptionTrader(
            broker=broker, dry_run=False, confirm_live=True,
            retry_config=RetryConfig(max_retries=1, base_delay_seconds=0.01),
            poll_timeout_seconds=1.0,
        )
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.ROLLED_BACK
        assert result.any_rejected
        # No rollback needed (no filled legs to cancel)
        assert broker.place_order.call_count == 2  # 2 retries, no rollback


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:

    @pytest.fixture(autouse=True)
    def _allow_live_orders(self, monkeypatch):
        """These classes exercise the LIVE path against a mock broker."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    def test_retries_on_failure(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = [
            MStockOrderError("Timeout"),
            MStockOrderError("Timeout"),
            BrokerOrderId("ORD003"),  # succeeds on 3rd try
        ]
        broker.get_order_book.return_value = [_make_filled_order("ORD003")]

        trader = LiveOptionTrader(
            broker=broker,
            dry_run=False, confirm_live=True,
            retry_config=RetryConfig(max_retries=3, base_delay_seconds=0.01),
            poll_timeout_seconds=1.0,
        )
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.FILLED
        assert result.legs[0].retries == 2  # failed twice, succeeded on 3rd

    def test_exhausts_retries(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = MStockOrderError("Always fails")

        trader = LiveOptionTrader(
            broker=broker,
            dry_run=False, confirm_live=True,
            retry_config=RetryConfig(max_retries=2, base_delay_seconds=0.01),
            poll_timeout_seconds=1.0,
        )
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.ROLLED_BACK
        assert result.legs[0].status == LegStatus.FAILED
        assert result.legs[0].retries == 2


# ---------------------------------------------------------------------------
# Order payload construction
# ---------------------------------------------------------------------------

class TestOrderPayload:
    def test_build_broker_order(self):
        broker = _make_mock_broker()
        trader = LiveOptionTrader(broker=broker, dry_run=True)

        leg = OptionLeg(
            instrument_token="T24800",
            trading_symbol="NIFTY26SEP24800CE",
            side="BUY",
            quantity=2,
            lot_size=25,
        )
        order = trader._build_broker_order(leg, "STRUCT123", 0)

        assert order.symbol == "NIFTY26SEP24800CE"
        assert order.side == "BUY"
        assert order.quantity == 50  # 2 lots × 25
        assert order.order_type == "MARKET"
        assert order.exchange == "NFO"
        assert order.product == "INTRADAY"
        assert "STRUCT12" in order.client_order_id  # truncated to 8 chars


# ---------------------------------------------------------------------------
# Integration test (dry-run)
# ---------------------------------------------------------------------------

class TestIntegrationDryRun:
    def test_full_workflow_dry_run(self):
        """T4.12: Execute a bull call spread in dry-run mode."""
        broker = _make_mock_broker()
        trader = LiveOptionTrader(broker=broker, dry_run=True)

        intent = _make_bull_call_spread_intent()
        result = trader.execute_structure(intent)

        # Verify structure
        assert result.structure_type == "bull_call_spread"
        assert result.status == StructureExecStatus.FILLED
        assert result.all_filled
        assert len(result.legs) == 2
        assert result.completed_at is not None

        # Verify leg details
        long_leg = result.legs[0]
        short_leg = result.legs[1]
        assert long_leg.leg.side == "BUY"
        assert short_leg.leg.side == "SELL"
        assert long_leg.leg.trading_symbol == "NIFTY26SEP24700CE"
        assert short_leg.leg.trading_symbol == "NIFTY26SEP25000CE"

        # Verify query APIs
        assert trader.get_result(result.structure_id) is result
        assert len(trader.get_all_results()) == 1


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------

class TestErrorLogging:

    @pytest.fixture(autouse=True)
    def _allow_live_orders(self, monkeypatch):
        """These classes exercise the LIVE path against a mock broker."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    def test_rejection_logged(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = MStockOrderError("RMS rejection: max positions exceeded")

        trader = LiveOptionTrader(
            broker=broker,
            dry_run=False, confirm_live=True,
            retry_config=RetryConfig(max_retries=0, base_delay_seconds=0.01),
            poll_timeout_seconds=1.0,
        )
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert "RMS rejection" in result.legs[0].error
        assert result.error or result.legs[0].error

    def test_timeout_logged(self):
        broker = _make_mock_broker()
        broker.place_order.return_value = BrokerOrderId("ORD999")
        # Order never fills
        broker.get_order_book.return_value = [_make_pending_order("ORD999")]

        trader = LiveOptionTrader(
            broker=broker,
            dry_run=False, confirm_live=True,
            retry_config=RetryConfig(max_retries=0),
            poll_timeout_seconds=0.5,
        )
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert "not filled within timeout" in result.legs[0].error
