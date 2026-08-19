"""Tests for tax-lot accounting and corporate actions (Step 4).

Step 3's tests in ``test_simulator_portfolio.py`` cover the Position basics
(valuation, P&L, partial closes). This module covers what Step 4 added:
FIFO/LIFO/average cost basis, splits, dividends, and single-position
persistence.

The cost-basis tests matter because the same trades produce different
realised P&L under each method, and Indian equity delivery mandates FIFO.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator import (
    CostBasisMethod,
    Lot,
    LotBook,
    Portfolio,
    Position,
    ValidationError,
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


def make(method: str = CostBasisMethod.AVERAGE, qty=10, px=100, **kw) -> Position:
    return Position(
        symbol="INFY", quantity=qty, average_entry_price=px, cost_basis_method=method, **kw
    )


# ===========================================================================
# Lot
# ===========================================================================


class TestLot:
    def test_construction(self):
        lot = Lot(quantity=10, price=100, acquired_at=T0)
        assert lot.quantity == D("10.00000000")
        assert lot.cost == D("1000.0000000000000000")
        assert lot.lot_id

    @pytest.mark.parametrize("kwargs", [dict(quantity=0), dict(quantity=-5), dict(price=0), dict(price=-1)])
    def test_rejects_non_positive(self, kwargs):
        with pytest.raises(ValidationError):
            Lot(**{"quantity": 10, "price": 100, **kwargs})

    def test_round_trip(self):
        lot = Lot(quantity=10, price=100, acquired_at=T0)
        restored = Lot.from_dict(lot.to_dict())
        assert restored.lot_id == lot.lot_id
        assert restored.quantity == lot.quantity
        assert restored.acquired_at == T0


# ===========================================================================
# LotBook
# ===========================================================================


class TestLotBook:
    def test_rejects_unknown_method(self):
        with pytest.raises(ValidationError, match="unknown cost basis method"):
            LotBook(method="wishful")

    def test_method_is_normalised(self):
        assert LotBook(method="FIFO").method == "fifo"

    def test_totals(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        book.add(10, 120)
        assert book.total_quantity == D("20.00000000")
        assert book.weighted_average_price == D("110.00000000")
        assert len(book) == 2

    def test_average_collapses_to_one_lot(self):
        book = LotBook(CostBasisMethod.AVERAGE)
        book.add(10, 100)
        book.add(10, 120)
        assert len(book) == 1
        assert book.weighted_average_price == D("110.00000000")

    def test_average_collapse_keeps_oldest_timestamp(self):
        book = LotBook(CostBasisMethod.AVERAGE)
        book.add(10, 100, T0)
        book.add(10, 120, T0 + timedelta(days=5))
        assert book.lots[0].acquired_at == T0

    def test_fifo_consumes_oldest(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100, T0)
        book.add(10, 120, T0 + timedelta(days=1))
        consumed = book.consume(10)
        assert len(consumed) == 1
        assert consumed[0].entry_price == D("100.00000000")
        assert book.weighted_average_price == D("120.00000000")

    def test_lifo_consumes_newest(self):
        book = LotBook(CostBasisMethod.LIFO)
        book.add(10, 100, T0)
        book.add(10, 120, T0 + timedelta(days=1))
        consumed = book.consume(10)
        assert consumed[0].entry_price == D("120.00000000")
        assert book.weighted_average_price == D("100.00000000")

    def test_consume_spanning_multiple_lots(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        book.add(10, 120)
        consumed = book.consume(15)
        assert [c.quantity for c in consumed] == [D("10.00000000"), D("5.00000000")]
        assert book.total_quantity == D("5.00000000")

    def test_partially_consumed_lot_is_kept(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        book.consume(4)
        assert len(book) == 1
        assert book.lots[0].quantity == D("6.00000000")

    def test_fully_consumed_lot_is_dropped(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        book.add(5, 120)
        book.consume(10)
        assert len(book) == 1
        assert book.lots[0].price == D("120.00000000")

    def test_over_consume_is_refused(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        with pytest.raises(ValidationError, match="more than the open quantity"):
            book.consume(11)

    def test_consume_everything_empties_the_book(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        book.consume(10)
        assert len(book) == 0
        assert not book
        assert book.total_quantity == D("0")
        assert book.weighted_average_price == D("0")

    def test_consume_zero_is_refused(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        with pytest.raises(ValidationError, match="non-zero"):
            book.consume(0)

    def test_collapse_empty_book_raises(self):
        with pytest.raises(ValidationError, match="empty lot book"):
            LotBook(CostBasisMethod.AVERAGE).collapse()

    def test_split_preserves_total_cost(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        book.add(5, 200)
        before = book.total_cost
        book.apply_split(2)
        assert book.total_quantity == D("30.00000000")
        assert book.total_cost == before

    def test_split_rejects_non_positive_ratio(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        with pytest.raises(ValidationError, match="split ratio must be positive"):
            book.apply_split(0)

    def test_reduce_cost_basis_floors_above_zero(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 5)
        book.reduce_cost_basis(100)  # dividend larger than the price
        assert book.lots[0].price > D("0")

    def test_reduce_cost_basis_rejects_negative(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        with pytest.raises(ValidationError, match="must not be negative"):
            book.reduce_cost_basis(-1)

    def test_round_trip(self):
        book = LotBook(CostBasisMethod.LIFO)
        book.add(10, 100, T0)
        book.add(5, 120, T0 + timedelta(days=1))
        restored = LotBook.from_dict(book.to_dict())
        assert restored.method == "lifo"
        assert len(restored) == 2
        assert restored.weighted_average_price == book.weighted_average_price

    def test_lots_view_is_immutable(self):
        book = LotBook(CostBasisMethod.FIFO)
        book.add(10, 100)
        assert isinstance(book.lots, tuple)


# ===========================================================================
# Cost basis on a Position
# ===========================================================================


class TestCostBasisMethods:
    """Buy 10@100, buy 10@120, sell 10@130 — three methods, three answers."""

    @pytest.mark.parametrize(
        "method, realised, remaining_basis",
        [
            (CostBasisMethod.FIFO, D("300.0000"), D("120.00000000")),
            (CostBasisMethod.LIFO, D("100.0000"), D("100.00000000")),
            (CostBasisMethod.AVERAGE, D("200.0000"), D("110.00000000")),
        ],
    )
    def test_documented_worked_example(self, method, realised, remaining_basis):
        p = make(method)
        p.add_shares(10, 120)
        result = p.reduce_shares(10, 130)
        assert result.realized_pnl == realised
        assert p.quantity == D("10.00000000")
        assert p.calculate_average_price() == remaining_basis

    def test_default_is_average(self):
        assert make().cost_basis_method == CostBasisMethod.AVERAGE

    def test_average_keeps_basis_after_partial_close(self):
        """Backwards-compatible with the Step 3 behaviour."""
        p = make(CostBasisMethod.AVERAGE)
        p.reduce_shares(4, 130)
        assert p.average_entry_price == D("100.00000000")

    def test_fifo_moves_basis_after_partial_close(self):
        p = make(CostBasisMethod.FIFO)
        p.add_shares(10, 120)
        p.reduce_shares(10, 130)
        assert p.average_entry_price == D("120.00000000")

    def test_consumed_lots_are_reported(self):
        p = make(CostBasisMethod.FIFO)
        p.add_shares(10, 120)
        result = p.reduce_shares(15, 130)
        assert len(result.consumed_lots) == 2
        assert result.consumed_lots[0].entry_price == D("100.00000000")
        assert result.consumed_lots[1].entry_price == D("120.00000000")
        assert result.realized_pnl == D("350.0000")   # 10*30 + 5*10

    def test_all_methods_agree_on_a_full_close(self):
        """Lot ordering cannot change the total when everything is sold."""
        totals = set()
        for method in CostBasisMethod.ALL:
            p = make(method)
            p.add_shares(10, 120)
            totals.add(p.close(130).realized_pnl)
        assert totals == {D("400.0000")}       # 10*30 + 10*10

    def test_short_fifo(self):
        """Short 10@100 then 10@80; buy back 10 @90 consumes the 100 lot."""
        p = make(CostBasisMethod.FIFO, qty=-10, px=100)
        p.add_shares(10, 80)
        result = p.reduce_shares(10, 90)
        assert result.realized_pnl == D("100.0000")    # sold at 100, bought at 90
        assert p.calculate_average_price() == D("80.00000000")

    def test_short_lifo(self):
        p = make(CostBasisMethod.LIFO, qty=-10, px=100)
        p.add_shares(10, 80)
        result = p.reduce_shares(10, 90)
        assert result.realized_pnl == D("-100.0000")   # sold at 80, bought at 90

    def test_lot_count_tracks_method(self):
        avg, fifo = make(CostBasisMethod.AVERAGE), make(CostBasisMethod.FIFO)
        for p in (avg, fifo):
            p.add_shares(10, 120)
            p.add_shares(10, 130)
        assert avg.lot_count == 1
        assert fifo.lot_count == 3

    def test_book_stays_in_sync_with_quantity(self):
        p = make(CostBasisMethod.FIFO)
        p.add_shares(7, 110)
        p.reduce_shares(3, 130)
        p.add_shares(5, 140)
        p.reduce_shares(6, 150)
        assert p.lot_book.total_quantity == abs(p.quantity)
        assert p.calculate_average_price() == p.average_entry_price

    def test_many_cycles_keep_book_consistent(self):
        p = make(CostBasisMethod.FIFO, qty=100, px=100)
        for i in range(50):
            p.add_shares(10, 100 + i)
            p.reduce_shares(10, 105 + i)
        assert p.lot_book.total_quantity == abs(p.quantity) == D("100.00000000")
        assert p.calculate_average_price() == p.average_entry_price

    def test_invalid_method_rejected_at_construction(self):
        with pytest.raises(ValidationError, match="unknown cost basis method"):
            make("guesswork")


class TestLotMetadata:
    def test_entry_date_aliases_opened_at(self):
        p = make()
        assert p.entry_date is p.opened_at

    def test_oldest_lot_age(self):
        p = Position(
            symbol="A", quantity=10, average_entry_price=100,
            cost_basis_method=CostBasisMethod.FIFO, opened_at=T0,
        )
        age = p.oldest_lot_age(now=T0 + timedelta(days=400))
        assert age >= timedelta(days=399)

    def test_holding_period_per_lot(self):
        p = Position(
            symbol="A", quantity=10, average_entry_price=100,
            cost_basis_method=CostBasisMethod.FIFO, opened_at=T0,
        )
        result = p.close(110)
        held = result.consumed_lots[0].holding_period(T0 + timedelta(days=30))
        assert held == timedelta(days=30)


# ===========================================================================
# Splits
# ===========================================================================


class TestSplits:
    def test_forward_split_scales_quantity_and_price(self):
        p = make(qty=10, px=100)
        result = p.apply_split(2)
        assert result.quantity_after == D("20.00000000")
        assert result.price_after == D("50.00000000")

    def test_split_creates_no_pnl(self):
        """A split must not invent profit out of thin air."""
        p = make(qty=10, px=100, current_price=110)
        mv, pnl = p.market_value, p.unrealized_pnl
        p.apply_split(2)
        assert p.market_value == mv
        assert p.unrealized_pnl == pnl

    def test_reverse_split(self):
        p = make(qty=10, px=100, current_price=100)
        p.apply_split(D("0.5"))
        assert p.quantity == D("5.00000000")
        assert p.average_entry_price == D("200.00000000")
        assert p.market_value == D("1000.0000")

    def test_split_adjusts_current_price(self):
        p = make(qty=10, px=100, current_price=120)
        p.apply_split(2)
        assert p.current_price == D("60.00000000")

    def test_split_across_multiple_lots(self):
        p = make(CostBasisMethod.FIFO, qty=10, px=100)
        p.add_shares(10, 200)
        p.apply_split(2)
        assert p.quantity == D("40.00000000")
        assert p.lot_count == 2
        assert p.calculate_average_price() == D("75.00000000")   # was 150

    def test_split_on_short(self):
        p = make(qty=-10, px=100, current_price=100)
        before = p.market_value                       # -1000
        p.apply_split(2)
        assert p.quantity == D("-20.00000000")
        assert p.average_entry_price == D("50.00000000")
        # Value is preserved, exactly as for a long: -10x100 == -20x50.
        assert p.market_value == before == D("-1000.0000")

    def test_split_leaves_realized_pnl_alone(self):
        """Already-banked P&L is currency; a later split cannot change it."""
        p = make(qty=20, px=100)
        p.reduce_shares(10, 110)
        assert p.realized_pnl == D("100.0000")
        p.apply_split(2)
        assert p.realized_pnl == D("100.0000")

    @pytest.mark.parametrize("ratio", [0, -1, D("-0.5")])
    def test_invalid_ratio_rejected(self, ratio):
        with pytest.raises(ValidationError, match="split ratio must be positive"):
            make().apply_split(ratio)

    def test_split_on_closed_position_rejected(self):
        p = make()
        p.close(110)
        with pytest.raises(ValidationError, match="closed position"):
            p.apply_split(2)

    def test_three_for_two_split(self):
        p = make(qty=100, px=90, current_price=90)
        mv = p.market_value
        p.apply_split(D("1.5"))
        assert p.quantity == D("150.00000000")
        assert p.average_entry_price == D("60.00000000")
        assert p.market_value == mv


# ===========================================================================
# Dividends
# ===========================================================================


class TestDividends:
    def test_long_receives_cash(self):
        assert make(qty=10, px=100).apply_dividend(5).cash_amount == D("50.0000")

    def test_short_pays_cash(self):
        """A real cost that naive models silently omit."""
        assert make(qty=-10, px=100).apply_dividend(5).cash_amount == D("-50.0000")

    def test_cost_basis_unchanged_by_default(self):
        p = make(qty=10, px=100)
        p.apply_dividend(5)
        assert p.average_entry_price == D("100.00000000")

    def test_cost_basis_mode_lowers_entry_price(self):
        p = make(qty=10, px=100)
        result = p.apply_dividend(5, reduce_cost_basis=True)
        assert result.cost_basis_reduced
        assert p.average_entry_price == D("95.00000000")

    def test_cost_basis_mode_increases_unrealized_pnl(self):
        p = make(qty=10, px=100, current_price=100)
        assert p.unrealized_pnl == D("0.0000")
        p.apply_dividend(5, reduce_cost_basis=True)
        assert p.unrealized_pnl == D("50.0000")

    def test_applies_to_every_lot(self):
        p = make(CostBasisMethod.FIFO, qty=10, px=100)
        p.add_shares(10, 200)
        p.apply_dividend(10, reduce_cost_basis=True)
        assert [lot.price for lot in p.lots] == [D("90.00000000"), D("190.00000000")]

    def test_zero_dividend_is_a_noop(self):
        p = make(qty=10, px=100)
        assert p.apply_dividend(0).cash_amount == D("0.0000")

    def test_negative_dividend_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            make().apply_dividend(-1)

    def test_dividend_on_closed_position_rejected(self):
        p = make()
        p.close(110)
        with pytest.raises(ValidationError, match="closed position"):
            p.apply_dividend(5)

    def test_large_dividend_keeps_price_positive(self):
        """Flooring protects Lot's positive-price invariant."""
        p = make(qty=10, px=5)
        p.apply_dividend(100, reduce_cost_basis=True)
        assert p.average_entry_price > D("0")


