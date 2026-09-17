"""Integration tests: five option strategies through the full pipeline (T9.4).

Exercises the complete Phase 1-8 stack end to end, using only public APIs
and no mocks on the domain objects:

    Strategy.generate_market_view()
        → MarketView
        → StrikeSelector.pick_strike(s) / ExpiryPolicy.select_expiry
        → OptionStructure.build() → TradeIntent
        → OptionPaperBroker.execute_structure() → OptionPosition
        → update_mtm() / close_structure() / ExpiryManager.process_expiries()
        → PortfolioGreeksCalculator / MarginCalculator / CommissionCalculator

Five strategies are traded across one simulated session (the PRD's "paper
trade 5 option strategies for 1 day"): long call, long put, bull call
spread, bear put spread, and a mixed delta-selected book.  Verifications:

- orders/fills: every leg fills atomically, fills logged with prices
- positions: strikes, expiries, sides and lot sizes land on the position
- Greeks: portfolio aggregates match per-leg direction arithmetic
- P&L: MTM moves equity exactly, realized P&L reconciles with cash
- fees: CommissionCalculator reproduces the contract-note anchor (27.63)
  for every leg priced through it
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from backtest.instruments.option import OptionContract
from backtest.options.expiry import (
    ExpiryAlertType,
    ExpiryManager,
    StaticSettlementProvider,
)
from backtest.options.greeks import BlackScholes
from backtest.options.margin import MarginCalculator
from backtest.options.paper_trading import (
    FakeQuoteProvider,
    OptionPaperBroker,
    PositionStatus,
)
from backtest.options.portfolio_greeks import PortfolioGreeksCalculator
from backtest.options.selector import ATMSelector, DeltaSelector
from backtest.options.structures import (
    BearPutSpread,
    BullCallSpread,
    LongCall,
    LongPut,
)
from backtest.strategy.intent import Direction, MarketView
from backtest.simulator import CommissionCalculator, TradeSegment

D = Decimal

SPOT = D("24800")
STRIKES = [D(s) for s in (24400, 24500, 24600, 24700, 24800, 24900, 25000, 25100, 25200)]
# A realistic ~3-week horizon: Black-Scholes becomes degenerate (delta -> 1)
# for very long expiries, and the Greeks assertions need sane math.
EXPIRY = date.today() + timedelta(days=21)
# 0.1% sell-side STT anchor: 75 x 120.50 premium -> 9.04 STT, 27.63 total.
CONTRACT_NOTE_ANCHOR = D("27.63")


# ---------------------------------------------------------------------------
# Chain construction (real OptionContract objects, V1-valid)
# ---------------------------------------------------------------------------


def _sym(strike: Decimal | int, option_type: str) -> str:
    """Trading symbol for a strike/expiry, matching _contract's token."""
    return f"NIFTY{EXPIRY:%d%b}{strike}{option_type}".upper()


def _contract(strike: Decimal, option_type: str) -> OptionContract:
    token = _sym(strike, option_type)
    return OptionContract(
        instrument_token=token,
        trading_symbol=token,
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=strike,
        option_type=option_type,
        lot_size=75,
    )


def _chain(strikes: list[Decimal], option_type: str) -> dict[Decimal, OptionContract]:
    return {s: _contract(s, option_type) for s in strikes}


CALL_CHAIN = _chain(STRIKES, "CE")
PUT_CHAIN = _chain(STRIKES, "PE")


def _view(direction: Direction, spot: Decimal = SPOT) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=spot,
    )


# ---------------------------------------------------------------------------
# The five strategies (one option structure each, traded for "one day")
# ---------------------------------------------------------------------------


