"""Tests for the options trade log and equity curve (task A2).

The equity backtester reconstructs trades from runs of position sign, which
cannot describe a book holding several multi-leg structures at once.  So the
options trade log is built from the structures themselves — these tests pin
that contract, plus the ``get_closed_structures`` query the log depends on
(without it, closed structures were simply unreachable).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from backtest.engine.option_backtest_driver import (
    build_intent_from_view,
    build_trade_log,
    capture_equity,
    structure_to_record,
)
from backtest.options.expiry import ExpiryManager, StaticSettlementProvider
from backtest.options.paper_trading import OptionPaperBroker
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView


SPOT = 24800.0
UNDERLYING = "NIFTY"
CAPITAL = 1_000_000


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Book:
    """A broker plus its chain/provider, with the seam wired in."""

    def __init__(self, option_type: str = "CE", capital: int = CAPITAL):
        self.generator = SyntheticChainGenerator()
        self.generator.set_spot(UNDERLYING, SPOT)
        self.spot = SPOT
        self.chain = self.generator.generate_chain(
            underlying=UNDERLYING, option_type=option_type
        )
        self.provider = SyntheticQuoteProvider(self.generator)
        self.provider.register_chain(self.chain)
        self.broker = OptionPaperBroker(capital=capital)
        self.expiry = next(iter(self.chain.values())).expiry

    def view(self, direction: Direction = Direction.BULLISH, confidence: float = 0.4):
        return MarketView(
            direction=direction,
            confidence=confidence,
            underlying=UNDERLYING,
            spot_price=Decimal(str(self.spot)),
            bar_timestamp=datetime.combine(date.today(), time(9, 15)),
        )

    def open(self, direction: Direction = Direction.BULLISH, confidence: float = 0.4,
             ts: datetime | None = None):
        intent = build_intent_from_view(
            self.view(direction, confidence), self.chain, self.expiry,
            strategy_name="directional_options",
        )
        assert intent is not None
        return self.broker.execute_structure(
            intent, self.provider, ts or datetime.combine(date.today(), time(9, 30))
        )

    def move_spot(self, spot: float):
        self.spot = spot
        self.generator.set_spot(UNDERLYING, spot)


# ---------------------------------------------------------------------------
# get_closed_structures — the missing query
# ---------------------------------------------------------------------------

class TestClosedStructureQuery:

    def test_empty_before_anything_opens(self):
        book = Book()
        assert book.broker.get_closed_structures() == []

    def test_an_open_structure_is_not_closed(self):
        book = Book()
        book.open()
        assert len(book.broker.get_open_structures()) == 1
        assert book.broker.get_closed_structures() == []

    def test_a_closed_structure_moves_across(self):
        book = Book()
        positions = book.open()
        book.broker.close_structure(positions[0].structure_id, book.provider)

        assert book.broker.get_open_structures() == []
        closed = book.broker.get_closed_structures()
        assert len(closed) == 1
        assert closed[0].structure_id == positions[0].structure_id

    def test_settled_structures_count_as_closed(self):
        """Expiry settlement must surface in the log too, not vanish."""
        book = Book()
        positions = book.open()
        sid = positions[0].structure_id

        manager = ExpiryManager(book.broker)
        manager.settle_expired(
            StaticSettlementProvider({UNDERLYING: 26000.0}),
            as_of=datetime.combine(book.expiry + timedelta(days=1), time(15, 30)),
        )

        closed = book.broker.get_closed_structures()
        assert [s.structure_id for s in closed] == [sid]
        assert closed[0].exit_reason == "expiry_settlement"

    def test_open_and_closed_partition_the_book(self):
        book = Book()
        first = book.open()
        book.open()
        book.broker.close_structure(first[0].structure_id, book.provider)

        assert (
            len(book.broker.get_open_structures())
            + len(book.broker.get_closed_structures())
            == 2
        )


# ---------------------------------------------------------------------------
# StructureTradeRecord
# ---------------------------------------------------------------------------

class TestStructureTradeRecord:

    def test_open_structure_record(self):
        book = Book()
        positions = book.open()
        record = structure_to_record(book.broker.get_structure(positions[0].structure_id))

        assert record.structure_type == "bull_call_spread"
        assert record.strategy_name == "directional_options"
        assert record.underlying == UNDERLYING
        assert record.expiry == book.expiry
        assert record.is_open is True
        assert record.exit_reason is None
        assert record.closed_at is None
        assert record.leg_count == 2

    def test_legs_carry_their_own_detail(self):
        book = Book()
        positions = book.open()
        record = structure_to_record(book.broker.get_structure(positions[0].structure_id))

        assert {leg["side"] for leg in record.legs} == {"BUY", "SELL"}
        for leg in record.legs:
            assert leg["trading_symbol"]
            assert leg["option_type"] == "CE"
            assert Decimal(leg["strike"]) in book.chain
            # Open legs have no exit price yet.
            assert leg["exit_price"] is None
            assert leg["status"] == "open"

    def test_debit_structure_has_positive_net_entry_cost(self):
        book = Book()
        positions = book.open()
        record = structure_to_record(book.broker.get_structure(positions[0].structure_id))
        assert record.net_entry_cost > 0

    def test_closed_record_records_reason_and_pnl(self):
        book = Book()
        positions = book.open()
        book.broker.close_structure(
            positions[0].structure_id, book.provider, reason="strategy_signal"
        )
        record = structure_to_record(book.broker.get_structure(positions[0].structure_id))

        assert record.is_open is False
        assert record.exit_reason == "strategy_signal"
        assert record.closed_at is not None
        for leg in record.legs:
            assert leg["exit_price"] is not None

    def test_is_win_requires_a_closed_profitable_structure(self):
        book = Book()
        positions = book.open()
        sid = positions[0].structure_id

        assert structure_to_record(book.broker.get_structure(sid)).is_win is False

        book.move_spot(25_400.0)  # a bull call spread gains on a rally
        book.broker.update_mtm(book.provider)
        book.broker.close_structure(sid, book.provider)

        record = structure_to_record(book.broker.get_structure(sid))
        assert record.realized_pnl > 0
        assert record.is_win is True

    def test_to_dict_is_json_serialisable(self):
        """The report is JSON; a raw Decimal or date would break json.dumps."""
        book = Book()
        positions = book.open()
        book.broker.close_structure(positions[0].structure_id, book.provider)
        record = structure_to_record(book.broker.get_structure(positions[0].structure_id))

        payload = json.dumps(record.to_dict())  # must not raise
        assert "bull_call_spread" in payload

    def test_money_is_serialised_as_a_string(self):
        """Money stays exact — never round-tripped through a float."""
        book = Book()
        positions = book.open()
        record = structure_to_record(book.broker.get_structure(positions[0].structure_id))

        payload = record.to_dict()
        assert isinstance(payload["net_entry_cost"], str)
        assert isinstance(payload["realized_pnl"], str)
        assert isinstance(payload["commission"], str)
        # …and it round-trips to the exact value.
        assert Decimal(payload["net_entry_cost"]) == record.net_entry_cost


# ---------------------------------------------------------------------------
# build_trade_log
# ---------------------------------------------------------------------------

class TestBuildTradeLog:

    def test_empty_book(self):
        assert build_trade_log(OptionPaperBroker()) == []

    def test_excludes_open_when_asked(self):
        book = Book()
        first = book.open()
        book.open()
        book.broker.close_structure(first[0].structure_id, book.provider)

        assert len(build_trade_log(book.broker, include_open=False)) == 1
        assert len(build_trade_log(book.broker)) == 2

    def test_ordered_oldest_first(self):
        book = Book()
        t0 = datetime.combine(date.today(), time(9, 30))
        book.open(ts=t0)
        book.open(ts=t0 + timedelta(minutes=1))

        log = build_trade_log(book.broker)
        assert [r.opened_at for r in log] == sorted(r.opened_at for r in log)

    def test_ordering_is_deterministic(self):
        """Same book, same order — no identifier involvement (PRD §9)."""

        def build():
            book = Book()
            t0 = datetime.combine(date.today(), time(9, 30))
            for i in range(3):
                book.open(ts=t0 + timedelta(minutes=i))
            return [r.structure_type for r in build_trade_log(book.broker)]

        assert build() == build()


# ---------------------------------------------------------------------------
# Equity capture
# ---------------------------------------------------------------------------

class TestEquityCapture:

    def test_fresh_book_equity_equals_capital(self):
        broker = OptionPaperBroker(capital=CAPITAL)
        point = capture_equity(broker, datetime.combine(date.today(), time(9, 15)))

        assert point.equity == Decimal(str(CAPITAL))
        assert point.cash == Decimal(str(CAPITAL))
        assert point.costs_paid == Decimal("0")
        assert point.margin_used == Decimal("0")

    def test_snapshot_matches_the_broker(self):
        book = Book()
        book.open()
        ts = datetime.combine(date.today(), time(10, 0))
        point = capture_equity(book.broker, ts)

        assert point.equity == book.broker.total_equity
        assert point.cash == book.broker.available_cash
        assert point.costs_paid == book.broker.total_costs_paid
        assert point.timestamp == ts

    def test_equity_responds_to_a_spot_move(self):
        book = Book()
        book.open()
        ts = datetime.combine(date.today(), time(10, 0))
        before = capture_equity(book.broker, ts).equity

        book.move_spot(25_400.0)  # bull call spread gains
        book.broker.update_mtm(book.provider)
        after = capture_equity(book.broker, ts).equity

        assert after > before

    def test_equity_point_is_json_serialisable(self):
        book = Book()
        book.open()
        point = capture_equity(book.broker, datetime.combine(date.today(), time(10, 0)))
        assert json.dumps(point.to_dict())

    def test_costs_are_accounted(self):
        """Opening a structure costs money — and the snapshot must show it."""
        book = Book()
        book.open()
        point = capture_equity(book.broker, datetime.combine(date.today(), time(10, 0)))
        assert point.costs_paid > 0  # commission on two legs
        assert point.cash < Decimal(str(CAPITAL))