# ===========================================================================
# close_position alias
# ===========================================================================


def test_close_position_is_the_spec_spelling():
    p = make(qty=10, px=100)
    result = p.close_position(110, commission=2)
    assert result.fully_closed
    assert result.realized_pnl == D("100.0000")
    assert not p.is_open


# ===========================================================================
# Serialisation
# ===========================================================================


class TestSerialisation:
    def test_lot_book_survives_round_trip(self):
        p = make(CostBasisMethod.FIFO, qty=10, px=100)
        p.add_shares(10, 120)
        p.add_shares(5, 140)
        restored = Position.from_dict(p.to_dict())
        assert restored.cost_basis_method == "fifo"
        assert restored.lot_count == 3
        assert restored.calculate_average_price() == p.calculate_average_price()

    def test_restored_position_keeps_fifo_ordering(self):
        p = make(CostBasisMethod.FIFO, qty=10, px=100)
        p.add_shares(10, 120)
        restored = Position.from_dict(json.loads(json.dumps(p.to_dict())))
        assert restored.reduce_shares(10, 130).realized_pnl == D("300.0000")

    def test_survives_json(self):
        p = make(CostBasisMethod.LIFO, qty=D("3.14159265"), px=D("1234.56789012"))
        restored = Position.from_dict(json.loads(json.dumps(p.to_dict())))
        assert restored.quantity == D("3.14159265")
        assert restored.average_entry_price == D("1234.56789012")

    def test_legacy_payload_without_lot_book(self):
        """A Step 3 snapshot must still load."""
        payload = make(qty=10, px=100).to_dict()
        payload.pop("lot_book")
        payload.pop("cost_basis_method")
        restored = Position.from_dict(payload)
        assert restored.cost_basis_method == CostBasisMethod.AVERAGE
        assert restored.lot_count == 1
        assert restored.quantity == D("10.00000000")


