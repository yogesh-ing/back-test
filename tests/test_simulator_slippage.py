"""Tests for the slippage engine (Step 7).

Slippage is the largest hidden cost in forward testing, so these tests pin
down the direction (always adverse), the shape (square-root in participation),
and the hard rules (a limit order can never fill worse than its limit).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from backtest.simulator import (
    FixedBpsSlippage,
    HybridSlippage,
    LiquidityTier,
    MarketSnapshot,
    Order,
    OrderSide,
    SlippageCalculator,
    SlippageConfig,
    SpreadSlippage,
    ValidationError,
    VolatilitySlippage,
    VolumeImpactSlippage,
    ZeroSlippage,
    load_slippage_config,
    resolve_slippage_model,
)

D = Decimal
IST = ZoneInfo("Asia/Kolkata")

QUOTE = {"bid": 1499.5, "ask": 1500.5, "last": 1500, "avg_volume": 1_000_000, "atr": 22.5}


def buy(qty=1000, symbol="INFY") -> Order:
    o = Order.market(symbol, "buy", qty)
    o.submit()
    return o


def sell(qty=1000, symbol="INFY") -> Order:
    o = Order.market(symbol, "sell", qty)
    o.submit()
    return o


# ===========================================================================
# MarketSnapshot
# ===========================================================================


class TestMarketSnapshot:
    def test_from_full_quote(self):
        s = MarketSnapshot.from_market_data(QUOTE)
        assert s.mid == D("1500.00000000")
        assert s.spread == D("1.00000000")
        assert s.spread_bps == D("6.666667")

    def test_bare_price(self):
        s = MarketSnapshot.from_market_data(1500)
        assert s.last == D("1500.00000000")
        assert s.spread is None and s.spread_bps is None
        assert s.mid == D("1500.00000000")

    def test_close_alias(self):
        assert MarketSnapshot.from_market_data({"close": 250}).last == D("250.00000000")

    def test_avg_volume_aliases(self):
        for key in ("avg_volume", "average_volume", "adv"):
            assert MarketSnapshot.from_market_data({"last": 100, key: 5000}).avg_volume == D("5000")

    def test_bid_only_falls_back(self):
        assert MarketSnapshot.from_market_data({"bid": 99}).last == D("99.00000000")

    def test_crossed_quote_rejected(self):
        with pytest.raises(ValidationError, match="crossed quote"):
            MarketSnapshot.from_market_data({"bid": 101, "ask": 100})

    def test_empty_rejected(self):
        with pytest.raises(ValidationError, match="must contain"):
            MarketSnapshot.from_market_data({})

    def test_none_rejected(self):
        with pytest.raises(ValidationError, match="market data is required"):
            MarketSnapshot.from_market_data(None)

    def test_iso_timestamp_parsed(self):
        s = MarketSnapshot.from_market_data({"last": 100, "timestamp": "2026-01-05T09:20:00+05:30"})
        assert s.timestamp.hour == 9


# ===========================================================================
# Individual models
# ===========================================================================


class TestZeroSlippage:
    def test_costs_nothing(self):
        calc = SlippageCalculator(model=ZeroSlippage())
        est = calc.calculate_slippage(buy(), QUOTE)
        assert est.bps == D("0")
        assert est.executed_price == est.reference_price

    def test_disabled_helper(self):
        assert SlippageCalculator.disabled().calculate_slippage(buy(), QUOTE).bps == D("0")


class TestFixedBpsSlippage:
    def test_flat_regardless_of_conditions(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=10))
        small = calc.calculate_slippage(buy(1), QUOTE)
        large = calc.calculate_slippage(buy(500_000), QUOTE)
        assert small.bps == large.bps == D("10.000000")

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            FixedBpsSlippage(bps_value=-1)

    def test_shorthand_number(self):
        assert resolve_slippage_model(7).bps_value == D("7")


class TestSpreadSlippage:
    def test_half_spread_by_default(self):
        """Crossing from mid costs half the quoted spread."""
        calc = SlippageCalculator(model=SpreadSlippage())
        est = calc.calculate_slippage(buy(), QUOTE, reference_price=1500)
        assert est.bps == D("3.333334")  # half of 6.666667

    def test_full_spread(self):
        calc = SlippageCalculator(model=SpreadSlippage(spread_fraction=D("1")))
        assert calc.calculate_slippage(buy(), QUOTE).bps == D("6.666667")

    def test_wider_spread_costs_more(self):
        calc = SlippageCalculator(model=SpreadSlippage())
        tight = calc.calculate_slippage(buy(), {"bid": 999.9, "ask": 1000.1})
        wide = calc.calculate_slippage(buy(), {"bid": 995, "ask": 1005})
        assert wide.bps > tight.bps

    def test_fallback_when_no_quote(self):
        """Daily bars have no quotes; returning zero would look free."""
        calc = SlippageCalculator(model=SpreadSlippage(fallback_bps=D("8")))
        est = calc.calculate_slippage(buy(), {"close": 1500})
        assert est.bps == D("8.000000")

    def test_negative_fraction_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            SpreadSlippage(spread_fraction=-1)


class TestVolumeImpactSlippage:
    @pytest.fixture()
    def model(self):
        return VolumeImpactSlippage(coefficient_bps=D("100"))

    def test_square_root_law(self, model):
        """Quadrupling participation should double impact, not quadruple it."""
        one = model.impact_bps(10_000, 1_000_000)  # 1%
        four = model.impact_bps(40_000, 1_000_000)  # 4%
        assert float(four) == pytest.approx(float(one) * 2, rel=1e-6)

    def test_known_values(self, model):
        assert model.impact_bps(10_000, 1_000_000) == D("10.000000")  # 1%
        assert model.impact_bps(250_000, 1_000_000) == D("50.000000")  # 25%
        assert model.impact_bps(1_000_000, 1_000_000) == D("100.000000")

    def test_participation_is_capped(self, model):
        """Ordering 5x the daily volume cannot cost 5x the full-day impact."""
        assert model.impact_bps(5_000_000, 1_000_000) == D("100.000000")

    def test_zero_without_volume_data(self, model):
        assert model.impact_bps(1000, 0) == D("0")
        calc = SlippageCalculator(model=model)
        assert calc.calculate_slippage(buy(), {"last": 1500}).bps == D("0")

    def test_bigger_orders_cost_more(self, model):
        calc = SlippageCalculator(model=model)
        small = calc.calculate_slippage(buy(1_000), QUOTE)
        big = calc.calculate_slippage(buy(100_000), QUOTE)
        assert big.bps > small.bps

    def test_invalid_config(self):
        with pytest.raises(ValidationError):
            VolumeImpactSlippage(coefficient_bps=-1)
        with pytest.raises(ValidationError):
            VolumeImpactSlippage(max_participation=0)


class TestVolatilitySlippage:
    def test_scales_with_atr(self):
        model = VolatilitySlippage(atr_fraction=D("0.1"), size_scaling=False)
        # ATR 15 on price 1500 = 1% ATR; 10% of that = 10 bps.
        assert model.volatility_bps(15, 1500) == D("10.000000")
        assert model.volatility_bps(30, 1500) == D("20.000000")

    def test_zero_without_atr(self):
        calc = SlippageCalculator(model=VolatilitySlippage())
        assert calc.calculate_slippage(buy(), {"last": 1500}).bps == D("0")

    def test_size_scaling_increases_cost(self):
        model = VolatilitySlippage(atr_fraction=D("0.1"), size_scaling=True)
        base = model.volatility_bps(15, 1500)
        scaled = model.volatility_bps(15, 1500, quantity=1_000_000, avg_volume=1_000_000)
        assert scaled == base * 2  # (1 + sqrt(1.0))

    def test_size_scaling_can_be_disabled(self):
        model = VolatilitySlippage(atr_fraction=D("0.1"), size_scaling=False)
        assert model.volatility_bps(15, 1500, 1_000_000, 1_000_000) == D("10.000000")

    def test_negative_fraction_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            VolatilitySlippage(atr_fraction=-1)


class TestHybridSlippage:
    def test_components_add_up(self):
        calc = SlippageCalculator(model=HybridSlippage())
        est = calc.calculate_slippage(buy(10_000), QUOTE)
        parts = {k: v for k, v in est.components.items() if k in ("spread", "impact", "volatility")}
        assert set(parts) == {"spread", "impact", "volatility"}
        assert float(sum(parts.values())) == pytest.approx(float(est.bps), rel=1e-6)

    def test_floor_prevents_free_execution(self):
        model = HybridSlippage(spread=SpreadSlippage(fallback_bps=D("0")), floor_bps=D("3"))
        calc = SlippageCalculator(model=model)
        assert calc.calculate_slippage(buy(), {"last": 1500}).bps == D("3.000000")

    def test_cap_limits_extremes(self):
        model = HybridSlippage(
            volume=VolumeImpactSlippage(coefficient_bps=D("100000")),
            cap_bps=D("50"),
        )
        calc = SlippageCalculator(model=model)
        assert calc.calculate_slippage(buy(500_000), QUOTE).bps == D("50.000000")

    def test_weights_disable_components(self):
        model = HybridSlippage(volatility_weight=D("0"), volume_weight=D("0"))
        calc = SlippageCalculator(model=model)
        est = calc.calculate_slippage(buy(), QUOTE)
        assert "volatility" not in est.components
        assert est.bps == D("3.333334")

    def test_cap_below_floor_rejected(self):
        with pytest.raises(ValidationError, match="cap_bps must be >= floor_bps"):
            HybridSlippage(floor_bps=D("10"), cap_bps=D("5"))


# ===========================================================================
# Direction — the rule everything else depends on
# ===========================================================================


class TestDirection:
    def test_buy_executes_above_reference(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=100))
        est = calc.calculate_slippage(buy(), QUOTE, reference_price=1000)
        assert est.executed_price == D("1010.00000000")

    def test_sell_executes_below_reference(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=100))
        est = calc.calculate_slippage(sell(), QUOTE, reference_price=1000)
        assert est.executed_price == D("990.00000000")

    def test_slippage_is_always_adverse(self):
        """Positive bps must mean 'worse' for both sides."""
        calc = SlippageCalculator(model=HybridSlippage())
        b = calc.calculate_slippage(buy(), QUOTE)
        s = calc.calculate_slippage(sell(), QUOTE)
        assert b.executed_price > b.reference_price
        assert s.executed_price < s.reference_price
        assert b.bps > 0 and s.bps > 0

    def test_reference_defaults_to_the_side_you_cross(self):
        calc = SlippageCalculator(model=ZeroSlippage())
        assert calc.calculate_slippage(buy(), QUOTE).reference_price == D("1500.50000000")
        assert calc.calculate_slippage(sell(), QUOTE).reference_price == D("1499.50000000")

    def test_amount_and_per_share(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=100))
        est = calc.calculate_slippage(buy(200), QUOTE, reference_price=1000)
        assert est.per_share == D("10.00000000")
        assert est.amount == D("2000.0000")

    def test_matches_the_fill_convention(self):
        """Fill.slippage_bps must agree with what the calculator reported."""
        from backtest.simulator import Fill

        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=50))
        for side in ("buy", "sell"):
            order = buy() if side == "buy" else sell()
            est = calc.calculate_slippage(order, QUOTE, reference_price=1000)
            fill = Fill(
                symbol="INFY",
                side=side,
                quantity=est.quantity,
                fill_price=est.executed_price,
                reference_price=est.reference_price,
            )
            assert float(fill.slippage_bps) == pytest.approx(float(est.bps), rel=1e-6)
            assert fill.slippage_bps > 0  # adverse for both sides


# ===========================================================================
# Order-type handling
# ===========================================================================


class TestOrderTypes:
    def test_limit_buy_cannot_fill_above_its_limit(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=500))
        order = Order.limit("INFY", "buy", 1000, 1500.20)
        order.submit()
        est = calc.calculate_slippage(order, QUOTE)
        assert est.capped
        assert est.executed_price == D("1500.20000000")

    def test_limit_sell_cannot_fill_below_its_limit(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=500))
        order = Order.limit("INFY", "sell", 1000, 1499.40)
        order.submit()
        est = calc.calculate_slippage(order, QUOTE)
        assert est.capped
        assert est.executed_price == D("1499.40000000")

    def test_capped_bps_is_recomputed(self):
        """Reported bps must describe the price actually used."""
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=500))
        order = Order.limit("INFY", "buy", 1000, 1500.20)
        order.submit()
        est = calc.calculate_slippage(order, QUOTE)
        expected = abs(est.executed_price - est.reference_price) / est.reference_price * 10000
        assert float(est.bps) == pytest.approx(float(expected), rel=1e-6)

    def test_generous_limit_is_not_capped(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=5))
        order = Order.limit("INFY", "buy", 1000, 1600)
        order.submit()
        assert not calc.calculate_slippage(order, QUOTE).capped

    def test_market_orders_take_full_slippage(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=500))
        assert not calc.calculate_slippage(buy(), QUOTE).capped

    def test_stop_orders_take_full_slippage(self):
        """A triggered stop becomes a market order — that is the cost of one."""
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=200))
        order = Order.stop("INFY", "sell", 1000, stop_price=1490)
        order.submit()
        assert not calc.calculate_slippage(order, QUOTE).capped


# ===========================================================================
# Time of day, tiers, overrides
# ===========================================================================


class TestTimeOfDay:
    @pytest.fixture()
    def calc(self):
        return SlippageCalculator(SlippageConfig(model=FixedBpsSlippage(bps_value=10)))

    @pytest.mark.parametrize(
        "hh, mm, expected",
        [
            (9, 15, "2.0"),  # open
            (9, 44, "2.0"),  # still inside the open window
            (9, 46, "1"),  # midday
            (12, 0, "1"),
            (15, 1, "1.5"),  # close window
            (15, 30, "1.5"),
            (18, 0, "2.0"),  # after hours: illiquid by definition
            (6, 0, "2.0"),  # pre-open
        ],
    )
    def test_session_multipliers(self, calc, hh, mm, expected):
        ts = datetime(2026, 1, 5, hh, mm, tzinfo=IST)
        assert calc.time_multiplier(ts) == D(expected)

    def test_no_timestamp_means_no_adjustment(self, calc):
        assert calc.time_multiplier(None) == D("1")

    def test_utc_is_converted_to_session_time(self, calc):
        """03:45 UTC is 09:15 IST — the open."""
        ts = datetime(2026, 1, 5, 3, 45, tzinfo=timezone.utc)
        assert calc.time_multiplier(ts) == D("2.0")

    def test_multiplier_applied_to_the_estimate(self, calc):
        quote = dict(QUOTE, timestamp=datetime(2026, 1, 5, 9, 20, tzinfo=IST))
        assert calc.calculate_slippage(buy(), quote).bps == D("20.000000")

    def test_multiplier_recorded_in_components(self, calc):
        quote = dict(QUOTE, timestamp=datetime(2026, 1, 5, 9, 20, tzinfo=IST))
        est = calc.calculate_slippage(buy(), quote)
        assert est.components["time_of_day"] == D("2.0")

    def test_effective_spread_helper(self, calc):
        base = calc.get_effective_spread(999.5, 1000.5)
        at_open = calc.get_effective_spread(999.5, 1000.5, datetime(2026, 1, 5, 9, 20, tzinfo=IST))
        assert at_open == base * 2

    def test_effective_spread_rejects_crossed_quote(self, calc):
        with pytest.raises(ValidationError, match="crossed quote"):
            calc.get_effective_spread(101, 100)


class TestLiquidityTiers:
    @pytest.fixture()
    def calc(self):
        return SlippageCalculator(
            SlippageConfig(
                model=FixedBpsSlippage(bps_value=10),
                symbol_tiers={"INFY": LiquidityTier.LARGE_CAP, "SMALLCO": LiquidityTier.SMALL_CAP},
            )
        )

    def test_tier_scales_cost(self, calc):
        large = calc.calculate_slippage(buy(symbol="INFY"), QUOTE)
        small = calc.calculate_slippage(buy(symbol="SMALLCO"), QUOTE)
        assert large.bps == D("10.000000")
        assert small.bps == D("25.000000")  # 2.5x

    def test_unknown_symbol_uses_default_tier(self, calc):
        assert calc.config.tier_for("NEVERHEARDOF") == LiquidityTier.LARGE_CAP

    def test_symbol_lookup_is_case_insensitive(self, calc):
        assert calc.config.tier_for("smallco") == LiquidityTier.SMALL_CAP

    def test_symbol_override_applies_last(self):
        calc = SlippageCalculator(
            SlippageConfig(
                model=FixedBpsSlippage(bps_value=10),
                symbol_overrides={"WEIRDCO": D("3")},
            )
        )
        assert calc.calculate_slippage(buy(symbol="WEIRDCO"), QUOTE).bps == D("30.000000")

    def test_unknown_tier_in_mapping_rejected(self):
        with pytest.raises(ValidationError, match="unknown tier"):
            SlippageConfig(symbol_tiers={"X": "platinum"})

    def test_max_bps_is_a_hard_ceiling(self):
        calc = SlippageCalculator(
            SlippageConfig(model=FixedBpsSlippage(bps_value=100_000), max_bps=D("250"))
        )
        assert calc.calculate_slippage(buy(), QUOTE).bps == D("250.000000")


# ===========================================================================
# Configuration
# ===========================================================================


class TestConfiguration:
    def test_ships_a_loadable_default_file(self):
        assert load_slippage_config().model.name == "hybrid"

    @pytest.mark.parametrize(
        "profile, model_name",
        [
            ("backtest", "zero"),
            ("simple", "fixed"),
            ("realistic", "hybrid"),
            ("pessimistic", "hybrid"),
            ("optimistic", "spread"),
        ],
    )
    def test_every_shipped_profile_loads(self, profile, model_name):
        assert load_slippage_config(profile=profile).model.name == model_name

    def test_profiles_are_ordered_by_cost(self):
        """backtest < optimistic < realistic < pessimistic on the same order."""
        results = {}
        for profile in ("backtest", "optimistic", "realistic", "pessimistic"):
            calc = SlippageCalculator.from_config(profile=profile)
            results[profile] = calc.calculate_slippage(buy(10_000), QUOTE).bps
        assert (
            results["backtest"]
            < results["optimistic"]
            < results["realistic"]
            < results["pessimistic"]
        )

    def test_unknown_profile_lists_options(self):
        with pytest.raises(ValidationError, match="unknown slippage profile"):
            load_slippage_config(profile="wishful")

    def test_missing_explicit_file_is_an_error(self):
        with pytest.raises(ValidationError, match="not found"):
            load_slippage_config(path="/nonexistent/slippage.yaml")

    def test_unknown_keys_rejected(self, tmp_path):
        bad = tmp_path / "s.yaml"
        bad.write_text("default:\n  nonsense: 1\n")
        with pytest.raises(ValidationError, match="unknown slippage config keys"):
            load_slippage_config(path=str(bad))

    def test_malformed_yaml_names_the_file(self, tmp_path):
        bad = tmp_path / "s.yaml"
        bad.write_text("default:\n  a: b: c\n")
        with pytest.raises(ValidationError, match="could not parse"):
            load_slippage_config(path=str(bad))

    def test_session_times_parsed(self, tmp_path):
        cfg = tmp_path / "s.yaml"
        cfg.write_text('default:\n  session_open: "10:00"\n  session_close: "16:00"\n')
        loaded = load_slippage_config(path=str(cfg))
        assert loaded.session_open.hour == 10 and loaded.session_close.hour == 16

    def test_bad_time_format_rejected(self, tmp_path):
        cfg = tmp_path / "s.yaml"
        cfg.write_text('default:\n  session_open: "morning"\n')
        with pytest.raises(ValidationError, match="HH:MM"):
            load_slippage_config(path=str(cfg))


class TestResolveModel:
    def test_passthrough_and_none(self):
        model = FixedBpsSlippage()
        assert resolve_slippage_model(model) is model
        assert isinstance(resolve_slippage_model(None), ZeroSlippage)

    def test_by_name(self):
        assert isinstance(resolve_slippage_model("hybrid"), HybridSlippage)
        assert isinstance(resolve_slippage_model("volume_based"), VolumeImpactSlippage)

    def test_from_dict(self):
        model = resolve_slippage_model({"model": "fixed", "bps": 12})
        assert model.bps_value == D("12")

    def test_nested_hybrid_from_dict(self):
        model = resolve_slippage_model({"model": "hybrid", "spread": {"spread_fraction": 0.25}})
        assert model.spread.spread_fraction == D("0.25")

    def test_unknown_name_lists_options(self):
        with pytest.raises(ValidationError, match="expected one of"):
            resolve_slippage_model("astrology")

    def test_bad_kwargs_reported(self):
        with pytest.raises(ValidationError, match="bad configuration"):
            resolve_slippage_model({"model": "fixed", "nonsense": 1})


# ===========================================================================
# Calculator plumbing and statistics
# ===========================================================================


class TestCalculator:
    def test_explicit_parameters_without_an_order(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=10))
        est = calc.calculate_slippage(market_data=QUOTE, symbol="INFY", side="buy", quantity=100)
        assert est.quantity == D("100.00000000")

    def test_missing_context_rejected(self):
        calc = SlippageCalculator(model=FixedBpsSlippage())
        with pytest.raises(ValidationError, match="explicit side and quantity"):
            calc.calculate_slippage(market_data=QUOTE)

    def test_model_can_be_overridden_per_call(self):
        calc = SlippageCalculator(model=ZeroSlippage())
        assert calc.calculate_slippage(buy(), QUOTE, model_type="fixed").bps == D("5.000000")
        assert calc.calculate_slippage(buy(), QUOTE).bps == D("0")

    def test_apply_returns_the_price(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=100))
        assert calc.apply(buy(), QUOTE, reference_price=1000) == D("1010.00000000")

    def test_market_impact_helper(self):
        calc = SlippageCalculator.from_config(profile="realistic")
        assert calc.estimate_market_impact(10_000, 1_000_000) == D("10.000000")

    def test_volatility_helper(self):
        calc = SlippageCalculator(model=VolatilitySlippage(size_scaling=False))
        assert calc.calculate_volatility_adjustment(15, price=1500) == D("10.000000")


class TestStatistics:
    def test_empty(self):
        assert SlippageCalculator(model=ZeroSlippage()).statistics() == {"count": 0}

    def test_aggregates(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=10))
        for _ in range(5):
            calc.calculate_slippage(buy(100), QUOTE, reference_price=1000)
        stats = calc.statistics()
        assert stats["count"] == 5
        assert stats["mean_bps"] == 10.0
        assert stats["median_bps"] == 10.0
        assert stats["total_amount"] == D("500.0000")  # 5 x 100 x 1.00

    def test_per_symbol_breakdown(self):
        calc = SlippageCalculator(
            SlippageConfig(
                model=FixedBpsSlippage(bps_value=10),
                symbol_tiers={"SMALLCO": LiquidityTier.SMALL_CAP},
            )
        )
        calc.calculate_slippage(buy(symbol="INFY"), QUOTE)
        calc.calculate_slippage(buy(symbol="SMALLCO"), QUOTE)
        calc.calculate_slippage(buy(symbol="SMALLCO"), QUOTE)
        by_symbol = calc.statistics()["by_symbol"]
        assert by_symbol["INFY"]["count"] == 1
        assert by_symbol["SMALLCO"]["count"] == 2
        assert by_symbol["SMALLCO"]["mean_bps"] > by_symbol["INFY"]["mean_bps"]

    def test_capped_orders_counted(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(bps_value=500))
        order = Order.limit("INFY", "buy", 1000, 1500.20)
        order.submit()
        calc.calculate_slippage(order, QUOTE)
        calc.calculate_slippage(buy(), QUOTE)
        assert calc.statistics()["capped_count"] == 1

    def test_percentiles(self):
        calc = SlippageCalculator(model=ZeroSlippage())
        for bps in range(1, 101):
            calc.calculate_slippage(buy(), QUOTE, model_type=Decimal(bps))
        stats = calc.statistics()
        assert stats["min_bps"] == 1.0 and stats["max_bps"] == 100.0
        assert 94 <= stats["p95_bps"] <= 96

    def test_recording_can_be_disabled(self):
        calc = SlippageCalculator(model=FixedBpsSlippage(), record=False)
        calc.calculate_slippage(buy(), QUOTE)
        assert calc.statistics() == {"count": 0}

    def test_reset(self):
        calc = SlippageCalculator(model=FixedBpsSlippage())
        calc.calculate_slippage(buy(), QUOTE)
        calc.reset()
        assert calc.statistics() == {"count": 0}


# ===========================================================================
# Serialisation
# ===========================================================================


def test_estimate_to_dict_is_json_safe():
    import json

    calc = SlippageCalculator.from_config(profile="realistic")
    payload = calc.calculate_slippage(buy(10_000), QUOTE).to_dict()
    assert json.loads(json.dumps(payload))["model"] == "hybrid"


def test_models_round_trip_through_dict():
    for model in (
        FixedBpsSlippage(bps_value=7),
        SpreadSlippage(spread_fraction=D("0.75")),
        VolumeImpactSlippage(coefficient_bps=D("150")),
        VolatilitySlippage(atr_fraction=D("0.2")),
        HybridSlippage(),
    ):
        rebuilt = resolve_slippage_model(model.to_dict())
        assert rebuilt.name == model.name
