"""Tests for the Order model and its state machine (Step 5).

Covers all five order types, the lifecycle transitions, fill accumulation,
trigger/trailing logic, callbacks, serialisation and persistence.

The state-machine tests matter most: an order that can move backwards out of
a terminal state corrupts the audit trail, and a trailing stop that loosens
silently widens the strategy's risk.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator import (
    InvalidTransitionError,
    Order,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidationError,
    Portfolio,
    TimeInForce,
    ValidationError,
)
from backtest.simulator.enums import (
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    WORKING_STATUSES,
)

D = Decimal
UTC = timezone.utc
T0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)


@pytest.fixture()
def db():
    manager = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


def working(**kw) -> Order:
    """A submitted market buy, ready to fill."""
    order = Order.market("INFY", "buy", kw.pop("quantity", 10), **kw)
    order.submit()
    return order


class _Fill:
    """Minimal stand-in satisfying the FillLike protocol (Step 6 replaces it)."""

    def __init__(self, quantity, fill_price, commission=D("0")):
        self.quantity = D(str(quantity))
        self.fill_price = D(str(fill_price))
        self.commission = D(str(commission))


# ===========================================================================
# Enums
# ===========================================================================


class TestEnums:
    def test_side_sign_and_opposite(self):
        assert OrderSide.BUY.sign == 1 and OrderSide.SELL.sign == -1
        assert OrderSide.BUY.opposite is OrderSide.SELL

    def test_parse_is_case_insensitive(self):
        assert OrderSide.parse("BUY") is OrderSide.BUY
        assert OrderType.parse(" Limit ") is OrderType.LIMIT
        assert OrderStatus.parse(OrderStatus.FILLED) is OrderStatus.FILLED

    def test_parse_error_lists_valid_values(self):
        with pytest.raises(ValueError, match="expected one of"):
            OrderSide.parse("hodl")

    def test_terminal_and_working_sets_are_disjoint(self):
        assert not (TERMINAL_STATUSES & WORKING_STATUSES)
        assert TERMINAL_STATUSES | WORKING_STATUSES == set(OrderStatus)

    def test_terminal_statuses_have_no_transitions(self):
        for status in TERMINAL_STATUSES:
            assert VALID_TRANSITIONS[status] == frozenset()

    def test_type_price_requirements(self):
        assert OrderType.LIMIT.needs_limit_price
        assert OrderType.STOP_LIMIT.needs_limit_price
        assert not OrderType.MARKET.needs_limit_price
        assert OrderType.STOP.needs_stop_price
        # A trailing stop derives its stop, so it does not require one upfront.
        assert not OrderType.TRAILING_STOP.needs_stop_price
        assert OrderType.TRAILING_STOP.is_stop_family

    def test_tif_immediacy(self):
        assert TimeInForce.IOC.is_immediate and TimeInForce.FOK.is_immediate
        assert not TimeInForce.DAY.is_immediate

    def test_enums_match_the_orm(self):
        """simulator/ defines these independently; they must not drift."""
        from backtest.db import models as orm

        assert set(OrderSide.values()) == set(orm.OrderSide.values())
        assert set(OrderType.values()) == set(orm.OrderType.values())
        assert set(OrderStatus.values()) == set(orm.OrderStatus.values())
        assert set(TimeInForce.values()) == set(orm.TimeInForce.values())

    def test_enums_match_the_sql_check_constraints(self):
        import pathlib

        sql = (
            pathlib.Path(__file__).resolve().parents[1]
            / "db" / "migrations" / "001_initial_schema.sql"
        ).read_text()
        for enum_cls in (OrderSide, OrderType, OrderStatus, TimeInForce):
            for value in enum_cls.values():
                assert f"'{value}'" in sql, f"{enum_cls.__name__}.{value} missing from SQL"


# ===========================================================================
# Construction and validation
# ===========================================================================


class TestConstruction:
    def test_market_order_defaults(self):
        o = Order.market("infy", "buy", 10)
        assert o.symbol == "INFY"
        assert o.order_type is OrderType.MARKET
        assert o.status is OrderStatus.PENDING
        assert o.time_in_force is TimeInForce.DAY
        assert not o.is_submitted
        assert o.remaining_quantity == D("10.00000000")

    def test_string_enums_are_coerced(self):
        o = Order(symbol="A", side="sell", order_type="limit", quantity=5,
                  limit_price=100, time_in_force="gtc")
        assert o.side is OrderSide.SELL
        assert o.time_in_force is TimeInForce.GTC

    @pytest.mark.parametrize("quantity", [0, -5])
    def test_non_positive_quantity_rejected(self, quantity):
        with pytest.raises(OrderValidationError, match="quantity must be positive"):
            Order.market("A", "buy", quantity)

    def test_negative_quantity_message_points_at_side(self):
        """Direction belongs in `side`, never in the sign."""
        with pytest.raises(OrderValidationError, match="side"):
            Order.market("A", "buy", -10)

    def test_empty_symbol_rejected(self):
        with pytest.raises(OrderValidationError, match="symbol"):
            Order.market("   ", "buy", 10)

    def test_bad_enum_rejected(self):
        with pytest.raises(OrderValidationError, match="invalid"):
            Order(symbol="A", side="maybe", quantity=1)

    def test_overfilled_construction_rejected(self):
        with pytest.raises(OrderValidationError, match="cannot exceed"):
            Order(symbol="A", side="buy", quantity=10, filled_quantity=11)

    def test_history_starts_with_creation(self):
        o = Order.market("A", "buy", 1)
        assert len(o.status_history) == 1
        assert o.status_history[0].note == "created"

    def test_convenience_constructors(self):
        assert Order.limit("A", "buy", 1, 100).order_type is OrderType.LIMIT
        assert Order.stop("A", "sell", 1, 90).order_type is OrderType.STOP
        assert Order.stop_limit("A", "buy", 1, 100, 101).order_type is OrderType.STOP_LIMIT
        assert Order.trailing_stop("A", "sell", 1, 5).order_type is OrderType.TRAILING_STOP


class TestValidation:
    def test_limit_without_price_rejected(self):
        o = Order(symbol="A", side="buy", order_type="limit", quantity=1)
        with pytest.raises(OrderValidationError, match="require a limit_price"):
            o.validate()

    def test_stop_without_price_rejected(self):
        o = Order(symbol="A", side="buy", order_type="stop", quantity=1)
        with pytest.raises(OrderValidationError, match="require a stop_price"):
            o.validate()

    def test_trailing_without_amount_rejected(self):
        o = Order(symbol="A", side="sell", order_type="trailing_stop", quantity=1)
        with pytest.raises(OrderValidationError, match="trailing_amount"):
            o.validate()

    def test_market_with_limit_price_rejected(self):
        """Silently ignoring the price at execution time would hide a bug."""
        o = Order(symbol="A", side="buy", order_type="market", quantity=1, limit_price=100)
        with pytest.raises(OrderValidationError, match="must not carry"):
            o.validate()

    def test_trailing_amount_on_non_trailing_rejected(self):
        o = Order(symbol="A", side="buy", order_type="limit", quantity=1,
                  limit_price=100, trailing_amount=5)
        with pytest.raises(OrderValidationError, match="only valid on trailing_stop"):
            o.validate()

    @pytest.mark.parametrize("field", ["limit_price", "stop_price"])
    def test_non_positive_prices_rejected(self, field):
        o = Order(symbol="A", side="buy", order_type="limit", quantity=1, limit_price=100)
        setattr(o, field, D("0"))
        with pytest.raises(OrderValidationError, match="must be positive"):
            o.validate()

    def test_unfillable_buy_stop_limit_rejected(self):
        """Buy triggers upward, so a limit below the stop can never fill."""
        o = Order.stop_limit("A", "buy", 1, stop_price=100, limit_price=95)
        with pytest.raises(OrderValidationError, match="can never fill"):
            o.validate()

    def test_unfillable_sell_stop_limit_rejected(self):
        o = Order.stop_limit("A", "sell", 1, stop_price=100, limit_price=105)
        with pytest.raises(OrderValidationError, match="can never fill"):
            o.validate()

    def test_valid_stop_limits_pass(self):
        Order.stop_limit("A", "buy", 1, stop_price=100, limit_price=105).validate()
        Order.stop_limit("A", "sell", 1, stop_price=100, limit_price=95).validate()


# ===========================================================================
# State machine
# ===========================================================================


class TestStateMachine:
    def test_submit_stamps_and_fires(self):
        o = Order.market("A", "buy", 1)
        seen = []
        o.add_callback(OrderEvent.SUBMIT, lambda order: seen.append(order.order_id))
        o.submit()
        assert o.is_submitted and o.is_working
        assert seen == [o.order_id]

    def test_double_submit_rejected(self):
        o = working()
        with pytest.raises(InvalidTransitionError, match="already been submitted"):
            o.submit()

    def test_submit_failure_rejects_and_raises(self):
        """The reason is recorded for the audit trail AND the caller must cope."""
        o = Order(symbol="A", side="buy", order_type="limit", quantity=1)
        with pytest.raises(OrderValidationError):
            o.submit()
        assert o.status is OrderStatus.REJECTED
        assert "limit_price" in o.reason_for_rejection

    def test_cancel_working_order(self):
        o = working()
        o.cancel("changed my mind")
        assert o.status is OrderStatus.CANCELLED
        assert o.cancelled_at is not None
        assert o.is_terminal

    def test_cancel_after_partial_keeps_the_fill(self):
        o = working(quantity=10)
        o.add_fill(quantity=4, fill_price=100)
        o.cancel("end of day")
        assert o.status is OrderStatus.CANCELLED
        assert o.filled_quantity == D("4.00000000")

    @pytest.mark.parametrize("terminal", ["cancel", "reject", "fill"])
    def test_terminal_orders_cannot_transition(self, terminal):
        o = working(quantity=1)
        if terminal == "cancel":
            o.cancel("x")
        elif terminal == "reject":
            o.reject("x")
        else:
            o.add_fill(quantity=1, fill_price=100)
        assert o.is_terminal
        with pytest.raises(InvalidTransitionError):
            o.update_status(OrderStatus.PENDING)

    def test_filled_cannot_be_cancelled(self):
        o = working(quantity=1)
        o.add_fill(quantity=1, fill_price=100)
        with pytest.raises(InvalidTransitionError, match="cannot move order"):
            o.cancel("too late")

    def test_reject_requires_a_reason(self):
        o = Order.market("A", "buy", 1)
        with pytest.raises(OrderValidationError, match="reason is required"):
            o.reject("")

    def test_transition_error_lists_allowed_moves(self):
        o = working(quantity=1)
        o.add_fill(quantity=1, fill_price=100)
        with pytest.raises(InvalidTransitionError) as excinfo:
            o.update_status(OrderStatus.PARTIAL)
        assert "terminal" in str(excinfo.value)

    def test_history_records_every_change(self):
        o = Order.market("A", "buy", 10)
        o.submit()
        o.add_fill(quantity=4, fill_price=100)
        o.cancel("done")
        notes = [c.note for c in o.status_history]
        assert notes[0] == "created" and "submitted" in notes
        assert any("filled" in n for n in notes)
        assert o.status_history[-1].status is OrderStatus.CANCELLED

    def test_cannot_submit_a_rejected_order(self):
        o = Order.market("A", "buy", 1)
        o.reject("no")
        with pytest.raises(InvalidTransitionError, match="cannot submit"):
            o.submit()


# ===========================================================================
# Fills
# ===========================================================================


class TestFills:
    def test_partial_then_complete(self):
        o = working(quantity=10)
        assert o.add_fill(quantity=4, fill_price=100) is OrderStatus.PARTIAL
        assert o.remaining_quantity == D("6.00000000")
        assert o.add_fill(quantity=6, fill_price=110) is OrderStatus.FILLED
        assert o.remaining_quantity == D("0")
        assert o.filled_at is not None

    def test_average_price_is_quantity_weighted(self):
        o = working(quantity=10)
        o.add_fill(quantity=4, fill_price=100)
        o.add_fill(quantity=6, fill_price=110)
        assert o.average_fill_price == D("106.00000000")   # (400+660)/10

    def test_three_way_average(self):
        o = working(quantity=9)
        for qty, px in ((3, 100), (3, 200), (3, 300)):
            o.add_fill(quantity=qty, fill_price=px)
        assert o.average_fill_price == D("200.00000000")

    def test_accepts_a_fill_object(self):
        o = working(quantity=10)
        o.add_fill(_Fill(10, 100, commission=5))
        assert o.status is OrderStatus.FILLED
        assert o.total_commission == D("5.0000")
        assert len(o.fills) == 1

    def test_fill_object_must_expose_required_fields(self):
        o = working()
        with pytest.raises(OrderValidationError, match="quantity.*fill_price"):
            o.add_fill(object())

    def test_overfill_rejected(self):
        o = working(quantity=10)
        with pytest.raises(OrderValidationError, match="exceed the order quantity"):
            o.add_fill(quantity=11, fill_price=100)

    def test_overfill_after_partial_rejected(self):
        o = working(quantity=10)
        o.add_fill(quantity=7, fill_price=100)
        with pytest.raises(OrderValidationError, match="exceed"):
            o.add_fill(quantity=4, fill_price=100)

    @pytest.mark.parametrize("qty, px", [(0, 100), (-1, 100), (1, 0), (1, -5)])
    def test_invalid_fill_values_rejected(self, qty, px):
        o = working()
        with pytest.raises(OrderValidationError):
            o.add_fill(quantity=qty, fill_price=px)

    def test_cannot_fill_unsubmitted_order(self):
        o = Order.market("A", "buy", 10)
        with pytest.raises(InvalidTransitionError, match="never submitted"):
            o.add_fill(quantity=1, fill_price=100)

    def test_cannot_fill_cancelled_order(self):
        o = working()
        o.cancel("x")
        with pytest.raises(InvalidTransitionError, match="cannot fill"):
            o.add_fill(quantity=1, fill_price=100)

    def test_callbacks_distinguish_partial_from_full(self):
        o = working(quantity=10)
        events = []
        o.add_callback(OrderEvent.PARTIAL_FILL, lambda *_: events.append("partial"))
        o.add_callback(OrderEvent.FILL, lambda *_: events.append("full"))
        o.add_fill(quantity=4, fill_price=100)
        o.add_fill(quantity=6, fill_price=100)
        assert events == ["partial", "full"]

    def test_dust_remainder_completes_the_order(self):
        o = working(quantity=D("10"))
        o.add_fill(quantity=D("9.999999995"), fill_price=100)
        assert o.status is OrderStatus.FILLED


# ===========================================================================
# Fillability and pricing
# ===========================================================================


class TestMarketOrders:
    def test_always_fillable(self):
        assert working().is_fillable({"bid": 99, "ask": 101})

    def test_buy_lifts_the_ask(self):
        assert working().calculate_fill_price({"bid": 99, "ask": 101}) == D("101.00000000")

    def test_sell_hits_the_bid(self):
        o = Order.market("A", "sell", 10); o.submit()
        assert o.calculate_fill_price({"bid": 99, "ask": 101}) == D("99.00000000")

    def test_bare_price_is_accepted(self):
        assert working().calculate_fill_price(100) == D("100.00000000")

    def test_close_is_used_when_no_quote(self):
        assert working().calculate_fill_price({"close": 250}) == D("250.00000000")

    def test_empty_market_data_rejected(self):
        with pytest.raises(ValidationError, match="market data"):
            working().is_fillable({})

    def test_none_market_data_rejected(self):
        with pytest.raises(ValidationError, match="market data is required"):
            working().is_fillable(None)

    def test_unsubmitted_is_not_fillable(self):
        assert not Order.market("A", "buy", 1).is_fillable(100)

    def test_terminal_is_not_fillable(self):
        o = working(); o.cancel("x")
        assert not o.is_fillable(100)


class TestLimitOrders:
    def test_buy_fills_at_or_below_limit(self):
        o = Order.limit("A", "buy", 10, 100); o.submit()
        assert o.is_fillable({"ask": 100})
        assert o.is_fillable({"ask": 99})
        assert not o.is_fillable({"ask": 101})

    def test_sell_fills_at_or_above_limit(self):
        o = Order.limit("A", "sell", 10, 100); o.submit()
        assert o.is_fillable({"bid": 100})
        assert o.is_fillable({"bid": 101})
        assert not o.is_fillable({"bid": 99})

    def test_buy_gets_price_improvement(self):
        """Fill at the market when it is better than the limit."""
        o = Order.limit("A", "buy", 10, 100); o.submit()
        assert o.calculate_fill_price({"ask": 95}) == D("95.00000000")

    def test_sell_gets_price_improvement(self):
        o = Order.limit("A", "sell", 10, 100); o.submit()
        assert o.calculate_fill_price({"bid": 105}) == D("105.00000000")

    def test_fill_price_capped_at_limit(self):
        o = Order.limit("A", "buy", 10, 100); o.submit()
        assert o.calculate_fill_price({"ask": 100}) == D("100.00000000")

    def test_unfillable_price_request_raises(self):
        o = Order.limit("A", "buy", 10, 100); o.submit()
        with pytest.raises(ValidationError, match="not fillable"):
            o.calculate_fill_price({"ask": 105})


class TestStopOrders:
    def test_sell_stop_triggers_on_the_way_down(self):
        o = Order.stop("A", "sell", 10, stop_price=90); o.submit()
        assert not o.check_trigger(95)
        assert o.check_trigger(90)
        assert o.triggered and o.triggered_at is not None

    def test_buy_stop_triggers_on_the_way_up(self):
        o = Order.stop("A", "buy", 10, stop_price=110); o.submit()
        assert not o.check_trigger(105)
        assert o.check_trigger(110)

    def test_trigger_is_sticky(self):
        """Un-triggering would turn a stop into a limit and change the risk."""
        o = Order.stop("A", "sell", 10, stop_price=90); o.submit()
        o.check_trigger(85)
        assert o.check_trigger(120) is True

    def test_not_fillable_before_trigger(self):
        o = Order.stop("A", "sell", 10, stop_price=90); o.submit()
        assert not o.is_fillable({"bid": 95, "last": 95})

    def test_behaves_as_market_after_trigger(self):
        o = Order.stop("A", "sell", 10, stop_price=90); o.submit()
        assert o.is_fillable({"bid": 88, "last": 88})
        assert o.calculate_fill_price({"bid": 88, "ask": 89, "last": 88}) == D("88.00000000")

    def test_trigger_fires_the_callback(self):
        o = Order.stop("A", "sell", 10, stop_price=90); o.submit()
        seen = []
        o.add_callback(OrderEvent.TRIGGER, lambda order, px: seen.append(px))
        o.check_trigger(85)
        assert seen == [D("85.00000000")]

    def test_non_stop_orders_are_always_triggered(self):
        assert working().check_trigger(1) is True


class TestStopLimitOrders:
    def test_needs_both_trigger_and_limit(self):
        o = Order.stop_limit("A", "sell", 10, stop_price=90, limit_price=88); o.submit()
        assert not o.is_fillable({"bid": 95, "last": 95})     # not triggered
        assert not o.is_fillable({"bid": 87, "last": 89})     # triggered, below limit
        assert o.is_fillable({"bid": 89, "last": 89})         # triggered and at limit

    def test_fill_price_respects_the_limit(self):
        o = Order.stop_limit("A", "sell", 10, stop_price=90, limit_price=88); o.submit()
        o.check_trigger(89)
        assert o.calculate_fill_price({"bid": 89, "last": 89}) == D("89.00000000")


class TestTrailingStops:
    def test_sell_stop_ratchets_up_only(self):
        o = Order.trailing_stop("A", "sell", 10, trailing_amount=50); o.submit()
        assert o.update_trailing(1500) == D("1450.00000000")
        assert o.update_trailing(1600) == D("1550.00000000")
        # Price retraces; the stop must hold, not loosen.
        assert o.update_trailing(1580) == D("1550.00000000")

    def test_buy_stop_ratchets_down_only(self):
        o = Order.trailing_stop("A", "buy", 10, trailing_amount=50); o.submit()
        assert o.update_trailing(1000) == D("1050.00000000")
        assert o.update_trailing(900) == D("950.00000000")
        assert o.update_trailing(950) == D("950.00000000")

    def test_high_water_mark_is_tracked(self):
        o = Order.trailing_stop("A", "sell", 10, trailing_amount=50); o.submit()
        for px in (1500, 1600, 1550):
            o.update_trailing(px)
        assert o.extreme_price == D("1600.00000000")

    def test_triggers_after_ratcheting(self):
        o = Order.trailing_stop("A", "sell", 10, trailing_amount=50); o.submit()
        for px in (1500, 1550, 1600, 1580):
            assert not o.check_trigger(px)
        assert o.check_trigger(1545)

    def test_becomes_market_after_trigger(self):
        o = Order.trailing_stop("A", "sell", 10, trailing_amount=50); o.submit()
        o.check_trigger(1600)
        o.check_trigger(1500)
        assert o.is_fillable({"bid": 1500, "last": 1500})

    def test_update_trailing_is_noop_for_other_types(self):
        assert working().update_trailing(100) is None

    def test_requires_trailing_amount(self):
        o = Order(symbol="A", side="sell", order_type="trailing_stop", quantity=1)
        with pytest.raises(OrderValidationError, match="trailing_amount"):
            o.update_trailing(100)

    def test_rejects_non_positive_price(self):
        o = Order.trailing_stop("A", "sell", 10, trailing_amount=5); o.submit()
        with pytest.raises(OrderValidationError, match="must be positive"):
            o.update_trailing(0)


# ===========================================================================
# Callbacks
# ===========================================================================


class TestCallbacks:
    def test_unknown_event_rejected(self):
        with pytest.raises(ValidationError, match="unknown order event"):
            working().add_callback("on_vibes", lambda: None)

    def test_multiple_handlers_all_run(self):
        o = Order.market("A", "buy", 1)
        seen = []
        for i in range(3):
            o.add_callback(OrderEvent.SUBMIT, lambda _o, i=i: seen.append(i))
        o.submit()
        assert seen == [0, 1, 2]

    def test_a_failing_callback_does_not_break_the_fill(self):
        """A broken alert hook must not roll back an execution that happened."""
        o = working(quantity=10)
        o.add_callback(OrderEvent.FILL, lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
        o.add_fill(quantity=10, fill_price=100)
        assert o.status is OrderStatus.FILLED

    def test_cancel_and_reject_callbacks(self):
        seen = []
        a = working(); a.add_callback(OrderEvent.CANCEL, lambda o, r: seen.append(("c", r)))
        a.cancel("why not")
        b = Order.market("A", "buy", 1)
        b.add_callback(OrderEvent.REJECT, lambda o, r: seen.append(("r", r)))
        b.reject("nope")
        assert seen == [("c", "why not"), ("r", "nope")]


# ===========================================================================
# Serialisation
# ===========================================================================


class TestSerialisation:
    def test_round_trip(self):
        o = Order.stop_limit("INFY", "sell", 10, stop_price=1400, limit_price=1390,
                             time_in_force="gtc", strategy_name="sma")
        o.submit()
        o.add_fill(quantity=4, fill_price=1395)
        restored = Order.from_dict(o.to_dict())
        assert restored.order_id == o.order_id
        assert restored.side is OrderSide.SELL
        assert restored.order_type is OrderType.STOP_LIMIT
        assert restored.status is OrderStatus.PARTIAL
        assert restored.filled_quantity == o.filled_quantity
        assert restored.average_fill_price == o.average_fill_price
        assert restored.time_in_force is TimeInForce.GTC

    def test_terminal_order_round_trips(self):
        """Restoring must not replay transitions and trip the guard."""
        o = working(quantity=1)
        o.add_fill(quantity=1, fill_price=100)
        restored = Order.from_dict(o.to_dict())
        assert restored.status is OrderStatus.FILLED
        assert restored.is_terminal

    def test_survives_json(self):
        o = Order.limit("A", "buy", D("3.14159265"), D("1234.56789012"))
        o.submit()
        restored = Order.from_dict(json.loads(json.dumps(o.to_dict())))
        assert restored.quantity == D("3.14159265")
        assert restored.limit_price == D("1234.56789012")

    def test_trailing_state_survives(self):
        o = Order.trailing_stop("A", "sell", 10, trailing_amount=50)
        o.submit()
        o.update_trailing(1600)
        restored = Order.from_dict(o.to_dict())
        assert restored.extreme_price == D("1600.00000000")
        assert restored.stop_price == D("1550.00000000")

    def test_status_history_survives(self):
        o = working(quantity=10)
        o.add_fill(quantity=4, fill_price=100)
        restored = Order.from_dict(o.to_dict())
        assert len(restored.status_history) == len(o.status_history)

    def test_rejection_reason_survives(self):
        o = Order.market("A", "buy", 1)
        o.reject("insufficient funds")
        assert Order.from_dict(o.to_dict()).reason_for_rejection == "insufficient funds"


# ===========================================================================
# Persistence
# ===========================================================================


class TestPersistence:
    def test_requires_a_portfolio(self, db):
        with pytest.raises(ValidationError, match="portfolio_id is required"):
            working().save_to_db(db)

    def test_save_and_read_back(self, db):
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        o = Order.limit("INFY", "buy", 10, 1500, portfolio_id=parent.portfolio_id)
        o.submit()
        o.save_to_db(db)

        row = db.fetch_one("SELECT * FROM orders WHERE symbol='INFY'")
        assert row["side"] == "buy" and row["order_type"] == "limit"
        assert D(str(row["quantity"])) == D("10.00000000")
        assert row["status"] == "pending"

    def test_filled_order_satisfies_the_db_check(self, db):
        """ck_orders_filled_consistency needs filled_at AND full quantity."""
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        o = Order.market("INFY", "buy", 10, portfolio_id=parent.portfolio_id)
        o.submit()
        o.add_fill(quantity=10, fill_price=1500)
        o.save_to_db(db)
        row = db.fetch_one("SELECT status, filled_at, filled_quantity FROM orders")
        assert row["status"] == "filled" and row["filled_at"] is not None

    def test_rejected_order_satisfies_the_db_check(self, db):
        """ck_orders_rejection_reason requires a non-null reason."""
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        o = Order.market("INFY", "buy", 10, portfolio_id=parent.portfolio_id)
        o.reject("no buying power")
        o.save_to_db(db)
        assert db.fetch_one("SELECT rejection_reason FROM orders")["rejection_reason"]

    def test_save_is_idempotent(self, db):
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        o = Order.market("INFY", "buy", 10, portfolio_id=parent.portfolio_id)
        o.submit(); o.save_to_db(db)
        o.add_fill(quantity=10, fill_price=1500); o.save_to_db(db)
        assert db.fetch_scalar("SELECT count(*) FROM orders") == 1
        assert db.fetch_one("SELECT status FROM orders")["status"] == "filled"

    def test_unsubmitted_order_uses_created_at(self, db):
        """submitted_at is NOT NULL in the schema."""
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        o = Order.market("INFY", "buy", 10, portfolio_id=parent.portfolio_id)
        o.save_to_db(db)
        assert db.fetch_one("SELECT submitted_at FROM orders")["submitted_at"] is not None

    def test_client_order_id_uniqueness_is_enforced(self, db):
        """The DB idempotency key must reject a duplicate submission."""
        from sqlalchemy.exc import IntegrityError

        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        for _ in range(1):
            a = Order.market("INFY", "buy", 1, portfolio_id=parent.portfolio_id,
                             client_order_id="dup-1")
            a.submit(); a.save_to_db(db)
        b = Order.market("INFY", "buy", 1, portfolio_id=parent.portfolio_id,
                         client_order_id="dup-1")
        b.submit()
        with pytest.raises(IntegrityError):
            b.save_to_db(db)


# ===========================================================================
# Portfolio integration
# ===========================================================================


class TestPortfolioOrders:
    def test_add_and_find(self):
        p = Portfolio(name="p", initial_capital=100_000)
        o = p.add_order(Order.market("INFY", "buy", 10))
        assert o.portfolio_id == p.portfolio_id
        assert p.pending_orders == [o]
        assert p.get_order(o.order_id) is o

    def test_terminal_orders_go_straight_to_filled(self):
        p = Portfolio(name="p", initial_capital=100_000)
        o = Order.market("INFY", "buy", 10)
        o.reject("nope")
        p.add_order(o)
        assert p.pending_orders == [] and p.filled_orders == [o]

    def test_sync_moves_completed_orders(self):
        p = Portfolio(name="p", initial_capital=100_000)
        a = p.add_order(working(quantity=10))
        p.add_order(working(quantity=10))
        a.add_fill(quantity=10, fill_price=100)
        assert p.sync_orders() == 1
        assert len(p.pending_orders) == 1 and p.filled_orders == [a]

    def test_orders_for_symbol(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.add_order(Order.market("INFY", "buy", 1))
        p.add_order(Order.market("TCS", "buy", 1))
        assert [o.symbol for o in p.orders_for("infy")] == ["INFY"]

    def test_cancel_all(self):
        p = Portfolio(name="p", initial_capital=100_000)
        for _ in range(3):
            p.add_order(working())
        assert p.cancel_all_orders() == 3
        assert p.pending_orders == []
        assert all(o.status is OrderStatus.CANCELLED for o in p.filled_orders)

    def test_cancel_all_survives_one_bad_order(self):
        """One un-cancellable order must not block the rest."""
        p = Portfolio(name="p", initial_capital=100_000)
        good = p.add_order(working())
        bad = working(quantity=1)
        bad.add_fill(quantity=1, fill_price=100)   # now FILLED, cannot cancel
        p.pending_orders.append(bad)
        assert p.cancel_all_orders() == 1
        assert good.status is OrderStatus.CANCELLED

    def test_summary_counts_pending_orders(self):
        p = Portfolio(name="p", initial_capital=100_000)
        p.add_order(working())
        assert p.summary()["pending_orders"] == 1


def test_restored_trailing_stop_keeps_ratcheting():
    """Regression: extreme_price was left as a str, so min()/max() would crash."""
    o = Order.trailing_stop("A", "sell", 10, trailing_amount=50)
    o.submit()
    o.update_trailing(1600)
    restored = Order.from_dict(json.loads(json.dumps(o.to_dict())))
    assert isinstance(restored.extreme_price, Decimal)
    # Must keep working after the round trip rather than raising TypeError.
    assert restored.update_trailing(1700) == D("1650.00000000")
    assert restored.update_trailing(1650) == D("1650.00000000")


def test_all_decimal_fields_are_decimal_after_restore():
    o = Order.stop_limit("A", "sell", 10, stop_price=1400, limit_price=1390)
    o.submit()
    o.add_fill(quantity=5, fill_price=1395)
    restored = Order.from_dict(json.loads(json.dumps(o.to_dict())))
    for field in ("quantity", "filled_quantity", "limit_price", "stop_price",
                  "average_fill_price"):
        assert isinstance(getattr(restored, field), Decimal), field