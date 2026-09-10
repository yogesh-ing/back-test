"""Unit tests for the instruments package.

Tests cover:
- OptionContract validation (V1 constraints)
- EquityInstrument validation
- InstrumentRegistry lookups
- Edge cases (invalid tokens, expired contracts, unsupported underlyings)
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.instruments import (
    EquityInstrument,
    InstrumentRegistry,
    OptionContract,
    OptionQuote,
)
from backtest.instruments.base import (
    ExerciseType,
    InstrumentType,
    OptionType,
    SettlementType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_option(**overrides) -> OptionContract:
    """Factory for valid option contracts with sensible defaults."""
    defaults = dict(
        instrument_token="NIFTY24DEC24500CE",
        trading_symbol="NIFTY24DEC24500CE",
        underlying="NIFTY",
        exchange="NSE",
        segment="NFO",
        expiry=date.today() + timedelta(days=30),
        strike=Decimal("24500"),
        option_type=OptionType.CE,
        lot_size=25,
        tick_size=Decimal("0.05"),
        contract_type=ExerciseType.EUROPEAN,
        settlement_type=SettlementType.CASH,
    )
    defaults.update(overrides)
    return OptionContract(**defaults)


def _make_equity(**overrides) -> EquityInstrument:
    """Factory for valid equity instruments."""
    defaults = dict(
        instrument_token="11536",
        trading_symbol="RELIANCE",
        exchange="NSE",
        segment="CASH",
        lot_size=1,
        tick_size=0.05,
    )
    defaults.update(overrides)
    return EquityInstrument(**defaults)


# ---------------------------------------------------------------------------
# OptionContract validation tests
# ---------------------------------------------------------------------------

class TestOptionContractValidation:
    """V1 validation rules for OptionContract."""

    def test_valid_option(self):
        opt = _make_option()
        assert opt.is_valid()
        assert opt.validate() == []

    def test_reject_empty_token(self):
        opt = _make_option(instrument_token="")
        errors = opt.validate()
        assert any("instrument_token" in e for e in errors)

    def test_reject_empty_symbol(self):
        opt = _make_option(trading_symbol="")
        errors = opt.validate()
        assert any("trading_symbol" in e for e in errors)

    def test_reject_unsupported_underlying(self):
        opt = _make_option(underlying="RELIANCE")
        errors = opt.validate()
        assert any("NIFTY" in e for e in errors)

    def test_accept_nifty(self):
        opt = _make_option(underlying="NIFTY")
        assert opt.is_valid()

    def test_accept_banknifty(self):
        opt = _make_option(underlying="BANKNIFTY")
        assert opt.is_valid()

    def test_reject_non_nse_exchange(self):
        opt = _make_option(exchange="BSE")
        errors = opt.validate()
        assert any("NSE" in e for e in errors)

    def test_reject_non_nfo_segment(self):
        opt = _make_option(segment="CASH")
        errors = opt.validate()
        assert any("NFO" in e for e in errors)

    def test_reject_past_expiry(self):
        opt = _make_option(expiry=date.today() - timedelta(days=1))
        errors = opt.validate()
        assert any("past" in e for e in errors)

    def test_reject_zero_strike(self):
        opt = _make_option(strike=Decimal("0"))
        errors = opt.validate()
        assert any("strike" in e for e in errors)

    def test_reject_negative_strike(self):
        opt = _make_option(strike=Decimal("-100"))
        errors = opt.validate()
        assert any("strike" in e for e in errors)

    def test_reject_invalid_option_type(self):
        opt = _make_option(option_type="XX")
        errors = opt.validate()
        assert any("CE or PE" in e for e in errors)

    def test_reject_zero_lot_size(self):
        opt = _make_option(lot_size=0)
        errors = opt.validate()
        assert any("lot_size" in e for e in errors)

    def test_reject_american_option(self):
        opt = _make_option(contract_type=ExerciseType.AMERICAN)
        errors = opt.validate()
        assert any("european" in e for e in errors)

    def test_reject_american_string(self):
        opt = _make_option(contract_type="american")
        errors = opt.validate()
        assert any("european" in e for e in errors)

    def test_reject_physical_settlement(self):
        opt = _make_option(settlement_type=SettlementType.PHYSICAL)
        errors = opt.validate()
        assert any("cash" in e for e in errors)

    def test_reject_physical_string(self):
        opt = _make_option(settlement_type="physical")
        errors = opt.validate()
        assert any("cash" in e for e in errors)

    def test_multiple_errors(self):
        opt = _make_option(
            instrument_token="",
            underlying="RELIANCE",
            strike=Decimal("0"),
        )
        errors = opt.validate()
        assert len(errors) >= 3

    def test_option_type_ce_string(self):
        opt = _make_option(option_type="CE")
        assert opt.is_valid()

    def test_option_type_pe_string(self):
        opt = _make_option(option_type="PE", strike=Decimal("25000"))
        assert opt.is_valid()

    def test_frozen_immutable(self):
        opt = _make_option()
        with pytest.raises(AttributeError):
            opt.strike = Decimal("26000")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EquityInstrument validation tests
# ---------------------------------------------------------------------------

class TestEquityInstrumentValidation:
    def test_valid_equity(self):
        eq = _make_equity()
        assert eq.is_valid()

    def test_reject_empty_token(self):
        eq = _make_equity(instrument_token="")
        assert not eq.is_valid()

    def test_reject_empty_symbol(self):
        eq = _make_equity(trading_symbol="")
        assert not eq.is_valid()

    def test_reject_bad_exchange(self):
        eq = _make_equity(exchange="UNKNOWN")
        assert not eq.is_valid()

    def test_reject_zero_lot_size(self):
        eq = _make_equity(lot_size=0)
        assert not eq.is_valid()


# ---------------------------------------------------------------------------
# InstrumentRegistry tests
# ---------------------------------------------------------------------------

class TestInstrumentRegistry:
    def test_register_and_get(self):
        reg = InstrumentRegistry()
        opt = _make_option()
        reg.register(opt)
        assert reg.count == 1
        assert reg.get_by_token("NIFTY24DEC24500CE") is opt

    def test_register_rejects_invalid(self):
        reg = InstrumentRegistry()
        opt = _make_option(instrument_token="")
        with pytest.raises(ValueError):
            reg.register(opt)

    def test_register_many(self):
        reg = InstrumentRegistry()
        opts = [
            _make_option(instrument_token=f"N{i}", trading_symbol=f"N{i}")
            for i in range(5)
        ]
        count = reg.register_many(opts)
        assert count == 5
        assert reg.count == 5

    def test_get_by_token_missing(self):
        reg = InstrumentRegistry()
        assert reg.get_by_token("MISSING") is None

    def test_find_options_by_underlying(self):
        reg = InstrumentRegistry()
        reg.register(_make_option(instrument_token="N1", trading_symbol="N1", underlying="NIFTY"))
        reg.register(_make_option(instrument_token="B1", trading_symbol="B1", underlying="BANKNIFTY"))
        nifty = reg.find_options(underlying="NIFTY")
        assert len(nifty) == 1
        assert nifty[0].underlying == "NIFTY"

    def test_find_options_by_expiry(self):
        reg = InstrumentRegistry()
        exp1 = date.today() + timedelta(days=10)
        exp2 = date.today() + timedelta(days=30)
        reg.register(_make_option(instrument_token="A", trading_symbol="A", expiry=exp1))
        reg.register(_make_option(instrument_token="B", trading_symbol="B", expiry=exp2))
        assert len(reg.find_options(expiry=exp1)) == 1

    def test_find_options_by_option_type(self):
        reg = InstrumentRegistry()
        reg.register(_make_option(instrument_token="C1", trading_symbol="C1", option_type=OptionType.CE))
        reg.register(_make_option(instrument_token="P1", trading_symbol="P1", option_type=OptionType.PE, strike=Decimal("25000")))
        assert len(reg.find_options(option_type="CE")) == 1
        assert len(reg.find_options(option_type="PE")) == 1

    def test_get_expiry_dates(self):
        reg = InstrumentRegistry()
        exp1 = date.today() + timedelta(days=10)
        exp2 = date.today() + timedelta(days=30)
        reg.register(_make_option(instrument_token="A", trading_symbol="A", expiry=exp1))
        reg.register(_make_option(instrument_token="B", trading_symbol="B", expiry=exp2))
        dates = reg.get_expiry_dates("NIFTY")
        assert dates == [exp1, exp2]

    def test_get_strikes(self):
        reg = InstrumentRegistry()
        exp = date.today() + timedelta(days=30)
        for s in [24000, 24500, 25000]:
            reg.register(_make_option(
                instrument_token=f"N{s}", trading_symbol=f"N{s}",
                expiry=exp, strike=Decimal(str(s)),
            ))
        strikes = reg.get_strikes("NIFTY", exp)
        assert strikes == [Decimal("24000"), Decimal("24500"), Decimal("25000")]

    def test_count_properties(self):
        reg = InstrumentRegistry()
        reg.register(_make_option())
        reg.register(_make_equity())
        assert reg.count == 2
        assert reg.option_count == 1
        assert reg.equity_count == 1

    def test_clear(self):
        reg = InstrumentRegistry()
        reg.register(_make_option())
        reg.clear()
        assert reg.count == 0

    def test_register_overwrites_same_token(self):
        reg = InstrumentRegistry()
        reg.register(_make_option(instrument_token="T1", trading_symbol="T1", strike=Decimal("24500")))
        reg.register(_make_option(instrument_token="T1", trading_symbol="T1", strike=Decimal("25000")))
        inst = reg.get_by_token("T1")
        assert inst is not None
        assert inst.strike == Decimal("25000")


# ---------------------------------------------------------------------------
# OptionQuote tests
# ---------------------------------------------------------------------------

class TestOptionQuote:
    def test_basic_quote(self):
        q = OptionQuote(
            instrument_token="NIFTY24DEC24500CE",
            ltp=Decimal("150.50"),
            bid=Decimal("150.00"),
            ask=Decimal("151.00"),
            volume=1000,
            oi=50000,
        )
        assert q.ltp == Decimal("150.50")
        assert q.oi == 50000

    def test_quote_defaults(self):
        q = OptionQuote(instrument_token="X", ltp=Decimal("10"))
        assert q.bid is None
        assert q.ask is None
        assert q.iv is None