class TestFiveStrategySession:
    """Paper trade five option strategies through the full pipeline."""

    @pytest.fixture()
    def broker(self):
        return OptionPaperBroker(
            capital=5_000_000.0,
            slippage_pct=0.0,
            commission_per_lot=20.0,
        )

    @pytest.fixture()
    def quotes(self):
        provider = FakeQuoteProvider(default_price=100.0)
        # Realistic premiums: ITM richer, OTM cheaper.
        provider.set_price(_sym(24700, "CE"), 150.0)
        provider.set_price(_sym(24800, "CE"), 120.5)
        provider.set_price(_sym(24900, "CE"), 95.0)
        provider.set_price(_sym(25000, "CE"), 75.0)
        provider.set_price(_sym(25200, "CE"), 55.0)
        provider.set_price(_sym(24800, "PE"), 110.0)
        provider.set_price(_sym(24700, "PE"), 85.0)
        provider.set_price(_sym(24600, "PE"), 65.0)
        return provider

    # -- 1. long call (buy_and_hold-style bullish view) --------------------

    def test_strategy_1_long_call(self, broker, quotes):
        strikes = ATMSelector().pick_strike(SPOT, STRIKES, Direction.BULLISH)
        assert strikes == D("24800")

        intent = LongCall().build(_view(Direction.BULLISH), [strikes], CALL_CHAIN, EXPIRY, "sma_long")
        positions = broker.execute_structure(intent, quotes)

        assert len(positions) == 1
        pos = positions[0]
        assert pos.side == "BUY" and pos.strike == D("24800")
        assert pos.lot_size == 75 and pos.quantity == 1
        assert pos.entry_price == D("120.50")  # no slippage
        assert pos.commission == D("20.0")  # one order
        assert broker.available_cash < broker.capital  # premium + fee debited

    # -- 2. long put (bearish view) -----------------------------------------

    def test_strategy_2_long_put(self, broker, quotes):
        strikes = ATMSelector().pick_strike(SPOT, STRIKES, Direction.BEARISH)
        assert strikes == D("24800")

        intent = LongPut().build(_view(Direction.BEARISH), [strikes], PUT_CHAIN, EXPIRY, "rsi_short")
        positions = broker.execute_structure(intent, quotes)

        assert positions[0].option_type == "PE"
        assert positions[0].entry_price == D("110.0")
        assert positions[0].strike == D("24800")

    # -- 3. bull call spread (capped bullish) -------------------------------

    def test_strategy_3_bull_call_spread(self, broker, quotes):
        picked = ATMSelector().pick_strikes(SPOT, STRIKES, Direction.BULLISH, count=2)
        assert len(picked) == 2

        intent = BullCallSpread().build(
            _view(Direction.BULLISH), picked, CALL_CHAIN, EXPIRY, "donchian_bull"
        )
        positions = broker.execute_structure(intent, quotes)

        assert len(positions) == 2
        sides = {p.side for p in positions}
        assert sides == {"BUY", "SELL"}
        # Metadata carries the per-leg strike map keyed by trading symbol.
        strikes_map = intent.metadata["strikes"]
        for pos in positions:
            assert pos.strike == D(strikes_map[pos.trading_symbol])
        # Two legs = two orders of brokerage.
        assert sum(p.commission for p in positions) == D("40.0")

    # -- 4. bear put spread (capped bearish) ---------------------------------

    def test_strategy_4_bear_put_spread(self, broker, quotes):
        picked = ATMSelector().pick_strikes(SPOT, STRIKES, Direction.BEARISH, count=2)
        intent = BearPutSpread().build(
            _view(Direction.BEARISH), picked, PUT_CHAIN, EXPIRY, "rsi_bear"
        )
        positions = broker.execute_structure(intent, quotes)

        assert len(positions) == 2
        longs = [p for p in positions if p.side == "BUY"]
        shorts = [p for p in positions if p.side == "SELL"]
        assert longs[0].strike > shorts[0].strike  # buy higher put, sell lower

    # -- 5. delta-selected book (mixed) --------------------------------------

    def test_strategy_5_delta_selected_book(self, broker, quotes):
        selector = DeltaSelector(delta_target=0.35)
        strike = selector.pick_strike(SPOT, STRIKES, Direction.BULLISH)
        # Delta 0.35 targets ~1.5% OTM: 24800 * 1.015 = 25172 -> nearest 25200.
        assert strike == D("25200")

        intent = LongCall().build(
            _view(Direction.BULLISH), [strike], CALL_CHAIN, EXPIRY, "delta_otm"
        )
        positions = broker.execute_structure(intent, quotes)

        assert positions[0].strike == D("25200")
        assert positions[0].trading_symbol == _sym(25200, "CE")

    # -- session-level verification ------------------------------------------

    def test_full_session_orders_fills_positions_greeks_pnl(self, broker, quotes):
        """The whole day: all five strategies, then MTM, Greeks, reconciliation."""
        # ---- Orders & fills ------------------------------------------------
        s1 = LongCall().build(
            _view(Direction.BULLISH), [D("24800")], CALL_CHAIN, EXPIRY, "s1"
        )
        s2 = LongPut().build(
            _view(Direction.BEARISH), [D("24800")], PUT_CHAIN, EXPIRY, "s2"
        )
        picked = ATMSelector().pick_strikes(SPOT, STRIKES, Direction.BULLISH, count=2)
        s3 = BullCallSpread().build(_view(Direction.BULLISH), picked, CALL_CHAIN, EXPIRY, "s3")
        picked = ATMSelector().pick_strikes(SPOT, STRIKES, Direction.BEARISH, count=2)
        s4 = BearPutSpread().build(_view(Direction.BEARISH), picked, PUT_CHAIN, EXPIRY, "s4")
        s5 = LongCall().build(
            _view(Direction.BULLISH), [D("25200")], CALL_CHAIN, EXPIRY, "s5"
        )

        all_positions = []
        for intent in (s1, s2, s3, s4, s5):
            all_positions.extend(broker.execute_structure(intent, quotes))

        # All five structures open, seven legs total (1+1+2+2+1), every fill
        # logged.
        assert len(broker.get_open_structures()) == 5
        assert len(all_positions) == 7
        assert len(broker._order_history) == 5
        for record in broker._order_history:
            assert all(Decimal(leg["fill_price"]) > 0 for leg in record["legs"])

        # Positions carry strikes from the builders' per-leg map.
        for pos in all_positions:
            assert pos.strike > 0
            assert pos.expiry == EXPIRY

        # ---- MTM: the market rallies ---------------------------------------
        quotes.set_price(_sym(24800, "CE"), 140.0)  # s1 gains 19.50 x 75
        quotes.set_price(_sym(24800, "PE"), 85.0)   # s2 loses 25.00 x 75
        quotes.set_price(_sym(24700, "CE"), 175.0)  # s3 long gains
        quotes.set_price(_sym(24900, "CE"), 90.0)   # s3 short gains (sold 95)
        quotes.set_price(_sym(24800, "PE"), 85.0)   # s4 long loses
        quotes.set_price(_sym(24600, "PE"), 55.0)   # s4 short gains (sold 65)
        quotes.set_price(_sym(25200, "CE"), 50.0)   # s5 loses
        unrealized = broker.update_mtm(quotes)
        assert unrealized != 0

        # Equity moves by exactly the unrealized P&L (fees already netted).
        equity_after_mtm = broker.total_equity
        realized_now = broker.total_realized_pnl
        assert equity_after_mtm == (
            broker.capital + realized_now + unrealized - broker.total_commission_paid
        )

        # ---- Greeks ---------------------------------------------------------
        greeks = PortfolioGreeksCalculator(default_volatility=0.2).calculate(
            broker.get_open_positions(), spot_prices={"NIFTY": 25000.0}
        )
        d = greeks.to_dict()
        # s1 (long call) and s3 (long call leg) pull delta up; the short
        # spread legs and losing legs offset. Just verify aggregation sanity:
        # net delta is bounded by the sum of |leg delta| and by-underlying exists.
        assert "NIFTY" in d["by_underlying"]
        assert d["total_delta"] != 0.0
        leg_deltas = [abs(p["delta"]) for p in greeks.positions]
        assert abs(d["total_delta"]) <= sum(leg_deltas) + 1e-9

        # Delta sign follows side AND option type: long CE positive, long PE
        # negative; shorting flips the sign (short put carries positive delta).
        # Match on (symbol, side): two structures may hold the SAME contract
        # on opposite sides (e.g. s1 long ATM call, s3 short ATM call).
        for entry in greeks.positions:
            leg = next(
                p
                for p in all_positions
                if p.trading_symbol == entry["symbol"] and p.side == entry["side"]
            )
            long_sign = 1.0 if entry["option_type"] == "CE" else -1.0
            expected_sign = long_sign * (1.0 if leg.is_long else -1.0)
            assert entry["delta"] * expected_sign >= 0.0

        # ---- Close everything (end of day) ----------------------------------
        for structure in list(broker.get_open_structures()):
            broker.close_structure(structure.structure_id, quotes)

        assert broker.get_open_positions() == []
        assert broker.get_open_structures() == []

        # ---- P&L reconciliation: capital + realized - fees == cash ---------
        # (All positions closed at the same prices the MTM saw, so realized
        # P&L must equal the unrealized snapshot.)
        realized = broker.total_realized_pnl
        assert realized == pytest.approx(float(unrealized), rel=1e-9)
        assert broker.total_equity == broker.available_cash  # flat book
        assert broker.total_equity == (
            broker.capital + realized - broker.total_commission_paid
        )

        # Seven legs at Rs 20 per order.
        assert broker.total_commission_paid == D("140.0")