# ===========================================================================
# Persistence
# ===========================================================================


class TestPositionPersistence:
    def test_save_requires_a_portfolio(self, db):
        with pytest.raises(ValidationError, match="portfolio_id is required"):
            make().save_to_db(db)

    def test_save_and_read_back(self, db):
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)

        p = make(qty=10, px=1500, current_price=1600)
        p.save_to_db(db, portfolio_id=parent.portfolio_id)

        row = db.fetch_one("SELECT * FROM positions WHERE symbol='INFY'")
        assert D(str(row["quantity"])) == D("10.00000000")
        assert D(str(row["current_price"])) == D("1600.00000000")
        assert row["status"] == "open"
        assert row["position_type"] == "long"

    def test_save_is_idempotent(self, db):
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        p = make(qty=10, px=1500)
        p.save_to_db(db, parent.portfolio_id)
        p.update_price(1700)
        p.save_to_db(db, parent.portfolio_id)

        assert db.fetch_scalar("SELECT count(*) FROM positions") == 1
        row = db.fetch_one("SELECT current_price FROM positions")
        assert D(str(row["current_price"])) == D("1700.00000000")

    def test_closed_position_persists_as_closed(self, db):
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        p = make(qty=10, px=1500)
        p.close(1600)
        p.save_to_db(db, parent.portfolio_id)
        row = db.fetch_one("SELECT status, closed_at FROM positions")
        assert row["status"] == "closed"
        assert row["closed_at"] is not None

    def test_uses_position_id_already_set(self, db):
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        p = make(qty=10, px=1500, portfolio_id=parent.portfolio_id)
        assert p.save_to_db(db) == p.position_id

    def test_lot_detail_is_not_persisted(self, db):
        """Documented limitation: the schema has no lots table.

        A reloaded FIFO position collapses to one lot at the stored average.
        Recorded here so the behaviour is deliberate, not a surprise.
        """
        parent = Portfolio(name="p", initial_capital=100_000)
        parent.save_to_db(db)
        p = make(CostBasisMethod.FIFO, qty=10, px=100, portfolio_id=parent.portfolio_id)
        p.add_shares(10, 120)
        p.save_to_db(db)

        reloaded = Portfolio.load_from_db(db, parent.portfolio_id).get_position("INFY")
        assert reloaded.quantity == D("20.00000000")
        assert reloaded.average_entry_price == D("110.00000000")
        assert reloaded.lot_count == 1        # lot granularity is gone

    def test_full_json_snapshot_does_keep_lots(self, db):
        """to_dict()/from_dict() is the lossless path — use it for state files."""
        p = make(CostBasisMethod.FIFO, qty=10, px=100)
        p.add_shares(10, 120)
        assert Position.from_dict(p.to_dict()).lot_count == 2
