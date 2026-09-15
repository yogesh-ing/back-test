"""Option margin calculation and pre-trade risk checks.

V1 implements a conservative margin model based on NSE SPAN margin
approximation.  Real SPAN margins require exchange-provided risk
parameters; V1 uses a simplified formula.

Usage::

    from backtest.options.margin import MarginCalculator, MarginResult

    calc = MarginCalculator()
    result = calc.calculate_spread_margin(
        long_strike=24700, short_strike=25000,
        long_premium=150, short_premium=50,
        lot_size=25, lots=1,
    )

    from backtest.options.margin import PreTradeRiskCheck

    risk = PreTradeRiskCheck(max_margin=20_00_000)
    check = risk.check(margin_required=1_50_000, current_margin_used=10_00_000)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backtest.strategy.intent import TradeIntent


@dataclass
class MarginResult:
    """Result of a margin calculation.

    Attributes
    ----------
    initial_margin:
        Total initial margin required (₹).
    span_margin:
        SPAN margin component (₹).
    exposure_margin:
        Exposure margin component (₹).
    premium_margin:
        Premium payable for long options (₹).
    net_margin:
        Net margin = initial_margin - credit from short options + premium paid.
    """

    initial_margin: float = 0.0
    span_margin: float = 0.0
    exposure_margin: float = 0.0
    premium_margin: float = 0.0
    net_margin: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "initial_margin": round(self.initial_margin, 2),
            "span_margin": round(self.span_margin, 2),
            "exposure_margin": round(self.exposure_margin, 2),
            "premium_margin": round(self.premium_margin, 2),
            "net_margin": round(self.net_margin, 2),
        }


class MarginCalculator:
    """Conservative margin calculator for index options.

    V1 uses simplified formulas:

    - **Long options**: margin = premium × lot_size × lots (you pay the premium)
    - **Short options**: margin = SPAN + exposure (exchange requirement)
    - **Spreads**: margin = max(long leg margin, short leg margin) - spread credit

    The SPAN margin is approximated as:
    ``SPAN ≈ max(0.15 × underlying, 0.10 × strike) × lot_size × lots``

    This is conservative; real SPAN uses delta-weighted risk arrays.
    """

    def __init__(
        self,
        span_pct: float = 0.15,
        exposure_pct: float = 0.10,
        min_margin_per_lot: float = 5000.0,
    ) -> None:
        """
        Parameters
        ----------
        span_pct:
            SPAN margin as % of underlying (e.g. 0.15 = 15%).
        exposure_pct:
            Exposure margin as % of strike (e.g. 0.10 = 10%).
        min_margin_per_lot:
            Minimum margin per lot (₹ floor).
        """
        self.span_pct = span_pct
        self.exposure_pct = exposure_pct
        self.min_margin_per_lot = min_margin_per_lot

    def calculate_long_margin(
        self,
        premium: float,
        lot_size: int,
        lots: int,
    ) -> MarginResult:
        """Margin for a long option position.

        Long options require full premium payment upfront.
        """
        total_units = lot_size * lots
        premium_margin = premium * total_units
        return MarginResult(
            initial_margin=premium_margin,
            premium_margin=premium_margin,
            net_margin=premium_margin,
        )

    def calculate_short_margin(
        self,
        underlying_price: float,
        strike: float,
        lot_size: int,
        lots: int,
        premium: float = 0.0,
    ) -> MarginResult:
        """Margin for a short option position.

        Short options require SPAN + exposure margin.
        """
        total_units = lot_size * lots

        # SPAN: max of underlying-based and strike-based
        span_underlying = underlying_price * self.span_pct
        span_strike = strike * self.exposure_pct
        span_per_unit = max(span_underlying, span_strike, self.min_margin_per_lot / total_units)
        span_margin = span_per_unit * total_units

        # Exposure margin: additional buffer
        exposure_per_unit = underlying_price * 0.05  # 5% of underlying
        exposure_margin = exposure_per_unit * total_units

        initial_margin = span_margin + exposure_margin

        # Credit from premium received
        premium_credit = premium * total_units
        net_margin = initial_margin - premium_credit

        return MarginResult(
            initial_margin=initial_margin,
            span_margin=span_margin,
            exposure_margin=exposure_margin,
            premium_margin=-premium_credit,  # negative = credit
            net_margin=max(net_margin, 0),
        )

    def calculate_spread_margin(
        self,
        long_strike: float,
        short_strike: float,
        long_premium: float,
        short_premium: float,
        lot_size: int,
        lots: int,
        underlying_price: float,
    ) -> MarginResult:
        """Margin for a spread (bull call or bear put).

        Spread margin = max(long margin, short margin) - spread credit.
        This is the key benefit of spreads: defined risk = defined margin.
        """
        # Long leg: just premium
        long_result = self.calculate_long_margin(long_premium, lot_size, lots)

        # Short leg: SPAN + exposure
        short_result = self.calculate_short_margin(
            underlying_price, short_strike, lot_size, lots, short_premium,
        )

        # Spread credit: difference in premiums
        spread_credit = (short_premium - long_premium) * lot_size * lots

        # Margin = max of legs, minus spread credit
        net_margin = max(long_result.net_margin, short_result.net_margin) - spread_credit
        net_margin = max(net_margin, 0)

        return MarginResult(
            initial_margin=max(long_result.initial_margin, short_result.initial_margin),
            span_margin=short_result.span_margin,
            exposure_margin=short_result.exposure_margin,
            premium_margin=long_result.premium_margin + short_result.premium_margin,
            net_margin=net_margin,
        )

    def calculate_structure_margin(
        self,
        intent: TradeIntent,
        premiums: dict[str, float],
        underlying_price: float,
        lot_size: int = 25,
    ) -> MarginResult:
        """Calculate margin for any structure from a TradeIntent.

        Parameters
        ----------
        intent:
            The trade intent.
        premiums:
            Premium per unit for each leg, keyed by trading_symbol.
        underlying_price:
            Current underlying price.
        lot_size:
            Default lot size.
        """
        total_margin = 0.0
        total_premium = 0.0

        for leg in intent.legs:
            premium = premiums.get(leg.trading_symbol, 0.0)
            units = leg.lot_size * leg.quantity

            if leg.side == "BUY":
                # Long: pay premium
                total_premium += premium * units
            else:
                # Short: need margin
                result = self.calculate_short_margin(
                    underlying_price, 0,  # strike unknown here
                    leg.lot_size, leg.quantity, premium,
                )
                total_margin += result.net_margin

        # For spreads, we'd ideally use spread margin, but this is a
        # reasonable approximation for V1
        net = total_margin + total_premium

        return MarginResult(
            initial_margin=net,
            net_margin=net,
            premium_margin=total_premium,
        )


# ---------------------------------------------------------------------------
# Pre-trade risk checks
# ---------------------------------------------------------------------------

@dataclass
class RiskCheckResult:
    """Result of a pre-trade risk check."""

    allowed: bool
    reason: str = ""
    details: dict[str, Any] | None = None

    def __bool__(self) -> bool:
        return self.allowed


class PreTradeRiskCheck:
    """Pre-trade risk checks for option orders.

    Parameters
    ----------
    max_margin:
        Maximum total margin the portfolio can use (₹).
    max_positions:
        Maximum number of open positions.
    max_loss_per_trade_pct:
        Maximum loss per trade as % of capital.
    max_single_position_pct:
        Maximum notional of a single position as % of capital.
    """

    def __init__(
        self,
        max_margin: float = 20_00_000,  # ₹20 lakh
        max_positions: int = 20,
        max_loss_per_trade_pct: float = 2.0,
        max_single_position_pct: float = 10.0,
        capital: float = 10_00_000,
    ) -> None:
        self.max_margin = max_margin
        self.max_positions = max_positions
        self.max_loss_per_trade_pct = max_loss_per_trade_pct
        self.max_single_position_pct = max_single_position_pct
        self.capital = capital

    def check(
        self,
        margin_required: float,
        current_margin_used: float = 0.0,
        current_positions: int = 0,
        trade_notional: float = 0.0,
    ) -> RiskCheckResult:
        """Run all pre-trade risk checks.

        Parameters
        ----------
        margin_required:
            Margin needed for the proposed trade.
        current_margin_used:
            Margin currently in use.
        current_positions:
            Number of currently open positions.
        trade_notional:
            Notional value of the proposed trade.

        Returns
        -------
        ``RiskCheckResult`` — ``True`` if all checks pass.
        """
        # Check 1: Margin capacity
        total_after = current_margin_used + margin_required
        if total_after > self.max_margin:
            return RiskCheckResult(
                allowed=False,
                reason=f"Margin limit exceeded: ₹{total_after:,.0f} > ₹{self.max_margin:,.0f}",
                details={
                    "requested": margin_required,
                    "available": self.max_margin - current_margin_used,
                },
            )

        # Check 2: Position count
        if current_positions >= self.max_positions:
            return RiskCheckResult(
                allowed=False,
                reason=f"Position limit reached: {current_positions} >= {self.max_positions}",
            )

        # Check 3: Single position notional
        max_notional = self.capital * self.max_single_position_pct / 100
        if trade_notional > max_notional:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Position too large: ₹{trade_notional:,.0f} > "
                    f"₹{max_notional:,.0f} ({self.max_single_position_pct}% of capital)"
                ),
            )

        # Check 4: Loss limit (margin as proxy for max loss)
        max_loss = self.capital * self.max_loss_per_trade_pct / 100
        if margin_required > max_loss:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Potential loss exceeds limit: ₹{margin_required:,.0f} > "
                    f"₹{max_loss:,.0f} ({self.max_loss_per_trade_pct}% of capital)"
                ),
            )

        return RiskCheckResult(allowed=True)
