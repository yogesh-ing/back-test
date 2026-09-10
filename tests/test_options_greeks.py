"""Tests for options Greeks and risk (Phase 5).

Covers:
- T5.1: Black-Scholes pricing (calls and puts)
- T5.2: Greeks (delta, gamma, theta, vega, rho) vs reference values
- T5.3: Portfolio Greeks aggregation
- T5.4: Implied volatility solver
- T5.5-T5.6: Margin calculation (long, short, spread)
- T5.7-T5.8: Pre-trade risk checks
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
import pytest

from backtest.options.greeks import (
    BlackScholes,
    OptionGreeks,
    calculate_price,
    calculate_greeks,
)
from backtest.options.portfolio_greeks import (
    PortfolioGreeks,
    PortfolioGreeksCalculator,
)
from backtest.options.margin import (
    MarginCalculator,
    MarginResult,
    PreTradeRiskCheck,
    RiskCheckResult,
)
from backtest.options.paper_trading import OptionPosition, PositionStatus
from backtest.instruments.base import ExerciseType, SettlementType


# ---------------------------------------------------------------------------
# Black-Scholes pricing tests
# ---------------------------------------------------------------------------

class TestBlackScholesPrice:
    def setup_method(self):
        self.bs = BlackScholes(risk_free_rate=0.06, volatility=0.20)

    def test_at_the_money_call(self):
        # ATM call: spot = strike = 25000, 30 days to expiry
        price = self.bs.price(spot=25000, strike=25000, expiry_years=30/365, option_type="CE")
        assert price > 0
        # ATM call with 20% vol, 30 days: reasonable range
        assert 100 < price < 1000

    def test_at_the_money_put(self):
        price = self.bs.price(spot=25000, strike=25000, expiry_years=30/365, option_type="PE")
        assert price > 0
        # ATM put should also be positive
        assert 100 < price < 1000

    def test_deep_in_the_money_call(self):
        # Deep ITM: spot=26000, strike=24000 → intrinsic ≈ 2000
        price = self.bs.price(spot=26000, strike=24000, expiry_years=30/365, option_type="CE")
        assert price > 2000  # at least intrinsic

    def test_deep_out_of_the_money(self):
        # Deep OTM: spot=24000, strike=26000 → very low value
        price = self.bs.price(spot=24000, strike=26000, expiry_years=30/365, option_type="CE")
        assert 0 < price < 100

    def test_at_expiry_returns_intrinsic(self):
        price = self.bs.price(spot=25500, strike=25000, expiry_years=0, option_type="CE")
        assert price == 500.0  # intrinsic = max(0, 25500-25000)

    def test_put_at_expiry(self):
        price = self.bs.price(spot=24500, strike=25000, expiry_years=0, option_type="PE")
        assert price == 500.0  # intrinsic = max(0, 25000-24500)

    def test_invalid_option_type(self):
        with pytest.raises(ValueError, match="option_type must be CE or PE"):
            self.bs.price(spot=25000, strike=25000, expiry_years=0.1, option_type="XY")

    def test_negative_rate_raises(self):
        with pytest.raises(ValueError, match="risk_free_rate must be >= 0"):
            BlackScholes(risk_free_rate=-0.01)

    def test_zero_volatility_raises(self):
        with pytest.raises(ValueError, match="volatility must be > 0"):
            BlackScholes(volatility=0)


# ---------------------------------------------------------------------------
# Greeks tests
# ---------------------------------------------------------------------------

class TestGreeks:
    def setup_method(self):
        self.bs = BlackScholes(risk_free_rate=0.06, volatility=0.20)

    def test_call_delta_range(self):
        greeks = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="CE")
        assert 0 < greeks.delta < 1
        # ATM call delta ≈ 0.5
        assert 0.4 < greeks.delta < 0.6

    def test_put_delta_range(self):
        greeks = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="PE")
        assert -1 < greeks.delta < 0
        # ATM put delta ≈ -0.5
        assert -0.6 < greeks.delta < -0.4

    def test_gamma_positive(self):
        greeks = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="CE")
        assert greeks.gamma > 0

    def test_theta_negative(self):
        # Time decay should be negative for long options
        greeks = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="CE")
        assert greeks.theta < 0

    def test_vega_positive(self):
        greeks = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="CE")
        assert greeks.vega > 0

    def test_call_rho_positive(self):
        greeks = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="CE")
        assert greeks.rho > 0

    def test_put_rho_negative(self):
        greeks = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="PE")
        assert greeks.rho < 0

    def test_deep_itm_call_delta_near_1(self):
        greeks = self.bs.greeks(spot=27000, strike=24000, expiry_years=30/365, option_type="CE")
        assert greeks.delta > 0.95

    def test_deep_otm_call_delta_near_0(self):
        greeks = self.bs.greeks(spot=23000, strike=26000, expiry_years=30/365, option_type="CE")
        assert greeks.delta < 0.05

    def test_greeks_at_expiry(self):
        greeks = self.bs.greeks(spot=25500, strike=25000, expiry_years=0, option_type="CE")
        assert greeks.delta == 1.0
        assert greeks.gamma == 0.0
        assert greeks.theta == 0.0

    def test_put_call_parity_greeks(self):
        # Call delta - Put delta ≈ 1 (for same strike/expiry)
        call = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="CE")
        put = self.bs.greeks(spot=25000, strike=25000, expiry_years=30/365, option_type="PE")
        assert abs((call.delta - put.delta) - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Implied volatility tests
# ---------------------------------------------------------------------------

class TestImpliedVolatility:
    def setup_method(self):
        self.bs = BlackScholes(risk_free_rate=0.06, volatility=0.20)

    def test_roundtrip(self):
        """Price an option at 20% vol, then solve for IV → should get 20% back."""
        spot, strike, t = 25000, 25000, 30/365
        price = self.bs.price(spot, strike, t, "CE", volatility=0.20)
        iv = self.bs.implied_volatility(price, spot, strike, t, "CE")
        assert abs(iv - 0.20) < 0.001

    def test_roundtrip_different_vol(self):
        spot, strike, t = 24800, 25000, 45/365
        price = self.bs.price(spot, strike, t, "CE", volatility=0.25)
        iv = self.bs.implied_volatility(price, spot, strike, t, "CE")
        assert abs(iv - 0.25) < 0.001

    def test_roundtrip_put(self):
        spot, strike, t = 24800, 25000, 60/365
        price = self.bs.price(spot, strike, t, "PE", volatility=0.18)
        iv = self.bs.implied_volatility(price, spot, strike, t, "PE")
        assert abs(iv - 0.18) < 0.001


# ---------------------------------------------------------------------------
# Portfolio Greeks tests
# ---------------------------------------------------------------------------

class TestPortfolioGreeks:
    def _make_position(
        self,
        option_type: str = "CE",
        side: str = "BUY",
        strike: int = 25000,
        quantity: int = 1,
        lot_size: int = 25,
        underlying: str = "NIFTY",
    ) -> OptionPosition:
        return OptionPosition(
            side=side,
            quantity=quantity,
            lot_size=lot_size,
            strike=Decimal(str(strike)),
            option_type=option_type,
            underlying=underlying,
            expiry=date.today() + timedelta(days=30),
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            status=PositionStatus.OPEN,
        )

    def test_single_long_call(self):
        calc = PortfolioGreeksCalculator(risk_free_rate=0.06)
        positions = [self._make_position("CE", "BUY")]
        result = calc.calculate(positions, spot_prices={"NIFTY": 25000})
        assert result.total_delta > 0
        assert result.total_gamma > 0
        assert result.total_theta < 0

    def test_long_call_short_put_spread(self):
        """Bullish position: long call + short put → positive delta."""
        calc = PortfolioGreeksCalculator(risk_free_rate=0.06)
        positions = [
            self._make_position("CE", "BUY", strike=25000),
            self._make_position("PE", "SELL", strike=24500),
        ]
        result = calc.calculate(positions, spot_prices={"NIFTY": 25000})
        # Long call (+delta) + short put (+delta) → strongly positive
        assert result.total_delta > 0.5

    def test_by_underlying_breakdown(self):
        calc = PortfolioGreeksCalculator(risk_free_rate=0.06)
        positions = [
            self._make_position("CE", "BUY", underlying="NIFTY"),
            self._make_position("CE", "BUY", underlying="BANKNIFTY"),
        ]
        result = calc.calculate(positions, spot_prices={"NIFTY": 25000, "BANKNIFTY": 52000})
        assert "NIFTY" in result.by_underlying
        assert "BANKNIFTY" in result.by_underlying

    def test_empty_positions(self):
        calc = PortfolioGreeksCalculator()
        result = calc.calculate([])
        assert result.total_delta == 0.0
        assert result.total_value == 0.0

    def test_to_dict(self):
        calc = PortfolioGreeksCalculator()
        result = calc.calculate([self._make_position()])
        d = result.to_dict()
        assert "total_delta" in d
        assert "by_underlying" in d


# ---------------------------------------------------------------------------
# Margin calculator tests
# ---------------------------------------------------------------------------

class TestMarginCalculator:
    def setup_method(self):
        self.calc = MarginCalculator()

    def test_long_margin(self):
        result = self.calc.calculate_long_margin(
            premium=100.0, lot_size=25, lots=2,
        )
        # Premium = 100 × 25 × 2 = 5000
        assert result.net_margin == 5000.0
        assert result.premium_margin == 5000.0

    def test_short_margin(self):
        result = self.calc.calculate_short_margin(
            underlying_price=25000, strike=25000,
            lot_size=25, lots=1, premium=100.0,
        )
        assert result.net_margin > 0
        assert result.span_margin > 0
        assert result.exposure_margin > 0

    def test_short_margin_premium_reduces(self):
        result_no_premium = self.calc.calculate_short_margin(
            underlying_price=25000, strike=25000,
            lot_size=25, lots=1, premium=0.0,
        )
        result_with_premium = self.calc.calculate_short_margin(
            underlying_price=25000, strike=25000,
            lot_size=25, lots=1, premium=200.0,
        )
        assert result_with_premium.net_margin < result_no_premium.net_margin

    def test_spread_margin(self):
        result = self.calc.calculate_spread_margin(
            long_strike=24700, short_strike=25000,
            long_premium=150, short_premium=50,
            lot_size=25, lots=1, underlying_price=25000,
        )
        assert result.net_margin >= 0
        # Spread margin with small premiums: basic check
        assert result.span_margin > 0


# ---------------------------------------------------------------------------
# Pre-trade risk checks
# ---------------------------------------------------------------------------

class TestPreTradeRiskCheck:
    def setup_method(self):
        self.risk = PreTradeRiskCheck(
            max_margin=10_00_000,
            max_positions=10,
            max_loss_per_trade_pct=2.0,
            max_single_position_pct=10.0,
            capital=10_00_000,
        )

    def test_allows_valid_trade(self):
        # margin 15K < 2% of 10L (20K), notional 50K < 10% of 10L (1L)
        result = self.risk.check(
            margin_required=15_000,
            current_margin_used=2_00_000,
            current_positions=3,
            trade_notional=50_000,
        )
        assert result.allowed

    def test_rejects_margin_exceeded(self):
        result = self.risk.check(
            margin_required=9_00_000,
            current_margin_used=5_00_000,
            current_positions=3,
        )
        assert not result.allowed
        assert "Margin limit" in result.reason

    def test_rejects_position_limit(self):
        result = self.risk.check(
            margin_required=50_000,
            current_margin_used=2_00_000,
            current_positions=10,
        )
        assert not result.allowed
        assert "Position limit" in result.reason

    def test_rejects_position_too_large(self):
        result = self.risk.check(
            margin_required=50_000,
            current_margin_used=2_00_000,
            current_positions=3,
            trade_notional=15_00_000,  # > 10% of 10L capital
        )
        assert not result.allowed
        assert "too large" in result.reason

    def test_rejects_loss_limit(self):
        result = self.risk.check(
            margin_required=3_00_000,  # > 2% of 10L = 2L
            current_margin_used=2_00_000,
            current_positions=3,
        )
        assert not result.allowed
        assert "loss exceeds" in result.reason

    def test_passes_all_checks(self):
        # margin 15K < 2% of 10L (20K), notional 50K < 10% of 10L (1L)
        result = self.risk.check(
            margin_required=15_000,
            current_margin_used=1_00_000,
            current_positions=5,
            trade_notional=50_000,
        )
        assert result
        assert result.allowed
