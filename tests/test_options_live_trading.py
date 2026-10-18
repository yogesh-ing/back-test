"""Tests for live option trading (Phase 4).

Covers:
- T4.1-T4.2: Order payload construction for options
- T4.3: Multi-leg orchestration (sequential submission)
- T4.4: Auto-cancel/rollback if a leg fails
- T4.5: Order status polling
- T4.6: Fill reconciliation
- T4.7: Handle partial fills, rejections
- T4.8: No blind retry — one placement per decision, reconcile instead
- T4.9: Dry-run mode
- T4.11: Error logging
- T4.12: Integration test (dry-run)
- Intent journal wiring: idempotency keys, decided/submitted/filled/halted events
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.mstock import (
    MStockBroker,
    MStockOrderError,
    OrderOutcomeUnknown,
)
from backtest.options.execution_gate import RESEARCH_STRUCTURES
from backtest.options.intent_journal import (
    KIND_DECIDED,
    KIND_FILLED,
    KIND_HALTED,
    KIND_RECONCILED,
    KIND_REJECTED,
    KIND_SUBMITTED,
    KIND_UNCERTAIN,
    MemoryJournal,
    intent_key,
)
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
        bar_timestamp=datetime(2026, 10, 5, 4, 30),
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


def _research_trader(broker, **kwargs) -> LiveOptionTrader:
    """Trader widened to the research profile — the multi-leg mechanics tests
    submit spreads, which the V1 execution gate refuses by design."""
    kwargs.setdefault("allowed_structures", RESEARCH_STRUCTURES)
    return LiveOptionTrader(broker=broker, **kwargs)


def _make_filled_order(order_id: str = "ORD123") -> BrokerOrder:
    """A real ``BrokerOrder`` as the order book would report it, executed."""
    return BrokerOrder(
        broker_order_id=BrokerOrderId(order_id),
        symbol="NIFTY26SEP24800CE",
        side="BUY",
        quantity=25,
        status="EXECUTED",
        filled_quantity=25,
        average_fill_price=100.0,
    )


def _make_pending_order(order_id: str = "ORD123") -> BrokerOrder:
    """An accepted order the broker has not filled yet."""
    return BrokerOrder(
        broker_order_id=BrokerOrderId(order_id),
        symbol="NIFTY26SEP24800CE",
        side="BUY",
        quantity=25,
        status="OPEN",
        filled_quantity=0,
        average_fill_price=None,
    )


def _live_trader(broker, **kwargs) -> LiveOptionTrader:
    """Trader armed for live against a mock broker (env gate already set)."""
    kwargs.setdefault("dry_run", False)
    kwargs.setdefault("confirm_live", True)
    kwargs.setdefault("poll_timeout_seconds", 1.0)
    return LiveOptionTrader(broker=broker, **kwargs)


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
        trader = _research_trader(_make_mock_broker(), dry_run=True)

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

        trader = _research_trader(
            broker, dry_run=False, confirm_live=True, poll_timeout_seconds=1.0
        )
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
        # leg 2: submit fails with MStockOrderError (no retry)
        # rollback: flatten leg 1
        broker.place_order.side_effect = [
            BrokerOrderId("ORD001"),  # leg 1 submit
            MStockOrderError("Rejected: insufficient margin"),  # leg 2
            BrokerOrderId("ROLL001"),  # rollback
        ]
        broker.get_order_book.side_effect = [
            [_make_filled_order("ORD001")],  # leg 1 poll → filled
        ]

        trader = _research_trader(
            broker, dry_run=False, confirm_live=True, poll_timeout_seconds=1.0
        )
        intent = _make_bull_call_spread_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.ROLLED_BACK
        assert result.any_rejected
        assert "Leg 2 failed" in result.error
        # Rollback was attempted — no retries on the reject
        assert broker.place_order.call_count == 3

    def test_first_leg_rejects_no_rollback_needed(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = [
            MStockOrderError("Rejected: session expired"),
        ]
        broker.get_order_book.return_value = []

        trader = LiveOptionTrader(
            broker=broker, dry_run=False, confirm_live=True, poll_timeout_seconds=1.0
        )
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.ROLLED_BACK
        assert result.any_rejected
        # No rollback needed (no filled legs to cancel), one attempt only
        assert broker.place_order.call_count == 1


# ---------------------------------------------------------------------------
# No blind retry — idempotency keys and reconcile-once
# ---------------------------------------------------------------------------


class TestNoBlindRetry:

    @pytest.fixture(autouse=True)
    def _allow_live_orders(self, monkeypatch):
        """These classes exercise the LIVE path against a mock broker."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")

    def test_a_definite_reject_is_not_retried(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = MStockOrderError("RMS: max positions exceeded")

        trader = _live_trader(broker)
        result = trader.execute_structure(_make_long_call_intent())

        assert result.legs[0].status == LegStatus.REJECTED
        assert not result.legs[0].uncertain
        assert broker.place_order.call_count == 1

    def test_an_uncertain_submit_halts_and_does_not_retry(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = OrderOutcomeUnknown("gateway 502")
        broker.get_order_book.return_value = []

        journal = MemoryJournal()
        trader = _live_trader(broker, journal=journal)
        ts = datetime(2026, 10, 5, 4, 30)
        result = trader.execute_structure(_make_long_call_intent(), timestamp=ts)

        assert result.status == StructureExecStatus.HALTED
        assert result.legs[0].uncertain
        assert broker.place_order.call_count == 1
        # The uncertain event is recorded with dedupe=False (it may repeat if
        # the operator retries after a failed reconcile), so check kinds() instead.
        assert KIND_UNCERTAIN in journal.kinds(result.intent_id)

    def test_reconciliation_finds_the_ticket_and_sets_it_filled(self):
        broker = _make_mock_broker()
        broker.place_order.side_effect = OrderOutcomeUnknown("timeout")
        # The order book already has the ticket, executed.
        broker.get_order_book.return_value = [_make_filled_order("ORD-RECON")]

        journal = MemoryJournal()
        trader = _live_trader(broker, journal=journal)
        result = trader.execute_structure(_make_long_call_intent())

        assert result.legs[0].status == LegStatus.FILLED
        assert not result.legs[0].uncertain
        assert result.legs[0].broker_order_id == "ORD-RECON"
        assert journal.order_id_for(result.intent_id, 0) == "ORD-RECON"

    def test_a_replayed_decision_skips_place_order(self):
        """The idempotency key prevents a second ``place_order`` for the same leg."""
        broker = _make_mock_broker()
        broker.get_order_book.return_value = [_make_filled_order("ORD-REPLAY")]

        journal = MemoryJournal()
        intent = _make_long_call_intent()
        ts = datetime(2026, 10, 5, 4, 30)
        iid = intent_key(intent, clock=ts)

        # Pre-seed: the previous run submitted this leg and we lost the answer.
        journal.append(
            intent_id=iid, kind=KIND_SUBMITTED, leg_index=0, broker_order_id="ORD-REPLAY"
        )

        trader = _live_trader(broker, journal=journal)
        result = trader.execute_structure(intent, timestamp=ts)

        assert result.legs[0].status == LegStatus.FILLED
        assert result.legs[0].broker_order_id == "ORD-REPLAY"
        assert broker.place_order.call_count == 0

    def test_poll_timeout_is_uncertain_and_halts(self):
        broker = _make_mock_broker()
        broker.place_order.return_value = BrokerOrderId("ORD-TIMEOUT")
        broker.get_order_book.return_value = [_make_pending_order("ORD-TIMEOUT")]

        journal = MemoryJournal()
        trader = _live_trader(broker, journal=journal, poll_timeout_seconds=0.2)
        result = trader.execute_structure(_make_long_call_intent())

        assert result.legs[0].uncertain
        assert result.status == StructureExecStatus.HALTED
        assert journal.has(result.intent_id, KIND_UNCERTAIN, 0)
        assert journal.order_id_for(result.intent_id, 0) == "ORD-TIMEOUT"

    def test_journal_records_decided_submitted_and_filled(self):
        broker = _make_mock_broker()
        broker.place_order.return_value = BrokerOrderId("ORD-J")
        broker.get_order_book.return_value = [_make_filled_order("ORD-J")]

        journal = MemoryJournal()
        trader = _live_trader(broker, journal=journal)
        result = trader.execute_structure(_make_long_call_intent())

        kinds = journal.kinds(result.intent_id)
        assert {KIND_DECIDED, KIND_SUBMITTED, KIND_FILLED} <= kinds
        assert journal.order_id_for(result.intent_id, 0) == "ORD-J"

    def test_dry_run_still_records_the_decision(self):
        journal = MemoryJournal()
        trader = LiveOptionTrader(broker=_make_mock_broker(), dry_run=True, journal=journal)
        intent = _make_long_call_intent()
        result = trader.execute_structure(intent)

        assert result.status == StructureExecStatus.FILLED
        assert journal.has(result.intent_id, KIND_DECIDED)


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
        trader = _research_trader(_make_mock_broker(), dry_run=True)

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

        trader = _live_trader(broker)
        result = trader.execute_structure(_make_long_call_intent())

        assert "RMS rejection" in result.legs[0].error
        assert result.error or result.legs[0].error

    def test_timeout_is_uncertain_and_halts(self):
        broker = _make_mock_broker()
        broker.place_order.return_value = BrokerOrderId("ORD999")
        # Order never fills within the poll window
        broker.get_order_book.return_value = [_make_pending_order("ORD999")]

        journal = MemoryJournal()
        trader = _live_trader(broker, journal=journal, poll_timeout_seconds=0.2)
        result = trader.execute_structure(_make_long_call_intent())

        assert result.status == StructureExecStatus.HALTED
        assert result.legs[0].uncertain
        assert "not filled within timeout" in result.legs[0].error
        assert journal.has(result.intent_id, KIND_UNCERTAIN, 0)
