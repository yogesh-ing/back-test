"""PRD §4.1/§4.2 — BidAskQuoteProvider: buy at ask, sell at bid, mark at the far side.

Two contracts under test:

1. The wrapper itself — protocol-compatible passthrough for the dashboard,
   side-aware ``execution_quote`` for fills, side-aware ``mark_quote`` for held
   legs, and full delegation so it can sit anywhere in the provider chain.
2. The paper broker seam — a wrapped provider makes fills cross the spread and
   marks conservative; an unwrapped provider keeps LTP behaviour byte-identical.

Missing/zero side prices fail CLOSED: no fill, no invented mark.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.options.paper_trading import FakeQuoteProvider, OptionPaperBroker
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    NoExecutableQuoteError,
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent

EXPIRY = SyntheticChainGenerator().next_monthly_expiry()  # always in the future


def _long_call_intent() -> TradeIntent:
    leg = OptionLeg(
        instrument_token="T24800",
        trading_symbol="NIFTYTEST24800CE",
        side="BUY",
        quantity=1,
        lot_size=25,
    )
    view = MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
    )
    return TradeIntent(
        view=view,
        structure_type="long_call",
        legs=(leg,),
        expiry=EXPIRY,
        strategy_name="test_strategy",
        metadata={"option_type": "CE", "strike": "24800"},
    )


class TestWrapper:
    def test_get_quote_is_an_unchanged_passthrough(self):
        provider = BidAskQuoteProvider(FakeQuoteProvider(default_price=100.0))
        assert provider.get_quote("T1") == {"ltp": 100.0, "bid": 99.5, "ask": 100.5}

    def test_buy_executes_at_ask(self):
        provider = BidAskQuoteProvider(FakeQuoteProvider(default_price=100.0))
        quote = provider.execution_quote("T1", "BUY")
        assert quote["ltp"] == 100.5
        assert quote["execution_side"] == "BUY"

    def test_sell_executes_at_bid(self):
        provider = BidAskQuoteProvider(FakeQuoteProvider(default_price=100.0))
        quote = provider.execution_quote("T1", "SELL")
        assert quote["ltp"] == 99.5
        assert quote["execution_side"] == "SELL"

    def test_missing_ask_fails_closed_instead_of_using_ltp(self):
        """§4.1: a one-sided book is not a price to buy at — refuse, don't substitute."""
        class BidOnly:
            def get_quote(self, token):
                return {"ltp": 42.0, "bid": 41.5, "ask": 0.0}

        provider = BidAskQuoteProvider(BidOnly())
        with pytest.raises(NoExecutableQuoteError) as exc:
            provider.execution_quote("T1", "BUY")
        assert exc.value.reason == "NO_EXECUTION_QUOTE"
        # the same book is still sellable at the bid
        assert provider.execution_quote("T1", "SELL")["ltp"] == 41.5

    def test_error_row_fails_closed(self):
        class DeadSession:
            def get_quote(self, token):
                return {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "error": "session expired"}

        provider = BidAskQuoteProvider(DeadSession())
        with pytest.raises(NoExecutableQuoteError):
            provider.execution_quote("T1", "BUY")

    def test_long_leg_marks_at_the_bid(self):
        provider = BidAskQuoteProvider(FakeQuoteProvider(default_price=100.0))
        quote = provider.mark_quote("T1", "BUY")
        assert quote["ltp"] == 99.5  # what selling now would fetch
        assert quote["mark_basis"] == "bid"

    def test_short_leg_marks_at_the_ask(self):
        provider = BidAskQuoteProvider(FakeQuoteProvider(default_price=100.0))
        quote = provider.mark_quote("T1", "SELL")
        assert quote["ltp"] == 100.5  # what buying back now would cost
        assert quote["mark_basis"] == "ask"

    def test_mark_without_a_bid_reports_a_failed_quote(self):
        """No bid → the mark is labelled failed so the broker keeps the last
        known price instead of marking a long to a number nobody offered."""
        class AskOnly:
            def get_quote(self, token):
                return {"ltp": 42.0, "bid": 0.0, "ask": 42.5}

        quote = BidAskQuoteProvider(AskOnly()).mark_quote("T1", "BUY")
        assert quote["ltp"] == 42.0  # untouched, and…
        assert quote["error"], "…flagged: the fail-closed MTM path reads this"

    def test_delegates_to_the_inner_provider(self):
        inner = SyntheticQuoteProvider()
        provider = BidAskQuoteProvider(inner)
        provider.set_spot("NIFTY", 25_000.0)  # __getattr__ delegation
        assert inner.get_spot("NIFTY") == 25_000.0
        assert provider.generator is inner.generator
        assert provider.source_name == "synthetic:bs+bidask"