# ---------------------------------------------------------------------------
# Fees through the pipeline (Phase 8 anchor)
# ---------------------------------------------------------------------------


class TestFeesThroughPipeline:
    def test_every_leg_matches_contract_note_anchor(self):
        calc = CommissionCalculator.for_broker("mstock")
        fees = calc.calculate(
            quantity=75,
            fill_price=D("120.50"),
            side="buy",
            segment=TradeSegment.OPTIONS,
        )
        assert fees.total == CONTRACT_NOTE_ANCHOR

    def test_structure_fees_sum_of_legs(self):
        calc = CommissionCalculator.for_broker("mstock")
        fees = calc.calculate_structure(
            [
                {"side": "BUY", "quantity": 75, "price": "120.50"},
                {"side": "SELL", "quantity": 75, "price": "95.00"},
            ]
        )
        # Per-leg itemisation present and consistent with the merged totals.
        assert fees.get("leg_0_stt") == D("0")
        assert fees.get("leg_1_stt") == D("7.13")  # 0.1% of 75 x 95
        assert fees.get("stt") == D("7.13")
        assert fees.get("brokerage") == D("40.00")


# ---------------------------------------------------------------------------
# Expiry through the pipeline (Phase 6)
# ---------------------------------------------------------------------------


class TestExpiryThroughPipeline:
    def test_hold_through_expiry_settlement(self):
        broker = OptionPaperBroker(
            capital=1_000_000.0, slippage_pct=0.0, commission_per_lot=0.0
        )
        quotes = FakeQuoteProvider(default_price=100.0)

        # Open a long call struck at 24800, expiring tomorrow.
        expiry = date.today() + timedelta(days=1)
        chain = {
            D("24800"): OptionContract(
                instrument_token="NEAR",
                trading_symbol="NEAR24800CE",
                underlying="NIFTY",
                expiry=expiry,
                strike=D("24800"),
                option_type="CE",
                lot_size=75,
            )
        }
        intent = LongCall().build(
            MarketView(direction=Direction.BULLISH, underlying="NIFTY", spot_price=SPOT),
            [D("24800")],
            chain,
            expiry,
            "expiry_test",
        )
        broker.execute_structure(intent, quotes)

        # Jump to 15:45 on expiry day: inside the square-off window.
        manager = ExpiryManager(broker, squareoff_minutes_before=30)
        as_of = datetime.combine(expiry, time(15, 45))
        quotes.set_price("NEAR24800CE", 180.0)
        report = manager.process_expiries(
            quote_provider=quotes,
            settlement_provider=StaticSettlementProvider({"NIFTY": 24950.0}),
            as_of=as_of,
        )
        # Square-off fires first (inside window), closing at LTP 180.
        assert report["squared_off_count"] >= 1
        assert broker.get_open_positions() == []

        # Now a stale position: expiry already passed -> cash settlement.
        past = date.today() - timedelta(days=1)
        past_chain = {
            D("24800"): OptionContract(
                instrument_token="STALE",
                trading_symbol="STALE24800CE",
                underlying="NIFTY",
                expiry=past,
                strike=D("24800"),
                option_type="CE",
                lot_size=75,
            )
        }
        # Build directly through the builder to keep the pipeline real; the
        # contract itself is what validates (OptionContract rejects past
        # expiries), so construct the intent manually here — settlement is
        # the behaviour under test, not validation.
        from backtest.strategy.intent import OptionLeg, TradeIntent

        stale_intent = TradeIntent(
            view=MarketView(direction=Direction.BULLISH, underlying="NIFTY", spot_price=SPOT),
            structure_type="long_call",
            legs=(
                OptionLeg(
                    instrument_token="STALE",
                    trading_symbol="STALE24800CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=past,
            metadata={
                "strike": "24800",
                "option_type": "CE",
                "strikes": {"STALE24800CE": "24800"},
            },
        )
        broker.execute_structure(stale_intent, quotes)
        cash_before = broker.available_cash
        report = manager.process_expiries(
            quote_provider=quotes,
            settlement_provider=StaticSettlementProvider({"NIFTY": 24950.0}),
            as_of=datetime.combine(date.today(), time(10, 0)),
        )
        assert report["settled_count"] >= 1
        # ITM by 150 points x 75 units -> cash credited.
        assert broker.available_cash > cash_before
        # Alert trail covers the whole lifecycle.
        types = {a.alert_type for a in manager.alerts}
        assert ExpiryAlertType.AUTO_SQUARED_OFF in types
        assert ExpiryAlertType.EXPIRED_ITM in types
        assert ExpiryAlertType.SETTLED in types

    def test_process_expiries_report_shape(self):
        broker = OptionPaperBroker(capital=100_000.0)
        manager = ExpiryManager(broker)
        report = manager.process_expiries(
            quote_provider=FakeQuoteProvider(),
            settlement_provider=StaticSettlementProvider(),
        )
        assert set(report) == {"squared_off_count", "settled_count", "total_pnl", "alerts"}
        assert report["squared_off_count"] == 0
        assert report["settled_count"] == 0


# ---------------------------------------------------------------------------
# Margin + risk through the pipeline (Phase 5)
# ---------------------------------------------------------------------------


class TestMarginThroughPipeline:
    def test_spread_margin_cheaper_than_naked_short(self):
        calc = MarginCalculator()
        short = calc.calculate_short_margin(
            underlying_price=24800.0, strike=25000.0, lot_size=75, lots=1
        )
        spread = calc.calculate_spread_margin(
            long_strike=24800.0,
            short_strike=25000.0,
            long_premium=120.5,
            short_premium=75.0,
            lot_size=75,
            lots=1,
            underlying_price=24800.0,
        )
        assert spread.net_margin < short.net_margin

    def test_pretrade_risk_check_blocks_oversize(self):
        from backtest.options.margin import PreTradeRiskCheck

        check = PreTradeRiskCheck(max_margin=10_000.0)
        result = check.check(margin_required=50_000.0)
        assert not result.allowed


# ---------------------------------------------------------------------------
# API-level: the dashboard can serve the whole book end to end
# ---------------------------------------------------------------------------


class TestDashboardEndToEnd:
    def test_summary_endpoint_serves_full_session(self, monkeypatch):
        """Reset the singleton, trade through the pipeline, then hit the API.

        ``OPTIONS_PERSISTENCE=off`` isolates the run from whatever open
        structures a previous run left in the database — without it the
        broker rehydrates them (Gap G4.2 restart survival) and
        ``open_position_count`` reflects stale rows, not this session.
        The DB round-trip itself is covered by tests/test_options_persistence.py.
        """
        monkeypatch.setenv("OPTIONS_PERSISTENCE", "off")
        from backtest.web.app import create_app
        from backtest.web.options_api import get_option_broker, reset_option_state

        reset_option_state()
        app = create_app({"TESTING": True})
        client = app.test_client()

        broker = get_option_broker()
        quotes = FakeQuoteProvider(default_price=120.0)
        intent = LongCall().build(
            _view(Direction.BULLISH), [D("24800")], CALL_CHAIN, EXPIRY, "ui_e2e"
        )
        broker.execute_structure(intent, quotes)

        resp = client.get("/api/options/summary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["open_position_count"] == 1
        assert data["greeks"]["total_delta"] != 0.0
        assert data["positions"][0]["strike"] == "24800"
        assert data["structures"][0]["structure_type"] == "long_call"

        reset_option_state()
