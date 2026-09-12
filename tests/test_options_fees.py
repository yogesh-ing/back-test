"""Tests for option fee modelling (Phase 8 — T8.1–T8.4).

The reference figures are the FY 2024-25 NFO rates a real contract note
shows for a single NIFTY option order of 75 units (one lot) at a ₹120.50
premium (₹9,037.50 turnover) on the mStock flat-fee plan:

    brokerage             20.00   (flat per order)
    exchange_transaction   3.17   (0.03503% of premium)
    sebi_turnover          0.01   (Rs 10/crore)
    ipft                   0.01
    stamp_duty             0.27   (0.003%, buy side only)
    gst                    4.17   (18% on brokerage + exchange + SEBI + IPFT)
    TOTAL                 27.63

The sell side additionally pays STT at 0.1% of the premium — buy side pays
none. Every test below prices against these anchors.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backtest.options.paper_trading import OptionPaperBroker
from backtest.simulator import (
    BrokerProfile,
    CommissionCalculator,
    FeeBreakdown,
    FlatCommission,
    IndiaEquityFees,
    OptionsCommission,
    TradeSegment,
    ValidationError,
    ZeroCommission,
)
from backtest.simulator.fees import ContractNote

D = Decimal

# One NIFTY lot: 75 units at a 120.50 premium = Rs 9,037.50 turnover.
LOT = dict(quantity=75, fill_price=D("120.50"))


# ===========================================================================
# T8.1 — OptionsCommission model
# ===========================================================================


class TestOptionsCommission:
    def test_flat_per_order_regardless_of_size(self):
        model = OptionsCommission()
        assert model.calculate(75, D("120.50")) == D("20.0000")
        # Ten lots is still one order.
        assert model.calculate(750, D("120.50")) == D("20.0000")

    def test_per_order_overridable(self):
        assert OptionsCommission(per_order=D("0")).calculate(1, D("1")) == D("0")

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="must not be negative"):
            OptionsCommission(per_order=D("-1"))

    def test_resolves_from_config_dict(self):
        from backtest.simulator.commission import resolve_commission_model

        model = resolve_commission_model({"model": "options_flat", "per_order": "20"})
        assert isinstance(model, OptionsCommission)
        assert model.per_order == D("20")

    def test_side_does_not_matter(self):
        from backtest.simulator.enums import OrderSide

        model = OptionsCommission()
        assert model.calculate(75, D("120.50"), OrderSide.BUY) == model.calculate(
            75, D("120.50"), OrderSide.SELL
        )


# ===========================================================================
# T8.1 — the statutory stack on one option order
# ===========================================================================


class TestOptionStatutoryStack:
    @pytest.fixture()
    def buy(self):
        calc = CommissionCalculator.for_broker("mstock")
        return calc.calculate(**LOT, side="buy", segment=TradeSegment.OPTIONS)

    @pytest.fixture()
    def sell(self):
        calc = CommissionCalculator.for_broker("mstock")
        return calc.calculate(**LOT, side="sell", segment=TradeSegment.OPTIONS)

    def test_brokerage_is_flat_per_order(self, buy):
        assert buy.get("brokerage") == D("20.00")

    def test_buy_side_breakdown_matches_contract_note(self, buy):
        # The full anchor breakdown for one buy-side NIFTY lot.
        assert buy.get("exchange_transaction") == D("3.17")  # 0.03503%
        assert buy.get("sebi_turnover") == D("0.01")  # Rs 10/crore
        assert buy.get("ipft") == D("0.01")
        assert buy.get("stamp_duty") == D("0.27")  # 0.003% buy only
        assert buy.get("gst") == D("4.17")  # 18% on 20 + 3.17 + 0.01 + 0.01
        assert buy.total == D("27.63")

    def test_buy_side_pays_no_stt(self, buy):
        assert buy.get("stt") == D("0")

    def test_sell_side_pays_stt_on_premium(self, sell):
        # 0.1% of the 9,037.50 premium.
        assert sell.get("stt") == D("9.04")
        assert sell.get("stamp_duty") == D("0")

    def test_options_exchange_charge_beats_equity_at_same_value(self):
        calc = CommissionCalculator.for_broker("india_zero")
        equity = calc.calculate(**LOT, side="buy", segment=TradeSegment.EQUITY_DELIVERY)
        options = calc.calculate(**LOT, side="buy", segment=TradeSegment.OPTIONS)
        assert options.get("exchange_transaction") > equity.get("exchange_transaction")


# ===========================================================================
# T8.1 — BrokerProfile option routing
# ===========================================================================


class TestBrokerProfileOptionsModel:
    def test_option_segment_uses_options_model(self):
        profile = BrokerProfile(
            name="test",
            commission_model=FlatCommission(per_trade=D("100")),
            options_commission_model=OptionsCommission(per_order=D("20")),
            fee_schedule=IndiaEquityFees(),
        )
        calc = CommissionCalculator(profile)
        equity = calc.calculate(**LOT, side="buy", segment=TradeSegment.EQUITY_DELIVERY)
        option = calc.calculate(**LOT, side="buy", segment=TradeSegment.OPTIONS)
        assert equity.get("brokerage") == D("100.00")
        assert option.get("brokerage") == D("20.00")

    def test_none_falls_back_to_default_model(self):
        profile = BrokerProfile(
            name="test",
            commission_model=FlatCommission(per_trade=D("100")),
            fee_schedule=IndiaEquityFees(),
        )
        calc = CommissionCalculator(profile)
        option = calc.calculate(**LOT, side="buy", segment=TradeSegment.OPTIONS)
        assert option.get("brokerage") == D("100.00")

    def test_resolves_from_dict(self):
        profile = BrokerProfile(
            name="test",
            options_commission_model={"model": "options_flat", "per_order": "20"},
            fee_schedule=IndiaEquityFees(),
        )
        assert isinstance(profile.options_commission_model, OptionsCommission)

    def test_mstock_preset_routes_options(self):
        profile = CommissionCalculator.for_broker("mstock").broker
        assert profile.model_for(TradeSegment.OPTIONS).to_dict() == {
            "model": "options_flat",
            "per_order": "20.0000",
        }
        assert profile.model_for(TradeSegment.EQUITY_INTRADAY).to_dict() == {
            "model": "flat",
            "per_trade": "20.0000",
        }

    def test_profile_to_dict_includes_options_model(self):
        profile = BrokerProfile(
            name="test",
            options_commission_model=OptionsCommission(per_order=D("20")),
            fee_schedule=IndiaEquityFees(),
        )
        assert profile.to_dict()["options_commission_model"] == {
            "model": "options_flat",
            "per_order": "20.0000",
        }


# ===========================================================================
# T8.2 — multi-leg structure fees
# ===========================================================================


class TestCalculateStructure:
    @pytest.fixture()
    def calc(self):
        return CommissionCalculator.for_broker("mstock")

    def test_bull_call_spread_brokerage_is_two_orders(self, calc):
        fees = calc.calculate_structure(
            [
                {"side": "BUY", "quantity": 75, "price": "120.50"},
                {"side": "SELL", "quantity": 75, "price": "45.20"},
            ]
        )
        assert fees.get("brokerage") == D("40.00")  # Rs 20 per leg

    def test_per_leg_components_are_kept(self, calc):
        fees = calc.calculate_structure(
            [
                {"side": "BUY", "quantity": 75, "price": "120.50"},
                {"side": "SELL", "quantity": 75, "price": "45.20"},
            ]
        )
        # Leg 0 is the BUY: no STT. Leg 1 is the SELL: 0.1% of 75 x 45.20.
        assert fees.get("leg_0_stt") == D("0")
        assert fees.get("leg_1_stt") == D("3.39")  # 0.001 x 3390
        assert fees.get("stt") == D("3.39")  # merged equals the sum

    def test_leg_fees_sum_to_merged_total(self, calc):
        fees = calc.calculate_structure(
            [
                {"side": "BUY", "quantity": 75, "price": "120.50"},
                {"side": "SELL", "quantity": 75, "price": "45.20"},
            ]
        )
        per_leg_totals = sum(
            fees.get(f"leg_{i}_{key}")
            for i in range(2)
            for key in ("stt", "exchange_transaction", "sebi_turnover", "ipft", "stamp_duty", "gst")
        )
        merged_non_brokerage = fees.total - fees.get("brokerage")
        assert per_leg_totals == merged_non_brokerage

    def test_empty_structure_rejected(self, calc):
        with pytest.raises(ValidationError, match="at least one leg"):
            calc.calculate_structure([])

    def test_missing_key_rejected(self, calc):
        with pytest.raises(ValidationError, match="missing required key"):
            calc.calculate_structure([{"side": "BUY", "quantity": 75}])

    def test_defaults_to_options_segment(self, calc):
        fees = calc.calculate_structure([{"side": "BUY", "quantity": 75, "price": "120.50"}])
        assert fees.segment == TradeSegment.OPTIONS

    def test_three_legs_pay_three_orders(self, calc):
        fees = calc.calculate_structure(
            [
                {"side": "BUY", "quantity": 75, "price": "120.50"},
                {"side": "SELL", "quantity": 75, "price": "45.20"},
                {"side": "SELL", "quantity": 75, "price": "10.00"},
            ]
        )
        assert fees.get("brokerage") == D("60.00")


# ===========================================================================
# T8.3 — contract-note validation
# ===========================================================================


class TestContractNoteValidation:
    def _note(self) -> ContractNote:
        return ContractNote(
            document_id="CN-2026-09-001",
            broker="mstock",
            note_date=date(2026, 9, 10),
            trade_value=D("9037.50"),
        )

    def test_matching_note_validates_clean(self):
        calc = CommissionCalculator.for_broker("mstock")
        expected = {
            "brokerage": "20.00",
            "exchange_transaction": "3.17",
            "sebi_turnover": "0.01",
            "ipft": "0.01",
            "stamp_duty": "0.27",
            "gst": "4.17",
        }
        mismatches = calc.validate_against_contract_note(
            trade_value=D("9037.50"),
            quantity=75,
            side="buy",
            segment=TradeSegment.OPTIONS,
            expected=expected,
            document=self._note(),
        )
        assert mismatches == []

    def test_drifted_rate_is_reported(self):
        calc = CommissionCalculator(
            BrokerProfile(
                name="mstock_wrong",
                commission_model=FlatCommission(per_trade=D("20")),
                fee_schedule=IndiaEquityFees(exchange_txn_options=D("0.0005")),  # too high
                currency="INR",
            )
        )
        mismatches = calc.validate_against_contract_note(
            trade_value=D("9037.50"),
            quantity=75,
            side="buy",
            segment=TradeSegment.OPTIONS,
            expected={"exchange_transaction": "3.17"},
        )
        assert len(mismatches) == 1
        assert "exchange_transaction" in mismatches[0]
        assert "contract note 3.17" in mismatches[0]

    def test_total_is_comparable(self):
        calc = CommissionCalculator.for_broker("mstock")
        mismatches = calc.validate_against_contract_note(
            trade_value=D("9037.50"),
            quantity=75,
            side="buy",
            segment=TradeSegment.OPTIONS,
            expected={"total": "27.63"},
        )
        assert mismatches == []

    def test_tolerance_is_respected(self):
        calc = CommissionCalculator.for_broker("mstock")
        # 0.50 tolerance forgives a rounding-level drift in stamp duty.
        mismatches = calc.validate_against_contract_note(
            trade_value=D("9037.50"),
            quantity=75,
            side="buy",
            segment=TradeSegment.OPTIONS,
            expected={"stamp_duty": "0.50"},
            tolerance=D("0.50"),
        )
        assert mismatches == []

    def test_note_brokerage_drives_gst_comparison(self):
        # The model's brokerage differs from the note (30 vs 20): the
        # brokerage mismatch is reported, but GST must NOT be additionally
        # flagged — it is recomputed on the note's own brokerage (20), so the
        # rate comparison stays like-for-like.
        calc = CommissionCalculator(
            BrokerProfile(
                name="model",
                commission_model=FlatCommission(per_trade=D("30")),
                fee_schedule=IndiaEquityFees(),
                currency="INR",
            )
        )
        # Note: brokerage 20, GST 18% of (20 + 3.17 + 0.01 + 0.01) = 4.17.
        mismatches = calc.validate_against_contract_note(
            trade_value=D("9037.50"),
            quantity=75,
            side="buy",
            segment=TradeSegment.OPTIONS,
            expected={"brokerage": "20.00", "gst": "4.17"},
        )
        assert len(mismatches) == 1
        assert "brokerage: contract note 20.00 vs computed 30.00" in mismatches[0]

    def test_stamped_document_round_trips(self):
        calc = CommissionCalculator.for_broker("mstock")
        breakdown = calc.calculate(
            **LOT,
            side="buy",
            segment=TradeSegment.OPTIONS,
            document=self._note(),
        )
        stamped = breakdown.components[FeeBreakdown.DOCUMENT_KEY]
        assert isinstance(stamped, ContractNote)
        assert stamped.document_id == "CN-2026-09-001"
        # A document reference must not corrupt the money maths.
        assert breakdown.total == D("27.63")
        assert breakdown.to_dict()["components"]["contract_note_document"] == stamped.to_dict()
        assert breakdown.describe()  # renders without the non-Decimal entry

    def test_document_reference_ignored_by_statistics(self):
        calc = CommissionCalculator.for_broker("mstock")
        calc.calculate(**LOT, side="buy", segment=TradeSegment.OPTIONS, document=self._note())
        stats = calc.statistics()
        assert "contract_note_document" not in stats["components"]
        assert stats["total"] == D("27.63")


class TestContractNote:
    def test_to_dict(self):
        note = ContractNote(
            document_id="CN-1",
            broker="mstock",
            note_date=date(2026, 9, 10),
            trade_value=D("9037.50"),
            file_path="notes/cn.pdf",
            notes="single NIFTY lot buy",
        )
        d = note.to_dict()
        assert d["note_date"] == "2026-09-10"
        assert d["trade_value"] == "9037.50"
        assert d["file_path"] == "notes/cn.pdf"


# ===========================================================================
# Integration — the paper broker's optional fee calculator
# ===========================================================================


class TestOptionPaperBrokerFeeIntegration:
    def _make_broker(self, fee_calculator):
        return OptionPaperBroker(
            capital=500_000.0,
            commission_per_lot=0.0,  # isolate the fee-calculator path
            fee_calculator=fee_calculator,
        )

    def _quote_provider(self, ltp: str = "120.50"):
        provider = MagicMock()
        provider.get_quote.return_value = {"ltp": ltp}
        return provider

    def test_fee_calculator_is_optional_and_backward_compatible(self):
        broker = OptionPaperBroker(capital=100_000.0, commission_per_lot=0.0)
        assert broker.fee_calculator is None
        # A structure still executes exactly as before.
        from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent
        from datetime import date as _date

        intent = TradeIntent(
            view=MarketView(direction=Direction.BULLISH, underlying="NIFTY"),
            structure_type="long_call",
            legs=(
                OptionLeg(
                    instrument_token="NIFTY_CE",
                    trading_symbol="NIFTY26SEP24500CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=_date(2099, 9, 30),
        )
        positions = broker.execute_structure(intent, self._quote_provider())
        assert len(positions) == 1
        assert positions[0].commission == D("0")

    def test_fee_calculator_receives_all_legs(self):
        fee_calc = MagicMock()
        broker = self._make_broker(fee_calc)
        from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent
        from datetime import date as _date

        intent = TradeIntent(
            view=MarketView(direction=Direction.BULLISH, underlying="NIFTY"),
            structure_type="bull_call_spread",
            legs=(
                OptionLeg(
                    instrument_token="CE_24500",
                    trading_symbol="NIFTY26SEP24500CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
                OptionLeg(
                    instrument_token="CE_24600",
                    trading_symbol="NIFTY26SEP24600CE",
                    side="SELL",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=_date(2099, 9, 30),
            metadata={"strikes": {"NIFTY26SEP24500CE": "24500", "NIFTY26SEP24600CE": "24600"}},
        )
        broker.execute_structure(intent, self._quote_provider())
        assert fee_calc.calculate_structure.call_count == 1
        legs = fee_calc.calculate_structure.call_args.args[0]
        assert len(legs) == 2
        assert legs[0]["side"] == "BUY" and legs[1]["side"] == "SELL"
        assert legs[0]["quantity"] == 75

    def test_fee_calculation_failure_never_blocks_execution(self):
        fee_calc = MagicMock()
        fee_calc.calculate_structure.side_effect = RuntimeError("quote feed down")
        broker = self._make_broker(fee_calc)
        from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent
        from datetime import date as _date

        intent = TradeIntent(
            view=MarketView(direction=Direction.BULLISH, underlying="NIFTY"),
            structure_type="long_call",
            legs=(
                OptionLeg(
                    instrument_token="NIFTY_CE",
                    trading_symbol="NIFTY26SEP24500CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=_date(2099, 9, 30),
        )
        positions = broker.execute_structure(intent, self._quote_provider())
        assert len(positions) == 1  # the trade still went through