class TestPaperBrokerSeam:
    def test_wrapped_provider_fills_a_buy_at_the_ask(self):
        broker = OptionPaperBroker(capital=1_000_000, slippage_pct=0)
        provider = BidAskQuoteProvider(FakeQuoteProvider(default_price=100.0))
        positions = broker.execute_structure(_long_call_intent(), provider)
        assert positions[0].entry_price == Decimal("100.50")  # ask, not LTP

    def test_wrapped_provider_closes_a_long_at_the_bid(self):
        broker = OptionPaperBroker(capital=1_000_000, slippage_pct=0)
        provider = BidAskQuoteProvider(FakeQuoteProvider(default_price=100.0))
        positions = broker.execute_structure(_long_call_intent(), provider)
        broker.close_structure(positions[0].structure_id, provider)
        # Entered at the ask (100.50), exited at the bid (99.50): the round
        # trip pays the full ₹1 spread × 25 lot — that IS the feature.
        assert positions[0].current_price == Decimal("99.50")  # bid, not LTP
        assert positions[0].realized_pnl == Decimal("-25.00")

    def test_unwrapped_provider_keeps_v1_ltp_fills(self):
        broker = OptionPaperBroker(capital=1_000_000, slippage_pct=0)
        provider = FakeQuoteProvider(default_price=100.0)
        positions = broker.execute_structure(_long_call_intent(), provider)
        assert positions[0].entry_price == Decimal("100.00")  # byte-identical V1

    def test_unpriceable_leg_leaves_no_book(self):
        """A BUY with no ask is refused before any position is created — the
        structure does not open half-filled."""
        class BidOnly:
            def get_quote(self, token):
                return {"ltp": 42.0, "bid": 41.5, "ask": 0.0}

        broker = OptionPaperBroker(capital=1_000_000, slippage_pct=0)
        provider = BidAskQuoteProvider(BidOnly())
        with pytest.raises(NoExecutableQuoteError):
            broker.execute_structure(_long_call_intent(), provider)
        assert broker.get_open_structures() == []
        assert float(broker.available_cash) == 1_000_000.0

    def test_held_long_is_marked_off_the_bid_not_the_ltp(self):
        """§4.2: the book carries the price it could actually exit at, so
        unrealized P&L and premium-% stops see the conservative number."""
        class Moving:
            def __init__(self):
                self.ltp = 100.0
                self.spread = 2.0

            def get_quote(self, token):
                return {
                    "ltp": self.ltp,
                    "bid": self.ltp - self.spread,
                    "ask": self.ltp + self.spread,
                }

        broker = OptionPaperBroker(capital=1_000_000, slippage_pct=0)
        provider = BidAskQuoteProvider(Moving())
        positions = broker.execute_structure(_long_call_intent(), provider)
        assert positions[0].entry_price == Decimal("102.00")  # bought at the ask

        provider.inner.ltp = 105.0
        unrealized = broker.update_mtm(provider)
        assert positions[0].current_price == Decimal("103.00")  # marked at the bid
        # Bought at 102 (ask), worth 103 (bid): a ₹1 net move, not the ₹3 LTP
        # move the unwrapped book would have booked.
        assert unrealized == Decimal("25.00")
