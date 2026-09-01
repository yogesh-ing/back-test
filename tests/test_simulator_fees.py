"""Tests for the complete fee stack and broker presets (Step 8).

The Indian figures here are checked against published brokerage-calculator
values: a ₹1,00,000 delivery buy costs ~₹118.74 and the round trip ~₹237.82
at a zero-brokerage broker. If those numbers drift, either a rate changed
(they do — see the note in ``config/brokers.yaml``) or something broke.

The delivery-vs-intraday STT asymmetry gets its own tests because getting the
segment wrong misprices a round trip by roughly 8x.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest

from backtest.simulator import (
    BROKER_PRESETS,
    BrokerProfile,
    CommissionCalculator,
    CurrencyConverter,
    FeeBreakdown,
    FlatCommission,
    IndiaEquityFees,
    NoStatutoryFees,
    Order,
    PaymentForOrderFlowCommission,
    PercentageCommission,
    TieredCommission,
    TradeSegment,
    USEquityFees,
    ValidationError,
    ZeroCommission,
    get_broker_preset,
    load_broker_profile,
    resolve_fee_schedule,
)

D = Decimal
LAKH = dict(quantity=100, fill_price=1000)  # ₹1,00,000 notional


# ===========================================================================
# TradeSegment
# ===========================================================================


class TestTradeSegment:
    def test_validates_and_normalises(self):
        assert TradeSegment.validate("EQUITY_DELIVERY") == "equity_delivery"

    def test_unknown_rejected(self):
        with pytest.raises(ValidationError, match="unknown trade segment"):
            TradeSegment.validate("crypto")


# ===========================================================================
# India fee schedule — the numbers that matter
# ===========================================================================


class TestIndiaDeliveryBuy:
    @pytest.fixture()
    def fees(self):
        calc = CommissionCalculator.for_broker("india_zero")
        return calc.calculate(**LAKH, side="buy", segment=TradeSegment.EQUITY_DELIVERY)

    def test_stt_both_sides_on_delivery(self, fees):
        assert fees.get("stt") == D("100.00")  # 0.1% of 1,00,000

    def test_exchange_transaction_charge(self, fees):
        assert fees.get("exchange_transaction") == D("2.97")

    def test_sebi_and_ipft(self, fees):
        assert fees.get("sebi_turnover") == D("0.10")  # ₹10 per crore
        assert fees.get("ipft") == D("0.10")

    def test_stamp_duty_charged_on_buy(self, fees):
        assert fees.get("stamp_duty") == D("15.00")  # 0.015%

    def test_gst_excludes_stt_and_stamp_duty(self, fees):
        """GST applies to brokerage + exchange + SEBI + IPFT only."""
        taxable = D("0") + D("2.97") + D("0.10") + D("0.10")
        assert fees.get("gst") == (taxable * D("0.18")).quantize(D("0.01"))

    def test_total_matches_published_calculators(self, fees):
        assert fees.total == D("118.74")

    def test_no_dp_charge_on_a_buy(self, fees):
        assert fees.get("dp_charges") == D("0")


class TestIndiaDeliverySell:
    @pytest.fixture()
    def fees(self):
        calc = CommissionCalculator.for_broker("india_zero")
        return calc.calculate(**LAKH, side="sell", segment=TradeSegment.EQUITY_DELIVERY)

    def test_stt_still_charged(self, fees):
        assert fees.get("stt") == D("100.00")

    def test_no_stamp_duty_on_a_sell(self, fees):
        assert fees.get("stamp_duty") == D("0")

    def test_dp_charge_applies(self, fees):
        assert fees.get("dp_charges") == D("15.34")

    def test_total(self, fees):
        assert fees.total == D("119.08")


class TestDeliveryVsIntraday:
    """The asymmetry that misprices a round trip by ~8x if you get it wrong."""

    def _round_trip(self, broker, segment):
        calc = CommissionCalculator.for_broker(broker)
        buy = calc.calculate(**LAKH, side="buy", segment=segment)
        sell = calc.calculate(**LAKH, side="sell", segment=segment)
        return buy, sell

    def test_delivery_round_trip(self):
        buy, sell = self._round_trip("india_zero", TradeSegment.EQUITY_DELIVERY)
        assert buy.total + sell.total == D("237.82")

    def test_delivery_pays_stt_twice(self):
        buy, sell = self._round_trip("india_zero", TradeSegment.EQUITY_DELIVERY)
        assert buy.get("stt") + sell.get("stt") == D("200.00")

    def test_intraday_pays_stt_once_at_a_quarter_rate(self):
        buy, sell = self._round_trip("india_zero", TradeSegment.EQUITY_INTRADAY)
        assert buy.get("stt") == D("0")
        assert sell.get("stt") == D("25.00")
        assert buy.get("stt") + sell.get("stt") == D("25.00")

    def test_intraday_stt_is_one_eighth_of_delivery(self):
        d_buy, d_sell = self._round_trip("india_zero", TradeSegment.EQUITY_DELIVERY)
        i_buy, i_sell = self._round_trip("india_zero", TradeSegment.EQUITY_INTRADAY)
        delivery = d_buy.get("stt") + d_sell.get("stt")
        intraday = i_buy.get("stt") + i_sell.get("stt")
        assert delivery / intraday == 8

    def test_intraday_stamp_duty_is_lower(self):
        buy, _ = self._round_trip("india_zero", TradeSegment.EQUITY_INTRADAY)
        assert buy.get("stamp_duty") == D("3.00")  # 0.003%

    def test_no_dp_charge_on_intraday(self):
        _, sell = self._round_trip("india_zero", TradeSegment.EQUITY_INTRADAY)
        assert sell.get("dp_charges") == D("0")

    def test_zerodha_intraday_round_trip(self):
        buy, sell = self._round_trip("zerodha", TradeSegment.EQUITY_INTRADAY)
        # brokerage capped at ₹20 per side
        assert buy.brokerage == sell.brokerage == D("20.00")
        assert buy.total + sell.total == D("82.68")

    def test_zerodha_delivery_brokerage_is_free(self):
        buy, _ = self._round_trip("zerodha", TradeSegment.EQUITY_DELIVERY)
        assert buy.brokerage == D("0.00")


class TestFuturesAndOptions:
    def test_futures_stt_sell_only(self):
        calc = CommissionCalculator.for_broker("india_zero")
        buy = calc.calculate(**LAKH, side="buy", segment=TradeSegment.FUTURES)
        sell = calc.calculate(**LAKH, side="sell", segment=TradeSegment.FUTURES)
        assert buy.get("stt") == D("0")
        assert sell.get("stt") == D("20.00")  # 0.02%

    def test_options_have_the_highest_exchange_charge(self):
        calc = CommissionCalculator.for_broker("india_zero")
        equity = calc.calculate(**LAKH, side="buy", segment=TradeSegment.EQUITY_DELIVERY)
        options = calc.calculate(**LAKH, side="buy", segment=TradeSegment.OPTIONS)
        assert options.get("exchange_transaction") > equity.get("exchange_transaction")


class TestIndiaFeeConfig:
    def test_rates_can_be_overridden(self):
        schedule = IndiaEquityFees(stt_delivery=D("0.002"), gst_rate=D("0"))
        calc = CommissionCalculator(
            BrokerProfile(name="custom", fee_schedule=schedule, currency="INR")
        )
        fees = calc.calculate(**LAKH, side="buy")
        assert fees.get("stt") == D("200.00")
        assert fees.get("gst") == D("0.00")

    def test_dp_charges_can_be_disabled(self):
        schedule = IndiaEquityFees(dp_charges=ZeroCommission and D("0"))
        calc = CommissionCalculator(
            BrokerProfile(name="custom", fee_schedule=schedule, currency="INR")
        )
        fees = calc.calculate(**LAKH, side="sell", segment=TradeSegment.EQUITY_DELIVERY)
        assert fees.get("dp_charges") == D("0")

    def test_negative_rate_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            IndiaEquityFees(stt_delivery=D("-0.001"))


# ===========================================================================
# US fee schedule
# ===========================================================================


class TestUSEquityFees:
    @pytest.fixture()
    def calc(self):
        return CommissionCalculator.for_broker("ibkr")

    def test_no_statutory_fees_on_a_buy(self, calc):
        fees = calc.calculate(quantity=100, fill_price=50, side="buy")
        assert fees.get("sec_fee") == D("0") and fees.get("finra_taf") == D("0")

    def test_sec_fee_on_a_sell(self, calc):
        fees = calc.calculate(quantity=100, fill_price=50, side="sell")
        assert fees.get("sec_fee") == D("0.14")  # 0.0000278 x 5000

    def test_finra_taf_per_share(self, calc):
        fees = calc.calculate(quantity=100, fill_price=50, side="sell")
        assert fees.get("finra_taf") == D("0.02")  # 0.000166 x 100

    def test_finra_taf_is_capped(self):
        calc = CommissionCalculator.for_broker("robinhood")
        fees = calc.calculate(quantity=1_000_000, fill_price=50, side="sell")
        assert fees.get("finra_taf") == D("8.30")

    def test_ibkr_per_share_minimum(self, calc):
        """0.005 x 100 = $0.50, floored at the $1 minimum."""
        assert calc.calculate(quantity=100, fill_price=50, side="buy").brokerage == D("1.00")

    def test_ibkr_large_order_scales(self, calc):
        assert calc.calculate(quantity=10_000, fill_price=50, side="buy").brokerage == D("50.00")

    def test_ecn_fee_optional(self):
        calc = CommissionCalculator(
            BrokerProfile(
                name="ecn",
                fee_schedule=USEquityFees(ecn_fee_per_share=D("0.003")),
                currency="USD",
            )
        )
        assert calc.calculate(quantity=100, fill_price=50, side="buy").get("ecn_fee") == D("0.30")


# ===========================================================================
# Broker presets
# ===========================================================================


class TestBrokerPresets:
    @pytest.mark.parametrize("name", sorted(BROKER_PRESETS))
    def test_every_preset_instantiates_and_prices(self, name):
        calc = CommissionCalculator.for_broker(name)
        fees = calc.calculate(quantity=100, fill_price=1000, side="buy")
        assert fees.total >= D("0")
        assert fees.broker == name

    def test_unknown_preset_lists_options(self):
        with pytest.raises(ValidationError, match="unknown broker preset"):
            get_broker_preset("bank_of_nowhere")

    def test_full_service_costs_far_more_than_discount(self):
        discount = CommissionCalculator.for_broker("zerodha").calculate(
            **LAKH, side="buy", segment=TradeSegment.EQUITY_INTRADAY
        )
        full = CommissionCalculator.for_broker("india_full_service").calculate(
            **LAKH, side="buy", segment=TradeSegment.EQUITY_INTRADAY
        )
        assert full.brokerage > discount.brokerage * 10

    def test_minimum_commission_applies(self):
        calc = CommissionCalculator.for_broker("india_full_service")
        fees = calc.calculate(quantity=1, fill_price=100, side="buy")
        assert fees.brokerage == D("25.00")

    def test_free_trading_is_not_actually_free(self):
        """Zero brokerage still costs ~12 bps on an Indian delivery buy."""
        fees = CommissionCalculator.for_broker("india_zero").calculate(**LAKH, side="buy")
        assert fees.brokerage == D("0.00")
        assert fees.total > D("100")
        assert fees.effective_bps(100_000) > D("11")

    def test_zero_preset_really_is_free(self):
        assert CommissionCalculator.for_broker("zero").calculate(**LAKH, side="buy").total == D(
            "0.00"
        )

    def test_delivery_override_only_affects_delivery(self):
        calc = CommissionCalculator.for_broker("zerodha")
        delivery = calc.calculate(**LAKH, side="buy", segment=TradeSegment.EQUITY_DELIVERY)
        intraday = calc.calculate(**LAKH, side="buy", segment=TradeSegment.EQUITY_INTRADAY)
        assert delivery.brokerage == D("0.00")
        assert intraday.brokerage == D("20.00")


class TestPaymentForOrderFlow:
    def test_commission_really_is_zero(self):
        assert PaymentForOrderFlowCommission().calculate(100, 50) == D("0")

    def test_hidden_cost_is_exposed(self):
        """'Free' is funded by worse fills; the model says how much."""
        model = PaymentForOrderFlowCommission(implied_slippage_bps=D("2.5"))
        assert model.hidden_cost(100, 50) == D("1.2500")  # 2.5bps of $5,000

    def test_robinhood_uses_it(self):
        assert CommissionCalculator.for_broker("robinhood").broker.commission_model.name == "pfof"

    def test_negative_implied_slippage_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            PaymentForOrderFlowCommission(implied_slippage_bps=D("-1"))


# ===========================================================================
# Calculator API
# ===========================================================================


class TestCalculatorAPI:
    def test_from_an_order(self):
        order = Order.market("INFY", "buy", 100)
        order.submit()
        order.add_fill(quantity=100, fill_price=1000)
        fees = CommissionCalculator.for_broker("zerodha").calculate(order)
        assert fees.brokerage == D("0.00")  # delivery default
        assert fees.total == D("118.74")

    def test_missing_context_rejected(self):
        with pytest.raises(ValidationError, match="quantity, fill_price and side"):
            CommissionCalculator.for_broker("zerodha").calculate()

    def test_non_positive_price_rejected(self):
        with pytest.raises(ValidationError, match="must be positive"):
            CommissionCalculator.for_broker("zerodha").calculate(
                quantity=1, fill_price=0, side="buy"
            )

    def test_calculate_commission_helper(self):
        calc = CommissionCalculator.for_broker("zerodha")
        assert calc.calculate_commission(
            quantity=100,
            fill_price=1000,
            side="buy",
            segment=TradeSegment.EQUITY_INTRADAY,
        ) == D("20.00")

    def test_calculate_regulatory_fees_helper(self):
        calc = CommissionCalculator.for_broker("india_zero")
        value = calc.calculate_regulatory_fees(
            100_000, side="buy", segment=TradeSegment.EQUITY_DELIVERY
        )
        # stt 100.00 + sebi 0.10 + stamp_duty 15.00 + gst 0.57
        assert value == D("115.67")

    def test_calculate_exchange_fees_helper(self):
        calc = CommissionCalculator.for_broker("india_zero")
        value = calc.calculate_exchange_fees(
            quantity=100,
            trade_value=100_000,
            side="buy",
            segment=TradeSegment.EQUITY_DELIVERY,
        )
        assert value == D("3.07")  # exchange txn + ipft

    def test_switch_broker_keeps_history(self):
        calc = CommissionCalculator.for_broker("zerodha")
        calc.calculate(**LAKH, side="buy")
        calc.switch_broker("ibkr")
        assert calc.broker.name == "ibkr"
        assert len(calc.history) == 1

    def test_switch_broker_changes_pricing(self):
        calc = CommissionCalculator.for_broker("zero")
        before = calc.calculate(**LAKH, side="buy").total
        calc.switch_broker("india_zero")
        after = calc.calculate(**LAKH, side="buy").total
        assert before == D("0.00") and after > D("100")


class TestMonthlyVolumeTracking:
    def test_volume_accumulates(self):
        calc = CommissionCalculator.for_broker("zerodha")
        when = datetime(2026, 3, 10)
        calc.record_volume(100_000, when)
        calc.record_volume(50_000, when)
        assert calc.monthly_volume(when) == D("150000")

    def test_volume_is_per_month(self):
        calc = CommissionCalculator.for_broker("zerodha")
        calc.record_volume(100_000, datetime(2026, 3, 10))
        calc.record_volume(50_000, datetime(2026, 4, 10))
        assert calc.monthly_volume(datetime(2026, 3, 1)) == D("100000")
        assert calc.monthly_volume(datetime(2026, 4, 1)) == D("50000")

    def test_calculate_tracks_volume_automatically(self):
        calc = CommissionCalculator.for_broker("zerodha")
        when = datetime(2026, 3, 10)
        calc.calculate(**LAKH, side="buy", when=when)
        assert calc.monthly_volume(when) == D("100000")

    def test_tracking_can_be_disabled(self):
        calc = CommissionCalculator.for_broker("zerodha")
        when = datetime(2026, 3, 10)
        calc.calculate(**LAKH, side="buy", when=when, track_volume=False)
        assert calc.monthly_volume(when) == D("0")

    def test_tiered_pricing_uses_monthly_volume(self):
        """A high-volume month should reach a cheaper tier."""
        profile = BrokerProfile(
            name="tiered",
            commission_model=TieredCommission(
                [(0, D("0.001")), (10_000_000, D("0.0001"))], maximum=None
            ),
            fee_schedule=NoStatutoryFees(),
            default_segment=TradeSegment.EQUITY_INTRADAY,
        )
        when = datetime(2026, 3, 10)

        low = CommissionCalculator(profile)
        first = low.calculate(**LAKH, side="buy", when=when).brokerage

        high = CommissionCalculator(profile)
        high.record_volume(20_000_000, when)
        later = high.calculate(**LAKH, side="buy", when=when).brokerage

        assert first == D("100.00")  # 0.1%
        assert later == D("10.00")  # 0.01% — cheaper tier
        assert later < first

    def test_reset_clears_volume_and_history(self):
        calc = CommissionCalculator.for_broker("zerodha")
        calc.calculate(**LAKH, side="buy")
        calc.reset()
        assert calc.get_total_fees() == D("0.00")
        assert calc.monthly_volume() == D("0")


# ===========================================================================
# FeeBreakdown
# ===========================================================================


class TestFeeBreakdown:
    @pytest.fixture()
    def fees(self):
        return CommissionCalculator.for_broker("zerodha").calculate(
            **LAKH, side="sell", segment=TradeSegment.EQUITY_INTRADAY
        )

    def test_buckets_sum_to_the_total(self, fees):
        assert fees.brokerage + fees.exchange_fees + fees.regulatory_fees == fees.total

    def test_gst_counts_as_regulatory(self, fees):
        """GST is a tax remitted to the government, not broker revenue."""
        assert fees.get("gst") > D("0")
        assert fees.get("gst") <= fees.regulatory_fees

    def test_as_fill_kwargs_matches_the_schema(self, fees):
        kwargs = fees.as_fill_kwargs()
        assert set(kwargs) == {"commission", "exchange_fees", "regulatory_fees"}
        assert sum(kwargs.values()) == fees.total

    def test_feeds_straight_into_a_fill(self):
        from backtest.simulator import Fill

        fees = CommissionCalculator.for_broker("zerodha").calculate(
            **LAKH, side="buy", segment=TradeSegment.EQUITY_INTRADAY
        )
        fill = Fill(
            symbol="INFY", side="buy", quantity=100, fill_price=1000, **fees.as_fill_kwargs()
        )
        assert fill.total_fees == fees.total

    def test_effective_bps(self, fees):
        assert fees.effective_bps(100_000) > D("0")
        assert fees.effective_bps(0) == D("0")

    def test_addition_combines_components(self):
        calc = CommissionCalculator.for_broker("zerodha")
        a = calc.calculate(**LAKH, side="buy")
        b = calc.calculate(**LAKH, side="sell")
        assert (a + b).total == a.total + b.total

    def test_describe_is_readable(self, fees):
        text = fees.describe()
        assert "TOTAL" in text and "zerodha" in text and "stt" in text

    def test_to_dict_is_json_safe(self, fees):
        assert json.loads(json.dumps(fees.to_dict()))["broker"] == "zerodha"

    def test_taxes_property(self):
        fees = CommissionCalculator.for_broker("india_zero").calculate(**LAKH, side="buy")
        assert fees.taxes == fees.get("stt") + fees.get("stamp_duty") + fees.get("gst")


# ===========================================================================
# Currency conversion
# ===========================================================================


class TestCurrencyConverter:
    @pytest.fixture()
    def fx(self):
        return CurrencyConverter(base="INR", rates={"USD": D("83"), "INR": D("1")})

    def test_same_currency_is_identity(self, fx):
        assert fx.convert(100, "INR", "INR") == D("100.0000")

    def test_usd_to_inr(self, fx):
        assert fx.convert(10, "USD", "INR") == D("830.0000")

    def test_inr_to_usd(self, fx):
        assert fx.convert(830, "INR", "USD") == D("10.0000")

    def test_unknown_currency_rejected(self, fx):
        """Guessing an FX rate would silently misstate costs."""
        with pytest.raises(ValidationError, match="no exchange rate"):
            fx.convert(100, "EUR", "INR")

    def test_non_positive_rate_rejected(self):
        with pytest.raises(ValidationError, match="must be positive"):
            CurrencyConverter(base="INR", rates={"USD": D("0")})

    def test_calculator_reports_in_another_currency(self, fx):
        calc = CommissionCalculator.for_broker("ibkr", converter=fx, report_currency="INR")
        fees = calc.calculate(quantity=100, fill_price=50, side="buy")
        assert fees.currency == "INR"
        assert fees.brokerage == D("83.0000")  # $1 minimum x 83

    def test_no_conversion_when_currencies_match(self):
        calc = CommissionCalculator.for_broker("ibkr")
        assert calc.calculate(quantity=100, fill_price=50, side="buy").currency == "USD"


# ===========================================================================
# Configuration
# ===========================================================================


class TestConfiguration:
    def test_ships_a_loadable_file(self):
        assert load_broker_profile().name == "zerodha"

    @pytest.mark.parametrize(
        "broker",
        [
            "zerodha",
            "mstock",
            "india_full_service",
            "india_zero",
            "india_tiered",
            "ibkr",
            "td_ameritrade",
            "robinhood",
            "generic_discount",
            "zero",
        ],
    )
    def test_every_configured_broker_loads_and_prices(self, broker):
        calc = CommissionCalculator.from_config(broker=broker)
        assert calc.calculate(quantity=100, fill_price=1000, side="buy").total >= D("0")

    def test_config_matches_the_preset(self):
        """The YAML and the built-in preset must not drift apart."""
        from_config = CommissionCalculator.from_config(broker="zerodha")
        from_preset = CommissionCalculator.for_broker("zerodha")
        args = dict(**LAKH, side="buy", segment=TradeSegment.EQUITY_INTRADAY)
        assert from_config.calculate(**args).total == from_preset.calculate(**args).total

    def test_unknown_broker_falls_back_to_preset(self):
        assert load_broker_profile(broker="ibkr").name == "ibkr"

    def test_missing_explicit_file_is_an_error(self):
        with pytest.raises(ValidationError, match="not found"):
            load_broker_profile(path="/nonexistent/brokers.yaml")

    def test_unknown_keys_rejected(self, tmp_path):
        bad = tmp_path / "b.yaml"
        bad.write_text("brokers:\n  x:\n    nonsense: 1\n")
        with pytest.raises(ValidationError, match="unknown broker config keys"):
            load_broker_profile(path=str(bad), broker="x")

    def test_malformed_yaml_names_the_file(self, tmp_path):
        bad = tmp_path / "b.yaml"
        bad.write_text("brokers:\n  a: b: c\n")
        with pytest.raises(ValidationError, match="could not parse"):
            load_broker_profile(path=str(bad))

    def test_custom_broker_from_yaml(self, tmp_path):
        cfg = tmp_path / "b.yaml"
        cfg.write_text(
            "brokers:\n"
            "  mine:\n"
            "    currency: INR\n"
            "    commission_model:\n"
            "      model: flat\n"
            "      per_trade: 9\n"
            "    fee_schedule:\n"
            "      schedule: none\n"
        )
        calc = CommissionCalculator.from_config(path=str(cfg), broker="mine")
        assert calc.calculate(**LAKH, side="buy").total == D("9.00")


class TestResolveFeeSchedule:
    def test_by_name(self):
        assert resolve_fee_schedule("india_equity").name == "india_equity"
        assert resolve_fee_schedule("us_equity").name == "us_equity"
        assert resolve_fee_schedule(None).name == "none"

    def test_from_dict_with_overrides(self):
        schedule = resolve_fee_schedule({"schedule": "india_equity", "gst_rate": 0.05})
        assert schedule.gst_rate == D("0.05")

    def test_unknown_rejected(self):
        with pytest.raises(ValidationError, match="unknown fee schedule"):
            resolve_fee_schedule("martian_equity")

    def test_bad_kwargs_reported(self):
        with pytest.raises(ValidationError, match="bad configuration"):
            resolve_fee_schedule({"schedule": "us_equity", "nonsense": 1})


# ===========================================================================
# Reporting
# ===========================================================================


class TestStatistics:
    def test_empty(self):
        stats = CommissionCalculator.for_broker("zerodha").statistics()
        assert stats["count"] == 0

    def test_totals_accumulate(self):
        calc = CommissionCalculator.for_broker("india_zero")
        calc.calculate(**LAKH, side="buy")
        calc.calculate(**LAKH, side="sell")
        stats = calc.statistics()
        assert stats["count"] == 2
        assert stats["total"] == D("237.82")
        assert calc.get_total_fees() == D("237.82")

    def test_component_breakdown(self):
        calc = CommissionCalculator.for_broker("india_zero")
        calc.calculate(**LAKH, side="buy")
        calc.calculate(**LAKH, side="sell")
        components = calc.statistics()["components"]
        assert components["stt"] == D("200.00")
        assert components["stamp_duty"] == D("15.00")
        assert components["dp_charges"] == D("15.34")

    def test_bucket_totals(self):
        calc = CommissionCalculator.for_broker("zerodha")
        calc.calculate(**LAKH, side="buy", segment=TradeSegment.EQUITY_INTRADAY)
        stats = calc.statistics()
        assert stats["brokerage"] == D("20.00")
        assert (
            stats["brokerage"] + stats["exchange_fees"] + stats["regulatory_fees"] == stats["total"]
        )

    def test_recording_can_be_disabled(self):
        calc = CommissionCalculator.for_broker("zerodha", record=False)
        calc.calculate(**LAKH, side="buy")
        assert calc.statistics()["count"] == 0
