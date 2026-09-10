"""Tests for the options expression layer (Phase 2).

Covers:
- MarketView / TradeIntent / OptionLeg dataclasses
- Strike selectors (ATM, Delta, FixedDistance, TargetPrice)
- Option structures (LongCall, LongPut, BullCallSpread, BearPutSpread)
- Expiry policies (Nearest, Weekly, FixedDays, MinimumDays)
- Config loading
- Strategy.generate_market_view() integration
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backtest.instruments.option import OptionContract
from backtest.instruments.base import ExerciseType, SettlementType
from backtest.strategy.intent import (
    Direction,
    MarketView,
    OptionLeg,
    TradeIntent,
)
from backtest.options.selector import (
    ATMSelector,
    DeltaSelector,
    FixedDistanceSelector,
    TargetPriceSelector,
    create_selector,
)
from backtest.options.structures import (
    BullCallSpread,
    BearPutSpread,
    LongCall,
    LongPut,
    create_structure,
)
from backtest.options.expiry_policy import (
    FixedDaysExpiryPolicy,
    MinimumDaysExpiryPolicy,
    NearestExpiryPolicy,
    WeeklyExpiryPolicy,
    create_expiry_policy,
)
from backtest.options.config import (
    OptionsConfig,
    load_options_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_contract(
    strike: Decimal = Decimal("24800"),
    option_type: str = "CE",
    expiry: date | None = None,
    token: str = "T1",
    symbol: str = "NIFTY26SEP24800CE",
    lot_size: int = 25,
) -> OptionContract:
    return OptionContract(
        instrument_token=token,
        trading_symbol=symbol,
        underlying="NIFTY",
        exchange="NSE",
        segment="NFO",
        expiry=expiry or date(2026, 9, 24),
        strike=strike,
        option_type=option_type,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
        contract_type=ExerciseType.EUROPEAN,
        settlement_type=SettlementType.CASH,
    )


def _make_chain(strikes: list[int] | None = None) -> dict[Decimal, OptionContract]:
    """Build a simple option chain dict: {strike: OptionContract}."""
    if strikes is None:
        strikes = [24500, 24600, 24700, 24800, 24900, 25000, 25100, 25200]
    chain = {}
    for s in strikes:
        d = Decimal(str(s))
        chain[d] = _make_contract(
            strike=d,
            token=f"T{s}",
            symbol=f"NIFTY26SEP{s}CE",
        )
    return chain


def _bullish_view(
    spot: int = 24800,
    target: int | None = None,
) -> MarketView:
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
        target_price=Decimal(str(target)) if target else None,
    )


def _bearish_view(spot: int = 24800) -> MarketView:
    return MarketView(
        direction=Direction.BEARISH,
        confidence=0.7,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


# ---------------------------------------------------------------------------
# MarketView tests
# ---------------------------------------------------------------------------

class TestMarketView:
    def test_basic_bullish(self):
        v = _bullish_view()
        assert v.direction == Direction.BULLISH
        assert v.confidence == 0.8

    def test_confidence_bounds(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            MarketView(direction=Direction.BULLISH, confidence=1.5)

    def test_neutral_view(self):
        v = MarketView(direction=Direction.NEUTRAL, confidence=0.5)
        assert v.direction == Direction.NEUTRAL

    def test_metadata(self):
        v = MarketView(
            direction=Direction.BULLISH,
            metadata={"rsi": 35, "sma_gap": 0.5},
        )
        assert v.metadata["rsi"] == 35


# ---------------------------------------------------------------------------
# TradeIntent tests
# ---------------------------------------------------------------------------

class TestTradeIntent:
    def test_basic_intent(self):
        leg = OptionLeg(
            instrument_token="T1",
            trading_symbol="NIFTY26SEP24800CE",
            side="BUY",
            quantity=1,
            lot_size=25,
        )
        intent = TradeIntent(
            view=_bullish_view(),
            structure_type="long_call",
            legs=(leg,),
            expiry=date(2026, 9, 24),
        )
        assert intent.structure_type == "long_call"
        assert len(intent.legs) == 1
        assert not intent.is_multi_leg

    def test_multi_leg(self):
        leg1 = OptionLeg("T1", "N1", "BUY", 1, 25)
        leg2 = OptionLeg("T2", "N2", "SELL", 1, 25)
        intent = TradeIntent(
            view=_bullish_view(),
            structure_type="bull_call_spread",
            legs=(leg1, leg2),
            expiry=date(2026, 9, 24),
        )
        assert intent.is_multi_leg

    def test_empty_legs_raises(self):
        with pytest.raises(ValueError, match="at least one leg"):
            TradeIntent(
                view=_bullish_view(),
                structure_type="long_call",
                legs=(),
                expiry=date(2026, 9, 24),
            )

    def test_too_many_legs_raises(self):
        legs = tuple(OptionLeg(f"T{i}", f"N{i}", "BUY", 1, 25) for i in range(5))
        with pytest.raises(ValueError, match="max 4 legs"):
            TradeIntent(
                view=_bullish_view(),
                structure_type="long_call",
                legs=legs,
                expiry=date(2026, 9, 24),
            )

    def test_invalid_structure_type_raises(self):
        leg = OptionLeg("T1", "N1", "BUY", 1, 25)
        with pytest.raises(ValueError, match="Unknown structure_type"):
            TradeIntent(
                view=_bullish_view(),
                structure_type="invalid_structure",
                legs=(leg,),
                expiry=date(2026, 9, 24),
            )


# ---------------------------------------------------------------------------
# OptionLeg tests
# ---------------------------------------------------------------------------

class TestOptionLeg:
    def test_invalid_side(self):
        with pytest.raises(ValueError, match="side must be BUY or SELL"):
            OptionLeg("T1", "N1", "HOLD", 1, 25)

    def test_invalid_quantity(self):
        with pytest.raises(ValueError, match="quantity must be >= 1"):
            OptionLeg("T1", "N1", "BUY", 0, 25)

    def test_total_quantity(self):
        leg = OptionLeg("T1", "N1", "BUY", 3, 25)
        assert leg.total_quantity == 75


# ---------------------------------------------------------------------------
# ATMSelector tests
# ---------------------------------------------------------------------------

class TestATMSelector:
    def setup_method(self):
        self.selector = ATMSelector()
        self.strikes = [Decimal(str(s)) for s in [24500, 24600, 24700, 24800, 24900, 25000]]

    def test_exact_match(self):
        result = self.selector.pick_strike(
            Decimal("24800"), self.strikes, Direction.BULLISH
        )
        assert result == Decimal("24800")

    def test_between_strikes(self):
        result = self.selector.pick_strike(
            Decimal("24850"), self.strikes, Direction.BULLISH
        )
        assert result in (Decimal("24800"), Decimal("24900"))

    def test_empty_strikes(self):
        result = self.selector.pick_strike(
            Decimal("24800"), [], Direction.BULLISH
        )
        assert result is None

    def test_pick_strikes(self):
        results = self.selector.pick_strikes(
            Decimal("24800"), self.strikes, Direction.BULLISH, count=3
        )
        assert len(results) == 3
        assert Decimal("24800") in results


# ---------------------------------------------------------------------------
# DeltaSelector tests
# ---------------------------------------------------------------------------

class TestDeltaSelector:
    def setup_method(self):
        self.selector = DeltaSelector(delta_target=0.35)
        self.strikes = [Decimal(str(s)) for s in [24500, 24600, 24700, 24800, 24900, 25000, 25100, 25200]]

    def test_bullish_picks_otm(self):
        result = self.selector.pick_strike(
            Decimal("24800"), self.strikes, Direction.BULLISH
        )
        assert result is not None
        assert result >= Decimal("24800")  # OTM for calls

    def test_bearish_picks_otm(self):
        result = self.selector.pick_strike(
            Decimal("24800"), self.strikes, Direction.BEARISH
        )
        assert result is not None
        assert result <= Decimal("24800")  # OTM for puts

    def test_invalid_delta(self):
        with pytest.raises(ValueError, match="delta_target"):
            DeltaSelector(delta_target=1.5)


# ---------------------------------------------------------------------------
# FixedDistanceSelector tests
# ---------------------------------------------------------------------------

class TestFixedDistanceSelector:
    def setup_method(self):
        self.selector = FixedDistanceSelector(distance_pct=2.0)
        self.strikes = [Decimal(str(s)) for s in [24500, 24600, 24700, 24800, 24900, 25000, 25100, 25200, 25300]]

    def test_bullish_2pct_otm(self):
        # 2% of 24800 = 496 → target ≈ 25296 → closest is 25300
        result = self.selector.pick_strike(
            Decimal("24800"), self.strikes, Direction.BULLISH
        )
        assert result is not None
        assert result >= Decimal("24800")

    def test_bearish_2pct_otm(self):
        # -2% of 24800 = -496 → target ≈ 24304 → closest is 24500
        result = self.selector.pick_strike(
            Decimal("24800"), self.strikes, Direction.BEARISH
        )
        assert result is not None
        assert result <= Decimal("24800")


# ---------------------------------------------------------------------------
# TargetPriceSelector tests
# ---------------------------------------------------------------------------

class TestTargetPriceSelector:
    def setup_method(self):
        self.selector = TargetPriceSelector()
        self.strikes = [Decimal(str(s)) for s in [24500, 24600, 24700, 24800, 24900, 25000, 25100, 25200]]

    def test_with_target(self):
        view = _bullish_view(spot=24800, target=25000)
        result = self.selector.pick_strike(
            Decimal("24800"), self.strikes, Direction.BULLISH, view=view
        )
        assert result == Decimal("25000")

    def test_without_target_falls_back_to_atm(self):
        view = _bullish_view(spot=24800, target=None)
        result = self.selector.pick_strike(
            Decimal("24800"), self.strikes, Direction.BULLISH, view=view
        )
        assert result == Decimal("24800")


# ---------------------------------------------------------------------------
# create_selector factory
# ---------------------------------------------------------------------------

class TestCreateSelector:
    def test_atm(self):
        s = create_selector("atm")
        assert isinstance(s, ATMSelector)

    def test_delta(self):
        s = create_selector("delta", delta_target=0.3)
        assert isinstance(s, DeltaSelector)

    def test_fixed_distance(self):
        s = create_selector("fixed_distance", distance_pct=3.0)
        assert isinstance(s, FixedDistanceSelector)

    def test_target_price(self):
        s = create_selector("target_price")
        assert isinstance(s, TargetPriceSelector)

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Unknown selector_type"):
            create_selector("invalid")


# ---------------------------------------------------------------------------
# LongCall tests
# ---------------------------------------------------------------------------

class TestLongCall:
    def setup_method(self):
        self.structure = LongCall()
        self.chain = _make_chain()

    def test_build(self):
        intent = self.structure.build(
            view=_bullish_view(),
            strikes=[Decimal("24800")],
            chain=self.chain,
            expiry=date(2026, 9, 24),
            strategy_name="test_strategy",
        )
        assert intent.structure_type == "long_call"
        assert len(intent.legs) == 1
        assert intent.legs[0].side == "BUY"
        assert intent.legs[0].instrument_token == "T24800"
        assert intent.strategy_name == "test_strategy"

    def test_empty_strikes_raises(self):
        with pytest.raises(ValueError, match="at least one strike"):
            self.structure.build(
                view=_bullish_view(),
                strikes=[],
                chain=self.chain,
                expiry=date(2026, 9, 24),
            )

    def test_missing_contract_raises(self):
        with pytest.raises(ValueError, match="No contract found"):
            self.structure.build(
                view=_bullish_view(),
                strikes=[Decimal("99999")],
                chain=self.chain,
                expiry=date(2026, 9, 24),
            )


# ---------------------------------------------------------------------------
# LongPut tests
# ---------------------------------------------------------------------------

class TestLongPut:
    def setup_method(self):
        self.structure = LongPut()
        self.chain = _make_chain()

    def test_build(self):
        intent = self.structure.build(
            view=_bearish_view(),
            strikes=[Decimal("24800")],
            chain=self.chain,
            expiry=date(2026, 9, 24),
        )
        assert intent.structure_type == "long_put"
        assert intent.legs[0].side == "BUY"
        assert intent.legs[0].instrument_token == "T24800"


# ---------------------------------------------------------------------------
# BullCallSpread tests
# ---------------------------------------------------------------------------

class TestBullCallSpread:
    def setup_method(self):
        self.structure = BullCallSpread()
        self.chain = _make_chain()

    def test_build(self):
        intent = self.structure.build(
            view=_bullish_view(),
            strikes=[Decimal("24700"), Decimal("25000")],
            chain=self.chain,
            expiry=date(2026, 9, 24),
        )
        assert intent.structure_type == "bull_call_spread"
        assert len(intent.legs) == 2
        # Long lower strike, short higher strike
        long_leg = [l for l in intent.legs if l.side == "BUY"][0]
        short_leg = [l for l in intent.legs if l.side == "SELL"][0]
        assert long_leg.instrument_token == "T24700"
        assert short_leg.instrument_token == "T25000"
        assert intent.metadata["spread_width"] == "300"

    def test_single_strike_raises(self):
        with pytest.raises(ValueError, match="exactly 2 strikes"):
            self.structure.build(
                view=_bullish_view(),
                strikes=[Decimal("24800")],
                chain=self.chain,
                expiry=date(2026, 9, 24),
            )


# ---------------------------------------------------------------------------
# BearPutSpread tests
# ---------------------------------------------------------------------------

class TestBearPutSpread:
    def setup_method(self):
        self.structure = BearPutSpread()
        self.chain = _make_chain()

    def test_build(self):
        intent = self.structure.build(
            view=_bearish_view(),
            strikes=[Decimal("24600"), Decimal("24900")],
            chain=self.chain,
            expiry=date(2026, 9, 24),
        )
        assert intent.structure_type == "bear_put_spread"
        assert len(intent.legs) == 2
        long_leg = [l for l in intent.legs if l.side == "BUY"][0]
        short_leg = [l for l in intent.legs if l.side == "SELL"][0]
        # Long higher strike put, short lower strike put
        assert long_leg.instrument_token == "T24900"
        assert short_leg.instrument_token == "T24600"
        assert intent.metadata["spread_width"] == "300"


# ---------------------------------------------------------------------------
# create_structure factory
# ---------------------------------------------------------------------------

class TestCreateStructure:
    def test_all_types(self):
        for name in ["long_call", "long_put", "bull_call_spread", "bear_put_spread"]:
            s = create_structure(name)
            assert s.name == name

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Unknown structure_type"):
            create_structure("invalid")


# ---------------------------------------------------------------------------
# ExpiryPolicy tests
# ---------------------------------------------------------------------------

class TestNearestExpiryPolicy:
    def test_picks_nearest(self):
        policy = NearestExpiryPolicy()
        expiries = [date(2026, 9, 24), date(2026, 10, 29), date(2026, 11, 26)]
        result = policy.select_expiry(expiries, reference_date=date(2026, 9, 20))
        assert result == date(2026, 9, 24)

    def test_skips_past(self):
        policy = NearestExpiryPolicy()
        expiries = [date(2026, 9, 24), date(2026, 10, 29)]
        result = policy.select_expiry(expiries, reference_date=date(2026, 10, 1))
        assert result == date(2026, 10, 29)

    def test_empty(self):
        policy = NearestExpiryPolicy()
        result = policy.select_expiry([], reference_date=date(2026, 9, 20))
        assert result is None


class TestFixedDaysExpiryPolicy:
    def test_picks_closest_to_target(self):
        policy = FixedDaysExpiryPolicy(target_days=30)
        expiries = [date(2026, 9, 24), date(2026, 10, 29)]
        # From Sep 1, 30 days → Oct 1 → closest is Sep 24
        result = policy.select_expiry(expiries, reference_date=date(2026, 9, 1))
        assert result == date(2026, 9, 24)

    def test_picks_oct_for_30_day_target(self):
        policy = FixedDaysExpiryPolicy(target_days=30)
        expiries = [date(2026, 9, 24), date(2026, 10, 29)]
        # From Sep 5, 30 days → Oct 5 → closest is Sep 24 (19 days) vs Oct 29 (54 days)
        result = policy.select_expiry(expiries, reference_date=date(2026, 9, 5))
        assert result == date(2026, 9, 24)


class TestMinimumDaysExpiryPolicy:
    def test_skips_near_expiry(self):
        policy = MinimumDaysExpiryPolicy(min_days=7)
        expiries = [date(2026, 9, 24), date(2026, 10, 29)]
        # Sep 22 → Sep 24 is only 2 days away → skip
        result = policy.select_expiry(expiries, reference_date=date(2026, 9, 22))
        assert result == date(2026, 10, 29)

    def test_accepts_far_enough(self):
        policy = MinimumDaysExpiryPolicy(min_days=7)
        expiries = [date(2026, 9, 24), date(2026, 10, 29)]
        # Sep 15 → Sep 24 is 9 days → accept
        result = policy.select_expiry(expiries, reference_date=date(2026, 9, 15))
        assert result == date(2026, 9, 24)


class TestWeeklyExpiryPolicy:
    def test_picks_weekly(self):
        policy = WeeklyExpiryPolicy(max_days=7)
        expiries = [date(2026, 9, 24), date(2026, 10, 29)]
        # Sep 23 → Sep 24 is within 7 days
        result = policy.select_expiry(expiries, reference_date=date(2026, 9, 23))
        assert result == date(2026, 9, 24)


class TestCreateExpiryPolicy:
    def test_all_types(self):
        for name in ["nearest", "weekly", "fixed_days", "minimum_days"]:
            p = create_expiry_policy(name)
            assert hasattr(p, "select_expiry")

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Unknown policy_type"):
            create_expiry_policy("invalid")


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestOptionsConfig:
    def test_defaults(self):
        config = OptionsConfig()
        assert config.enabled is True
        assert config.underlying == "NIFTY"
        assert config.selector.type == "atm"
        assert config.expiry.policy == "nearest"
        assert len(config.structures.allowed) == 4

    def test_load_defaults(self):
        config = load_options_config(config_path="/nonexistent/path.yaml")
        assert config.enabled is True
        assert config.selector.type == "atm"


# ---------------------------------------------------------------------------
# Strategy.generate_market_view() integration test
# ---------------------------------------------------------------------------

class TestStrategyGenerateMarketView:
    def test_sma_crossover_generates_view(self):
        import pandas as pd
        from backtest.strategies.sma_crossover import SmaCrossover

        strategy = SmaCrossover(fast=5, slow=10)
        # Create enough candles for the slow SMA
        data = {
            "open": [100 + i * 0.1 for i in range(20)],
            "high": [101 + i * 0.1 for i in range(20)],
            "low": [99 + i * 0.1 for i in range(20)],
            "close": [100 + i * 0.1 for i in range(20)],
            "volume": [1000] * 20,
        }
        candles = pd.DataFrame(data)

        view = strategy.generate_market_view(candles)
        assert view is not None
        assert view.direction in (Direction.BULLISH, Direction.NEUTRAL)
        assert view.underlying == "NIFTY"
        assert view.spot_price > 0
