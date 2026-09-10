"""Tests for options paper trading (Phase 3).

Covers:
- T3.1: Multi-leg execution via OptionPaperBroker
- T3.2: Premium calculation (fill prices with slippage)
- T3.3: Lot size validation
- T3.4: Atomic execution (all legs fill or none)
- T3.5: OptionPosition dataclass
- T3.6: Position tracking by structure
- T3.7: MTM calculation
- T3.8: Position closing by structure_id
- T3.11: Integration: open/hold/close spread
- T3.12: P&L reconciliation
- T3.13: Error handling (insufficient margin, invalid lots)
- T3.14: Logging (fill events recorded)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest

from backtest.instruments.option import OptionContract
from backtest.instruments.base import ExerciseType, SettlementType
from backtest.strategy.intent import (
    Direction,
    MarketView,
    OptionLeg,
    TradeIntent,
)
from backtest.options.structures import BullCallSpread, LongCall, LongPut
from backtest.options.paper_trading import (
    FakeQuoteProvider,
    InsufficientMarginError,
    LotSizeValidationError,
    OptionPaperBroker,
    OptionPosition,
    PositionStatus,
    StructurePosition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_view(spot: int = 24800) -> MarketView:
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


def _make_long_call_intent(
    strike: int = 24800,
    spot: int = 24800,
) -> TradeIntent:
    leg = OptionLeg(
        instrument_token=f"T{strike}",
        trading_symbol=f"NIFTY26SEP{strike}CE",
        side="BUY",
        quantity=1,
        lot_size=25,
    )
    return TradeIntent(
        view=_make_view(spot),
        structure_type="long_call",
        legs=(leg,),
        expiry=date(2026, 9, 24),
        strategy_name="test_strategy",
        metadata={"option_type": "CE", "strike": str(strike)},
    )


def _make_bull_call_spread_intent(
    long_strike: int = 24700,
    short_strike: int = 25000,
) -> TradeIntent:
    long_leg = OptionLeg(
        instrument_token=f"T{long_strike}",
        trading_symbol=f"NIFTY26SEP{long_strike}CE",
        side="BUY",
        quantity=1,
        lot_size=25,
    )
    short_leg = OptionLeg(
        instrument_token=f"T{short_strike}",
        trading_symbol=f"NIFTY26SEP{short_strike}CE",
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


def _make_bear_put_spread_intent(
    long_strike: int = 24900,
    short_strike: int = 24600,
) -> TradeIntent:
    long_leg = OptionLeg(
        instrument_token=f"P{long_strike}",
        trading_symbol=f"NIFTY26SEP{long_strike}PE",
        side="BUY",
        quantity=1,
        lot_size=25,
    )
    short_leg = OptionLeg(
        instrument_token=f"P{short_strike}",
        trading_symbol=f"NIFTY26SEP{short_strike}PE",
        side="SELL",
        quantity=1,
        lot_size=25,
    )
    return TradeIntent(
        view=_make_view(),
        structure_type="bear_put_spread",
        legs=(long_leg, short_leg),
        expiry=date(2026, 9, 24),
        strategy_name="test_strategy",
        metadata={"option_type": "PE"},
    )


# ---------------------------------------------------------------------------
# OptionPosition tests
# ---------------------------------------------------------------------------

class TestOptionPosition:
    def test_long_position_pnl(self):
        pos = OptionPosition(
            side="BUY",
            quantity=2,
            lot_size=25,
            entry_price=Decimal("100"),
            current_price=Decimal("120"),
        )
        # Long 2 lots × 25 = 50 units
        # P&L = (120 - 100) × 50 = 1000
        pnl = pos.calculate_unrealized_pnl()
        assert pnl == Decimal("1000")

    def test_short_position_pnl(self):
        pos = OptionPosition(
            side="SELL",
            quantity=1,
            lot_size=25,
            entry_price=Decimal("100"),
            current_price=Decimal("80"),
        )
        # Short 1 lot × 25 = 25 units
        # P&L = (100 - 80) × 25 = 500
        pnl = pos.calculate_unrealized_pnl()
        assert pnl == Decimal("500")

    def test_close_long(self):
        pos = OptionPosition(
            side="BUY",
            quantity=1,
            lot_size=25,
            entry_price=Decimal("100"),
        )
        realized = pos.close(Decimal("130"))
        assert realized == Decimal("750")  # (130-100) × 25
        assert pos.status == PositionStatus.CLOSED

    def test_close_short(self):
        pos = OptionPosition(
            side="SELL",
            quantity=1,
            lot_size=25,
            entry_price=Decimal("100"),
        )
        realized = pos.close(Decimal("70"))
        assert realized == Decimal("750")  # (100-70) × 25
        assert pos.status == PositionStatus.CLOSED

    def test_total_quantity(self):
        pos = OptionPosition(quantity=3, lot_size=25)
        assert pos.total_quantity == 75

    def test_update_mtm(self):
        pos = OptionPosition(
            side="BUY",
            quantity=1,
            lot_size=25,
            entry_price=Decimal("100"),
        )
        pnl = pos.update_mtm(Decimal("110"))
        assert pnl == Decimal("250")  # (110-100) × 25
        assert pos.current_price == Decimal("110")

    def test_no_pnl_when_closed(self):
        pos = OptionPosition(
            side="BUY",
            quantity=1,
            lot_size=25,
            entry_price=Decimal("100"),
            current_price=Decimal("120"),
            status=PositionStatus.CLOSED,
        )
        pnl = pos.calculate_unrealized_pnl()
        assert pnl == Decimal("0")


# ---------------------------------------------------------------------------
# StructurePosition tests
# ---------------------------------------------------------------------------

class TestStructurePosition:
    def test_total_entry_cost_debit(self):
        leg1 = OptionPosition(side="BUY", quantity=1, lot_size=25, entry_price=Decimal("100"))
        leg2 = OptionPosition(side="SELL", quantity=1, lot_size=25, entry_price=Decimal("50"))
        structure = StructurePosition(
            structure_id="S1",
            structure_type="bull_call_spread",
            strategy_name="test",
            underlying="NIFTY",
            expiry=date(2026, 9, 24),
            legs=[leg1, leg2],
        )
        # Debit = 100×25 - 50×25 = 1250
        assert structure.total_entry_cost == Decimal("1250")

    def test_is_open(self):
        leg = OptionPosition(side="BUY", status=PositionStatus.OPEN)
        structure = StructurePosition(
            structure_id="S1",
            structure_type="long_call",
            strategy_name="test",
            underlying="NIFTY",
            expiry=date(2026, 9, 24),
            legs=[leg],
        )
        assert structure.is_open

    def test_total_unrealized(self):
        leg1 = OptionPosition(side="BUY", unrealized_pnl=Decimal("100"))
        leg2 = OptionPosition(side="SELL", unrealized_pnl=Decimal("-50"))
        structure = StructurePosition(
            structure_id="S1",
            structure_type="bull_call_spread",
            strategy_name="test",
            underlying="NIFTY",
            expiry=date(2026, 9, 24),
            legs=[leg1, leg2],
        )
        assert structure.total_unrealized_pnl == Decimal("50")


# ---------------------------------------------------------------------------
# OptionPaperBroker — execution tests
# ---------------------------------------------------------------------------

class TestOptionPaperBrokerExecution:
    def setup_method(self):
        self.broker = OptionPaperBroker(capital=1_000_000.0)
        self.quotes = FakeQuoteProvider(default_price=100.0)

    def test_execute_long_call(self):
        intent = _make_long_call_intent()
        positions = self.broker.execute_structure(intent, self.quotes)

        assert len(positions) == 1
        assert positions[0].side == "BUY"
        assert positions[0].trading_symbol == "NIFTY26SEP24800CE"
        assert positions[0].status == PositionStatus.OPEN
        assert positions[0].quantity == 1

    def test_execute_bull_call_spread(self):
        intent = _make_bull_call_spread_intent()
        positions = self.broker.execute_structure(intent, self.quotes)

        assert len(positions) == 2
        long_pos = [p for p in positions if p.side == "BUY"][0]
        short_pos = [p for p in positions if p.side == "SELL"][0]
        assert long_pos.trading_symbol == "NIFTY26SEP24700CE"
        assert short_pos.trading_symbol == "NIFTY26SEP25000CE"
        # Same structure_id
        assert long_pos.structure_id == short_pos.structure_id

    def test_cash_reduced_after_buy(self):
        initial_cash = self.broker.available_cash
        intent = _make_long_call_intent()
        self.broker.execute_structure(intent, self.quotes)
        assert self.broker.available_cash < initial_cash

    def test_execute_records_order_history(self):
        intent = _make_long_call_intent()
        self.broker.execute_structure(intent, self.quotes)
        assert len(self.broker._order_history) == 1
        record = self.broker._order_history[0]
        assert record["structure_type"] == "long_call"
        assert len(record["legs"]) == 1

    def test_execute_slippage_applied(self):
        intent = _make_long_call_intent()
        positions = self.broker.execute_structure(intent, self.quotes)
        # BUY slippage: price should be slightly above 100
        assert positions[0].entry_price > Decimal("100")


# ---------------------------------------------------------------------------
# Lot size validation
# ---------------------------------------------------------------------------

class TestLotSizeValidation:
    def test_validate_lot_size_zero(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        # OptionLeg rejects lot_size=0 in __post_init__, so test the broker's
        # validation directly with a properly constructed leg that has bad lot_size
        # We bypass OptionLeg validation by creating a mock leg
        from unittest.mock import MagicMock
        bad_leg = MagicMock()
        bad_leg.lot_size = 0
        bad_leg.quantity = 1
        bad_leg.trading_symbol = "N1"
        with pytest.raises(LotSizeValidationError):
            broker._validate_lot_size(bad_leg)

    def test_validate_quantity_zero(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        from unittest.mock import MagicMock
        bad_leg = MagicMock()
        bad_leg.lot_size = 25
        bad_leg.quantity = 0
        bad_leg.trading_symbol = "N1"
        with pytest.raises(LotSizeValidationError):
            broker._validate_lot_size(bad_leg)


# ---------------------------------------------------------------------------
# Insufficient margin
# ---------------------------------------------------------------------------

class TestInsufficientMargin:
    def test_rejects_when_no_cash(self):
        broker = OptionPaperBroker(capital=100.0)  # very low capital
        quotes = FakeQuoteProvider(default_price=1000.0)

        intent = _make_long_call_intent()
        with pytest.raises(InsufficientMarginError):
            broker.execute_structure(intent, quotes)

    def test_rejects_for_expensive_spread(self):
        # Use a very expensive spread where even with SELL leg, net cost exceeds capital
        broker = OptionPaperBroker(capital=500.0)
        quotes = FakeQuoteProvider(default_price=10000.0)

        intent = _make_bull_call_spread_intent()
        with pytest.raises(InsufficientMarginError):
            broker.execute_structure(intent, quotes)


# ---------------------------------------------------------------------------
# MTM update
# ---------------------------------------------------------------------------

class TestMTMUpdate:
    def test_updates_prices(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        intent = _make_long_call_intent()
        positions = broker.execute_structure(intent, quotes)

        # Price goes up
        quotes.set_price("T24800", 120.0)
        total_unrealized = broker.update_mtm(quotes)

        assert total_unrealized > 0
        assert positions[0].current_price == Decimal("120.00")

    def test_total_equity_includes_unrealized(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        intent = _make_long_call_intent()
        broker.execute_structure(intent, quotes)

        equity_before = broker.total_equity

        quotes.set_price("T24800", 150.0)
        broker.update_mtm(quotes)

        assert broker.total_equity > equity_before


# ---------------------------------------------------------------------------
# Position closing
# ---------------------------------------------------------------------------

class TestPositionClosing:
    def test_close_structure(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        intent = _make_long_call_intent()
        positions = broker.execute_structure(intent, quotes)
        structure_id = positions[0].structure_id

        # Price went up
        quotes.set_price("T24800", 130.0)
        pnl = broker.close_structure(structure_id, quotes)

        assert pnl > 0
        assert positions[0].status == PositionStatus.CLOSED

    def test_close_spread(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        intent = _make_bull_call_spread_intent()
        positions = broker.execute_structure(intent, quotes)
        structure_id = positions[0].structure_id

        # Both legs move
        quotes.set_price("T24700", 120.0)
        quotes.set_price("T25000", 80.0)
        pnl = broker.close_structure(structure_id, quotes)

        assert pnl > 0  # long leg gained, short leg gained
        assert all(p.status == PositionStatus.CLOSED for p in positions)

    def test_close_nonexistent_raises(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider()
        with pytest.raises(ValueError, match="not found"):
            broker.close_structure("nonexistent", quotes)


# ---------------------------------------------------------------------------
# Integration: open → hold → close spread
# ---------------------------------------------------------------------------

class TestIntegrationSpreadLifecycle:
    def test_full_lifecycle(self):
        """T3.11: Open → hold (MTM update) → close bull call spread."""
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        # Open
        intent = _make_bull_call_spread_intent(long_strike=24700, short_strike=25000)
        positions = broker.execute_structure(intent, quotes)
        structure_id = positions[0].structure_id

        # Cash is reduced by premium paid + slippage + commission
        assert broker.total_equity < broker.capital
        # But no unrealized P&L yet (prices haven't moved)
        assert broker.total_realized_pnl == Decimal("0")

        # Hold — prices move
        quotes.set_price("T24700", 130.0)  # long leg goes up
        quotes.set_price("T25000", 90.0)   # short leg goes down
        unrealized = broker.update_mtm(quotes)
        assert unrealized > 0

        # Close
        pnl = broker.close_structure(structure_id, quotes)
        assert pnl > 0
        assert broker.total_realized_pnl > 0
        assert len(broker.get_open_positions()) == 0

    def test_pnl_reconciliation(self):
        """T3.12: Realized P&L matches expected calculation."""
        broker = OptionPaperBroker(
            capital=1_000_000.0,
            slippage_pct=0.0,  # no slippage for clean math
            commission_per_lot=0.0,  # no commission for clean math
        )
        quotes = FakeQuoteProvider(default_price=100.0)

        intent = _make_long_call_intent(strike=24800)
        positions = broker.execute_structure(intent, quotes)
        structure_id = positions[0].structure_id

        # Price goes from 100 → 150
        quotes.set_price("T24800", 150.0)
        pnl = broker.close_structure(structure_id, quotes)

        # Expected: (150 - 100) × 25 = 1250
        assert pnl == Decimal("1250")


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------

class TestQueries:
    def test_get_open_positions(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider()

        broker.execute_structure(_make_long_call_intent(), quotes)
        broker.execute_structure(_make_bull_call_spread_intent(), quotes)

        open_pos = broker.get_open_positions()
        assert len(open_pos) == 3  # 1 + 2

    def test_get_open_structures(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider()

        broker.execute_structure(_make_long_call_intent(), quotes)
        broker.execute_structure(_make_bull_call_spread_intent(), quotes)

        structures = broker.get_open_structures()
        assert len(structures) == 2

    def test_positions_by_structure(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider()

        positions = broker.execute_structure(_make_bull_call_spread_intent(), quotes)
        structure_id = positions[0].structure_id

        by_structure = broker.get_positions_by_structure(structure_id)
        assert len(by_structure) == 2

    def test_total_margin_used(self):
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        broker.execute_structure(_make_bull_call_spread_intent(), quotes)
        # One sell leg → margin = 100 × 25 = 2500
        assert broker.total_margin_used > 0


# ---------------------------------------------------------------------------
# FakeQuoteProvider tests
# ---------------------------------------------------------------------------

class TestFakeQuoteProvider:
    def test_default_price(self):
        qp = FakeQuoteProvider(default_price=50.0)
        quote = qp.get_quote("ANY")
        assert quote["ltp"] == 50.0

    def test_custom_price(self):
        qp = FakeQuoteProvider()
        qp.set_price("T24800", 200.0)
        quote = qp.get_quote("T24800")
        assert quote["ltp"] == 200.0
