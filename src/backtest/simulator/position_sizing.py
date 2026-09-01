"""Position sizing engine for forward testing (Step 14).

Implements six sizing methods plus constraints and risk parameters:

* Fixed quantity
* Fixed dollar amount
* Percentage of portfolio
* Risk-based (risk X% per trade with stop loss)
* Volatility-based / ATR-based
* Kelly Criterion (optimal growth, with fractional Kelly)

The engine is pure domain logic — no I/O, no DB, no broker calls — so it is
unit-testable and can be used both by ``StrategyAdapter`` and by the future
risk manager (Step 15).

Quick start
-----------
>>> from backtest.simulator.portfolio import Portfolio
>>> from backtest.simulator.position_sizing import PositionSizer, SizingConfig
>>> portfolio = Portfolio(name="test", initial_capital=100000)
>>> sizer = PositionSizer.from_config(
...     SizingConfig(method="risk_based", risk_per_trade=0.01, stop_loss_pct=0.02)
... )
>>> qty = sizer.calculate_position_size(symbol="INFY", current_price=1500, portfolio=portfolio)

Constraints are applied after the raw calculation:

* max position value / max position % per symbol
* max gross exposure % of equity
* min trade value (dust filter)
* round lots (e.g. NSE lot size)
* max open positions (delegates to portfolio.can_open_position)

All monetary values are :class:`Decimal` to match the rest of ``simulator/``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ONE, ZERO, money
from backtest.simulator.money import price as to_price
from backtest.simulator.money import quantize_money, to_decimal

logger = logging.getLogger("backtest.simulator.position_sizing")

__all__ = [
    "SizingMethod",
    "RiskParams",
    "SizingConstraints",
    "SizingConfig",
    "SizingResult",
    "PositionSizer",
    "FixedQuantitySizer",
    "FixedDollarSizer",
    "PercentagePortfolioSizer",
    "RiskBasedSizer",
    "VolatilitySizer",
    "ATRBasedSizer",
    "KellySizer",
    "all_in_size",
    "load_position_sizing_config",
    "DEFAULT_SIZING_CONFIG_PATH",
]

DEFAULT_SIZING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "position_sizing.yaml"

_DUST = Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Enums and configs
# ---------------------------------------------------------------------------


class SizingMethod:
    FIXED_QUANTITY = "fixed_quantity"
    FIXED_DOLLAR = "fixed_dollar"
    PERCENTAGE_PORTFOLIO = "percentage_portfolio"
    RISK_BASED = "risk_based"
    VOLATILITY = "volatility"
    ATR_BASED = "atr_based"
    KELLY = "kelly"

    ALL = (
        FIXED_QUANTITY,
        FIXED_DOLLAR,
        PERCENTAGE_PORTFOLIO,
        RISK_BASED,
        VOLATILITY,
        ATR_BASED,
        KELLY,
    )

    @classmethod
    def validate(cls, value: Any) -> str:
        v = str(value).strip().lower()
        # allow aliases
        aliases = {
            "fixed": cls.FIXED_QUANTITY,
            "fixed_qty": cls.FIXED_QUANTITY,
            "dollar": cls.FIXED_DOLLAR,
            "fixed_dollar_amount": cls.FIXED_DOLLAR,
            "percentage": cls.PERCENTAGE_PORTFOLIO,
            "percent": cls.PERCENTAGE_PORTFOLIO,
            "%": cls.PERCENTAGE_PORTFOLIO,
            "risk": cls.RISK_BASED,
            "risk_percentage": cls.RISK_BASED,
            "vol": cls.VOLATILITY,
            "atr": cls.ATR_BASED,
            "volatility_based": cls.VOLATILITY,
            "kelly_criterion": cls.KELLY,
        }
        if v in aliases:
            v = aliases[v]
        if v not in cls.ALL:
            raise ValidationError(f"unknown sizing method {value!r}; expected one of {cls.ALL}")
        return v


@dataclass
class RiskParams:
    """Risk parameters that drive sizing.

    Attributes
    ----------
    max_risk_per_trade:
        Fraction of equity to risk on one trade (e.g. 0.01 = 1%).
    stop_loss_pct:
        Expected loss if stop is hit, as fraction of entry price (e.g. 0.02 = 2%).
        Used by risk-based sizing.
    risk_amount:
        Absolute dollar amount to risk (alternative to max_risk_per_trade).
    atr_multiplier:
        Multiplier for ATR-based stops (e.g. 2x ATR).
    kelly_fraction:
        Fraction of full Kelly to use (0.5 = half-Kelly, safer).
    win_rate, avg_win, avg_loss:
        For Kelly: historical win rate 0..1, average win/loss in currency or %.
    """

    max_risk_per_trade: Optional[Decimal] = None
    stop_loss_pct: Optional[Decimal] = None
    risk_amount: Optional[Decimal] = None
    atr_multiplier: Decimal = Decimal("1")
    kelly_fraction: Decimal = Decimal("0.5")
    win_rate: Optional[Decimal] = None
    avg_win: Optional[Decimal] = None
    avg_loss: Optional[Decimal] = None
    max_total_risk: Optional[Decimal] = None
    max_concentration_pct: Optional[Decimal] = None

    def __post_init__(self) -> None:
        for name in (
            "max_risk_per_trade",
            "stop_loss_pct",
            "risk_amount",
            "max_total_risk",
            "max_concentration_pct",
        ):
            v = getattr(self, name)
            if v is not None:
                dec = to_decimal(v, name)
                if dec < ZERO:
                    raise ValidationError(f"{name} must not be negative")
                setattr(self, name, dec)

        self.atr_multiplier = to_decimal(self.atr_multiplier, "atr_multiplier")
        if self.atr_multiplier <= ZERO:
            raise ValidationError("atr_multiplier must be positive")

        self.kelly_fraction = to_decimal(self.kelly_fraction, "kelly_fraction")
        if self.kelly_fraction <= ZERO or self.kelly_fraction > ONE:
            raise ValidationError("kelly_fraction must be in (0, 1]")

        for name in ("win_rate", "avg_win", "avg_loss"):
            v = getattr(self, name)
            if v is not None:
                dec = to_decimal(v, name)
                if dec < ZERO:
                    raise ValidationError(f"{name} must not be negative")
                setattr(self, name, dec)

        if self.win_rate is not None and self.win_rate > ONE:
            raise ValidationError("win_rate must be between 0 and 1")

    def to_dict(self) -> Dict[str, Any]:
        def _s(v: Optional[Decimal]) -> Optional[str]:
            return str(v) if v is not None else None

        return {
            "max_risk_per_trade": _s(self.max_risk_per_trade),
            "stop_loss_pct": _s(self.stop_loss_pct),
            "risk_amount": _s(self.risk_amount),
            "atr_multiplier": str(self.atr_multiplier),
            "kelly_fraction": str(self.kelly_fraction),
            "win_rate": _s(self.win_rate),
            "avg_win": _s(self.avg_win),
            "avg_loss": _s(self.avg_loss),
            "max_total_risk": _s(self.max_total_risk),
            "max_concentration_pct": _s(self.max_concentration_pct),
        }


@dataclass
class SizingConstraints:
    """Hard limits applied after raw sizing.

    All limits are optional (None disables). Mirrors PortfolioLimits but
    focused on sizing decisions.

    Attributes
    ----------
    max_position_value:
        Absolute cap on notional per symbol.
    max_position_pct:
        Cap as fraction of equity (0.2 = 20%).
    max_gross_exposure_pct:
        Cap on sum of absolute exposures.
    min_trade_value:
        Reject dust trades below this notional.
    round_lots:
        Whether to round down to lot size.
    lot_size:
        Lot size for rounding (e.g. 1 for equities, 100 for NSE F&O).
    max_leverage:
        Leverage multiplier for buying power (1 = cash account).
    max_open_positions:
        Cap on concurrently open positions.
    """

    max_position_value: Optional[Decimal] = None
    max_position_pct: Optional[Decimal] = None
    max_gross_exposure_pct: Optional[Decimal] = None
    min_trade_value: Optional[Decimal] = None
    round_lots: bool = False
    lot_size: Decimal = Decimal("1")
    max_leverage: Decimal = Decimal("1")
    max_open_positions: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "max_position_value",
            "max_position_pct",
            "max_gross_exposure_pct",
            "min_trade_value",
        ):
            v = getattr(self, name)
            if v is not None:
                dec = to_decimal(v, name)
                if dec <= ZERO:
                    raise ValidationError(f"{name} must be positive when set")
                setattr(self, name, dec)

        self.lot_size = to_decimal(self.lot_size, "lot_size")
        if self.lot_size <= ZERO:
            raise ValidationError("lot_size must be positive")

        self.max_leverage = to_decimal(self.max_leverage, "max_leverage")
        if self.max_leverage < ONE:
            raise ValidationError("max_leverage must be >= 1")

        if self.max_open_positions is not None and self.max_open_positions < 1:
            raise ValidationError("max_open_positions must be >=1 when set")

    def to_dict(self) -> Dict[str, Any]:
        def _s(v: Optional[Decimal]) -> Optional[str]:
            return str(v) if v is not None else None

        return {
            "max_position_value": _s(self.max_position_value),
            "max_position_pct": _s(self.max_position_pct),
            "max_gross_exposure_pct": _s(self.max_gross_exposure_pct),
            "min_trade_value": _s(self.min_trade_value),
            "round_lots": self.round_lots,
            "lot_size": str(self.lot_size),
            "max_leverage": str(self.max_leverage),
            "max_open_positions": self.max_open_positions,
        }


@dataclass
class SizingConfig:
    """Full configuration for PositionSizer.

    Can be loaded from YAML via ``load_position_sizing_config``.
    """

    method: str = SizingMethod.FIXED_QUANTITY

    # method-specific params
    fixed_quantity: Decimal = Decimal("100")
    fixed_dollar_amount: Decimal = Decimal("10000")
    percentage: Decimal = Decimal("0.05")
    risk_per_trade: Decimal = Decimal("0.01")
    stop_loss_pct: Decimal = Decimal("0.02")
    atr: Optional[Decimal] = None
    atr_multiplier: Decimal = Decimal("2")
    risk_amount: Optional[Decimal] = None
    win_rate: Optional[Decimal] = None
    avg_win: Optional[Decimal] = None
    avg_loss: Optional[Decimal] = None
    kelly_fraction: Decimal = Decimal("0.5")

    # constraints
    constraints: SizingConstraints = field(default_factory=SizingConstraints)
    risk_params: RiskParams = field(default_factory=RiskParams)

    def __post_init__(self) -> None:
        self.method = SizingMethod.validate(self.method)

        self.fixed_quantity = to_price(self.fixed_quantity, "fixed_quantity")
        if self.fixed_quantity <= ZERO:
            raise ValidationError("fixed_quantity must be positive")

        self.fixed_dollar_amount = money(self.fixed_dollar_amount, "fixed_dollar_amount")
        if self.fixed_dollar_amount <= ZERO:
            raise ValidationError("fixed_dollar_amount must be positive")

        self.percentage = to_decimal(self.percentage, "percentage")
        if self.percentage <= ZERO or self.percentage > ONE:
            raise ValidationError("percentage must be in (0, 1]")

        self.risk_per_trade = to_decimal(self.risk_per_trade, "risk_per_trade")
        if self.risk_per_trade <= ZERO or self.risk_per_trade > ONE:
            raise ValidationError("risk_per_trade must be in (0, 1]")

        self.stop_loss_pct = to_decimal(self.stop_loss_pct, "stop_loss_pct")
        if self.stop_loss_pct <= ZERO or self.stop_loss_pct > ONE:
            raise ValidationError("stop_loss_pct must be in (0, 1]")

        if self.atr is not None:
            self.atr = to_price(self.atr, "atr")
            if self.atr <= ZERO:
                raise ValidationError("atr must be positive when set")

        self.atr_multiplier = to_decimal(self.atr_multiplier, "atr_multiplier")
        if self.atr_multiplier <= ZERO:
            raise ValidationError("atr_multiplier must be positive")

        if self.risk_amount is not None:
            self.risk_amount = money(self.risk_amount, "risk_amount")
            if self.risk_amount <= ZERO:
                raise ValidationError("risk_amount must be positive when set")

        self.kelly_fraction = to_decimal(self.kelly_fraction, "kelly_fraction")
        if self.kelly_fraction <= ZERO or self.kelly_fraction > ONE:
            raise ValidationError("kelly_fraction must be in (0, 1]")

        for name in ("win_rate", "avg_win", "avg_loss"):
            v = getattr(self, name)
            if v is not None:
                dec = to_decimal(v, name)
                if dec < ZERO:
                    raise ValidationError(f"{name} must not be negative")
                setattr(self, name, dec)

        if self.win_rate is not None and self.win_rate > ONE:
            raise ValidationError("win_rate must be between 0 and 1")

        if not isinstance(self.constraints, SizingConstraints):
            # allow dict
            if isinstance(self.constraints, dict):
                # pylint: disable-next=not-a-mapping  # narrowed to dict by the isinstance above
                self.constraints = SizingConstraints(**self.constraints)
            else:
                raise ValidationError("constraints must be SizingConstraints or dict")

        if not isinstance(self.risk_params, RiskParams):
            if isinstance(self.risk_params, dict):
                # pylint: disable-next=not-a-mapping  # narrowed to dict by the isinstance above
                self.risk_params = RiskParams(**self.risk_params)
            else:
                raise ValidationError("risk_params must be RiskParams or dict")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "fixed_quantity": str(self.fixed_quantity),
            "fixed_dollar_amount": str(self.fixed_dollar_amount),
            "percentage": str(self.percentage),
            "risk_per_trade": str(self.risk_per_trade),
            "stop_loss_pct": str(self.stop_loss_pct),
            "atr": str(self.atr) if self.atr else None,
            "atr_multiplier": str(self.atr_multiplier),
            "risk_amount": str(self.risk_amount) if self.risk_amount else None,
            "win_rate": str(self.win_rate) if self.win_rate else None,
            "avg_win": str(self.avg_win) if self.avg_win else None,
            "avg_loss": str(self.avg_loss) if self.avg_loss else None,
            "kelly_fraction": str(self.kelly_fraction),
            "constraints": self.constraints.to_dict(),
            "risk_params": self.risk_params.to_dict(),
        }


@dataclass(frozen=True)
class SizingResult:
    """Result of a sizing calculation with audit trail.

    Attributes
    ----------
    quantity:
        Final quantity after constraints and rounding.
    raw_quantity:
        Quantity before constraints.
    method:
        Sizing method used.
    current_price:
        Price used for calculation.
    notional:
        quantity * price.
    constrained:
        Whether any constraint capped the quantity.
    reason:
        Human-readable explanation.
    details:
        Extra info for logging (e.g. Kelly fraction, ATR value).
    """

    quantity: Decimal
    raw_quantity: Decimal
    method: str
    current_price: Decimal
    notional: Decimal = ZERO
    constrained: bool = False
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quantity": str(self.quantity),
            "raw_quantity": str(self.raw_quantity),
            "method": self.method,
            "current_price": str(self.current_price),
            "notional": str(self.notional),
            "constrained": self.constrained,
            "reason": self.reason,
            "details": dict(self.details),
        }


# ---------------------------------------------------------------------------
# Individual sizers (pure functions)
# ---------------------------------------------------------------------------


class FixedQuantitySizer:
    """Fixed quantity — always return same number of shares."""

    def __init__(self, quantity: Any = 100):
        self.quantity = to_price(quantity, "quantity")
        if self.quantity <= ZERO:
            raise ValidationError("fixed quantity must be positive")

    def calculate_position_size(
        self, signal: Any = None, portfolio: Any = None, current_price: Any = None, **kwargs: Any
    ) -> Decimal:
        return self.quantity

    def apply_fixed_quantity(self, quantity: Any) -> Decimal:
        q = to_price(quantity, "quantity")
        if q <= ZERO:
            raise ValidationError("quantity must be positive")
        self.quantity = q
        return self.quantity


class FixedDollarSizer:
    """Fixed dollar amount — size to $X at current price."""

    def __init__(self, dollar_amount: Any = 10000):
        self.dollar_amount = money(dollar_amount, "dollar_amount")
        if self.dollar_amount <= ZERO:
            raise ValidationError("dollar amount must be positive")

    def calculate_position_size(
        self, signal: Any = None, portfolio: Any = None, current_price: Any = None, **kwargs: Any
    ) -> Decimal:
        price = _resolve_price(current_price, signal, portfolio)
        if price <= ZERO:
            raise ValidationError("current_price must be positive")
        qty = (self.dollar_amount / price).quantize(Decimal("1"))
        return max(Decimal("1"), qty)

    def apply_fixed_dollar_amount(self, dollar_amount: Any) -> Decimal:
        amt = money(dollar_amount, "dollar_amount")
        if amt <= ZERO:
            raise ValidationError("dollar amount must be positive")
        self.dollar_amount = amt
        return self.dollar_amount


class PercentagePortfolioSizer:
    """Percentage of portfolio — e.g. 5% of equity per trade."""

    def __init__(self, percentage: Any = Decimal("0.05")):
        self.percentage = to_decimal(percentage, "percentage")
        if self.percentage <= ZERO or self.percentage > ONE:
            raise ValidationError("percentage must be in (0, 1]")

    def calculate_position_size(
        self, signal: Any = None, portfolio: Any = None, current_price: Any = None, **kwargs: Any
    ) -> Decimal:
        equity = _resolve_equity(portfolio)
        price = _resolve_price(current_price, signal, portfolio)
        if price <= ZERO:
            raise ValidationError("current_price must be positive")
        target_value = equity * self.percentage
        qty = (target_value / price).quantize(Decimal("1"))
        return max(Decimal("1"), qty)

    def apply_percentage_of_portfolio(self, percentage: Any) -> Decimal:
        pct = to_decimal(percentage, "percentage")
        if pct <= ZERO or pct > ONE:
            raise ValidationError("percentage must be in (0, 1]")
        self.percentage = pct
        return self.percentage


class RiskBasedSizer:
    """Risk-based: risk X% of portfolio with Y% stop loss.

    Formula: qty = (equity * risk_per_trade) / (price * stop_loss_pct)

    Example: equity 100k, risk 1% = 1k, stop 2% at price 100 => qty = 1000 / (100*0.02) = 500
    If stop is hit, loss = 500 * 100 * 0.02 = 1000 = 1% of equity.
    """

    def __init__(self, risk_per_trade: Any = Decimal("0.01"), stop_loss_pct: Any = Decimal("0.02")):
        self.risk_per_trade = to_decimal(risk_per_trade, "risk_per_trade")
        self.stop_loss_pct = to_decimal(stop_loss_pct, "stop_loss_pct")
        if self.risk_per_trade <= ZERO or self.risk_per_trade > ONE:
            raise ValidationError("risk_per_trade must be in (0, 1]")
        if self.stop_loss_pct <= ZERO or self.stop_loss_pct > ONE:
            raise ValidationError("stop_loss_pct must be in (0, 1]")

    def calculate_position_size(
        self,
        signal: Any = None,
        portfolio: Any = None,
        current_price: Any = None,
        risk_per_trade: Any = None,
        stop_loss_pct: Any = None,
        **kwargs: Any,
    ) -> Decimal:
        rp = (
            to_decimal(risk_per_trade, "risk_per_trade")
            if risk_per_trade is not None
            else self.risk_per_trade
        )
        sl = (
            to_decimal(stop_loss_pct, "stop_loss_pct")
            if stop_loss_pct is not None
            else self.stop_loss_pct
        )

        equity = _resolve_equity(portfolio)
        price = _resolve_price(current_price, signal, portfolio)

        if price <= ZERO:
            raise ValidationError("price must be positive")
        if sl <= ZERO:
            raise ValidationError("stop_loss_pct must be positive")

        risk_amount = equity * rp
        loss_per_share = price * sl
        qty = (risk_amount / loss_per_share).quantize(Decimal("1"))
        return max(Decimal("1"), qty)

    def apply_risk_percentage(self, risk_per_trade: Any, stop_loss_pct: Any) -> Decimal:
        self.risk_per_trade = to_decimal(risk_per_trade, "risk_per_trade")
        self.stop_loss_pct = to_decimal(stop_loss_pct, "stop_loss_pct")
        return self.risk_per_trade


class VolatilitySizer:
    """Volatility / ATR-based: qty = risk_amount / (ATR * multiplier)

    ATR measures volatility; higher ATR => smaller position to keep risk constant.
    """

    def __init__(
        self,
        risk_amount: Any = Decimal("1000"),
        atr_multiplier: Any = Decimal("1"),
        atr: Any = None,
    ):
        self.risk_amount = money(risk_amount, "risk_amount") if risk_amount is not None else None
        self.atr_multiplier = to_decimal(atr_multiplier, "atr_multiplier")
        if self.atr_multiplier <= ZERO:
            raise ValidationError("atr_multiplier must be positive")
        self.atr = to_price(atr, "atr") if atr is not None else None
        if self.atr is not None and self.atr <= ZERO:
            raise ValidationError("atr must be positive")

    def calculate_position_size(
        self,
        signal: Any = None,
        portfolio: Any = None,
        current_price: Any = None,
        atr: Any = None,
        risk_amount: Any = None,
        atr_multiplier: Any = None,
        **kwargs: Any,
    ) -> Decimal:
        # Priority: explicit atr param > signal indicators > instance atr
        effective_atr = None
        if atr is not None:
            effective_atr = to_price(atr, "atr")
        else:
            # try signal first (most current)
            if signal is not None:
                ind = getattr(signal, "indicators", {}) or {}
                if isinstance(ind, dict):
                    atr_val = ind.get("atr") or ind.get("ATR")
                    if atr_val is not None:
                        try:
                            effective_atr = to_price(atr_val, "atr")
                        except Exception:
                            pass
            # fallback to instance atr
            if effective_atr is None:
                effective_atr = self.atr

        if effective_atr is None or effective_atr <= ZERO:
            raise ValidationError("ATR is required for volatility-based sizing")

        mult = (
            to_decimal(atr_multiplier, "atr_multiplier")
            if atr_multiplier is not None
            else self.atr_multiplier
        )

        # risk amount: either absolute or % of equity
        if risk_amount is not None:
            risk_amt = money(risk_amount, "risk_amount")
        elif self.risk_amount is not None:
            risk_amt = self.risk_amount
        else:
            # default to 1% of equity
            equity = _resolve_equity(portfolio)
            risk_amt = equity * Decimal("0.01")

        denominator = effective_atr * mult
        qty = (risk_amt / denominator).quantize(Decimal("1"))
        return max(Decimal("1"), qty)

    def apply_volatility_based(
        self, atr: Any, risk_amount: Any, atr_multiplier: Any = Decimal("1")
    ) -> Decimal:
        self.atr = to_price(atr, "atr")
        self.risk_amount = money(risk_amount, "risk_amount")
        self.atr_multiplier = to_decimal(atr_multiplier, "atr_multiplier")
        return self.atr


# Alias for backward compatibility
ATRBasedSizer = VolatilitySizer


class KellySizer:
    """Kelly Criterion: optimal growth fraction.

    Formula: f* = p - (1-p)/b where b = avg_win/avg_loss
    Or: f* = (p*b - q)/b

    Position size = equity * f* * kelly_fraction / price

    Attributes
    ----------
    win_rate: 0..1
    avg_win, avg_loss: positive, same units (e.g. $ or %)
    kelly_fraction: 0..1, fraction of full Kelly (0.5 = half-Kelly, common practice)
    """

    def __init__(
        self,
        win_rate: Any = Decimal("0.55"),
        avg_win: Any = Decimal("100"),
        avg_loss: Any = Decimal("100"),
        kelly_fraction: Any = Decimal("0.5"),
    ):
        self.win_rate = to_decimal(win_rate, "win_rate")
        self.avg_win = to_decimal(avg_win, "avg_win")
        self.avg_loss = to_decimal(avg_loss, "avg_loss")
        self.kelly_fraction = to_decimal(kelly_fraction, "kelly_fraction")

        if not (ZERO <= self.win_rate <= ONE):
            raise ValidationError("win_rate must be between 0 and 1")
        if self.avg_win <= ZERO or self.avg_loss <= ZERO:
            raise ValidationError("avg_win and avg_loss must be positive")
        if not (ZERO < self.kelly_fraction <= ONE):
            raise ValidationError("kelly_fraction must be in (0, 1]")

    def _kelly_fraction_raw(
        self, win_rate: Decimal, avg_win: Decimal, avg_loss: Decimal
    ) -> Decimal:
        """Calculate raw Kelly fraction (can be negative)."""
        if avg_loss == ZERO:
            return ZERO
        b = avg_win / avg_loss  # odds
        if b == ZERO:
            return ZERO
        p = win_rate
        q = ONE - p
        # f* = (p*b - q)/b = p - q/b
        kelly = p - (q / b) if b != ZERO else ZERO
        return kelly

    def calculate_position_size(
        self,
        signal: Any = None,
        portfolio: Any = None,
        current_price: Any = None,
        win_rate: Any = None,
        avg_win: Any = None,
        avg_loss: Any = None,
        kelly_fraction: Any = None,
        **kwargs: Any,
    ) -> Decimal:
        wr = to_decimal(win_rate, "win_rate") if win_rate is not None else self.win_rate
        aw = to_decimal(avg_win, "avg_win") if avg_win is not None else self.avg_win
        al = to_decimal(avg_loss, "avg_loss") if avg_loss is not None else self.avg_loss
        kf = (
            to_decimal(kelly_fraction, "kelly_fraction")
            if kelly_fraction is not None
            else self.kelly_fraction
        )

        equity = _resolve_equity(portfolio)
        price = _resolve_price(current_price, signal, portfolio)

        if price <= ZERO:
            raise ValidationError("price must be positive")

        raw_kelly = self._kelly_fraction_raw(wr, aw, al)

        # Kelly can be negative (don't bet), cap at 0
        if raw_kelly <= ZERO:
            logger.info("Kelly raw fraction %.4f <=0, no bet", float(raw_kelly))
            return Decimal("0")

        # Apply fractional Kelly
        effective_kelly = raw_kelly * kf

        # Cap at 100% to avoid leverage unless explicitly allowed
        if effective_kelly > ONE:
            effective_kelly = ONE

        target_value = equity * effective_kelly
        qty = (target_value / price).quantize(Decimal("1"))
        return max(Decimal("1"), qty)

    def apply_kelly_criterion(
        self, win_rate: Any, avg_win: Any, avg_loss: Any, fraction: Any = Decimal("0.5")
    ) -> Decimal:
        self.win_rate = to_decimal(win_rate, "win_rate")
        self.avg_win = to_decimal(avg_win, "avg_win")
        self.avg_loss = to_decimal(avg_loss, "avg_loss")
        self.kelly_fraction = to_decimal(fraction, "kelly_fraction")
        raw = self._kelly_fraction_raw(self.win_rate, self.avg_win, self.avg_loss)
        return raw


# ---------------------------------------------------------------------------
# Composite sizer with constraints
# ---------------------------------------------------------------------------


class PositionSizer:
    """Main position sizing engine with constraints and logging.

    This is the class that ``StrategyAdapter`` uses. It delegates to one of
    the six methods based on ``SizingConfig.method``, then applies constraints.

    Example
    -------
    >>> config = SizingConfig(method="risk_based", risk_per_trade=0.01, stop_loss_pct=0.02,
    ...                       constraints=SizingConstraints(max_position_pct=0.2))
    >>> sizer = PositionSizer(config)
    >>> sizer.calculate_position_size(symbol="INFY", current_price=1500, portfolio=portfolio)
    Decimal('33')
    """

    def __init__(self, config: Optional[SizingConfig] = None):
        self.config = config or SizingConfig()
        if isinstance(self.config, dict):
            self.config = SizingConfig(**self.config)

        # build inner sizer based on method
        self._inner = self._build_inner(self.config)

    def _build_inner(self, config: SizingConfig):
        method = config.method
        if method == SizingMethod.FIXED_QUANTITY:
            return FixedQuantitySizer(quantity=config.fixed_quantity)
        elif method == SizingMethod.FIXED_DOLLAR:
            return FixedDollarSizer(dollar_amount=config.fixed_dollar_amount)
        elif method == SizingMethod.PERCENTAGE_PORTFOLIO:
            return PercentagePortfolioSizer(percentage=config.percentage)
        elif method == SizingMethod.RISK_BASED:
            return RiskBasedSizer(
                risk_per_trade=config.risk_per_trade, stop_loss_pct=config.stop_loss_pct
            )
        elif method in (SizingMethod.VOLATILITY, SizingMethod.ATR_BASED):
            return VolatilitySizer(
                risk_amount=config.risk_amount or config.fixed_dollar_amount,
                atr_multiplier=config.atr_multiplier,
                atr=config.atr,
            )
        elif method == SizingMethod.KELLY:
            return KellySizer(
                win_rate=config.win_rate or Decimal("0.55"),
                avg_win=config.avg_win or Decimal("100"),
                avg_loss=config.avg_loss or Decimal("100"),
                kelly_fraction=config.kelly_fraction,
            )
        else:
            raise ValidationError(f"unsupported method {method}")

    # -- public API required by spec ---------------------------------------

    def calculate_position_size(
        self,
        signal: Any = None,
        portfolio: Any = None,
        risk_params: Optional[Mapping[str, Any] | RiskParams] = None,
        current_price: Any = None,
        symbol: Optional[str] = None,
        **kwargs: Any,
    ) -> Decimal:
        """Calculate position size with constraints.

        Parameters
        ----------
        signal:
            Signal object or dict with symbol, indicators, etc.
        portfolio:
            Portfolio for equity and exposure checks.
        risk_params:
            Override risk parameters (dict or RiskParams).
        current_price:
            Current price, overrides signal's close.
        symbol:
            Symbol, overrides signal's symbol.

        Returns
        -------
        Decimal
            Final quantity after constraints, rounded to lot size if enabled.
        """
        # Resolve symbol and price
        sym = symbol
        if sym is None and signal is not None:
            sym = getattr(signal, "symbol", None) or (
                signal.get("symbol") if isinstance(signal, dict) else None
            )

        price = _resolve_price(current_price, signal, portfolio)

        # Merge risk_params overrides
        if risk_params is not None:
            if isinstance(risk_params, RiskParams):
                rp = risk_params
            elif isinstance(risk_params, dict):
                # build from dict, merging with existing
                rp_dict = self.config.risk_params.to_dict()
                rp_dict.update({k: v for k, v in risk_params.items() if v is not None})
                # remove None string values that to_dict produced as None?
                # Actually to_dict returns str or None
                # Let's build RiskParams directly from merged dict filtering
                merged = {}
                for k in (f.name for f in fields(RiskParams)):
                    if k in risk_params and risk_params[k] is not None:
                        merged[k] = risk_params[k]
                    else:
                        # get from config.risk_params
                        val = getattr(self.config.risk_params, k)
                        if val is not None:
                            merged[k] = val
                # also handle atr_multiplier separately
                if "atr_multiplier" in risk_params:
                    merged["atr_multiplier"] = risk_params["atr_multiplier"]
                try:
                    rp = RiskParams(**merged)
                except Exception:
                    rp = self.config.risk_params
            else:
                rp = self.config.risk_params

            # If risk_params overrides contain sizing-relevant fields,
            # rebuild inner sizer temporarily?
            # For simplicity, pass them as kwargs to inner calculate
            kwargs.update(
                {
                    "risk_per_trade": getattr(rp, "max_risk_per_trade", None)
                    or getattr(rp, "risk_per_trade", None),
                    "stop_loss_pct": getattr(rp, "stop_loss_pct", None),
                    "risk_amount": getattr(rp, "risk_amount", None),
                    "atr_multiplier": getattr(rp, "atr_multiplier", None),
                    "win_rate": getattr(rp, "win_rate", None),
                    "avg_win": getattr(rp, "avg_win", None),
                    "avg_loss": getattr(rp, "avg_loss", None),
                    "kelly_fraction": getattr(rp, "kelly_fraction", None),
                }
            )
            # clean None
            kwargs = {k: v for k, v in kwargs.items() if v is not None}

        # Also allow explicit overrides from kwargs for ATR etc.
        if "atr" in kwargs and kwargs["atr"] is None:
            kwargs.pop("atr")

        # Raw calculation
        try:
            raw_qty = self._inner.calculate_position_size(
                signal=signal, portfolio=portfolio, current_price=price, **kwargs
            )
        except Exception as exc:
            logger.warning("sizer %s failed: %s, falling back to fixed 1", self.config.method, exc)
            raw_qty = Decimal("1")

        raw_qty = to_price(raw_qty, "raw_quantity")

        # Apply constraints
        result = self._apply_constraints(
            raw_qty=raw_qty, price=price, symbol=sym, portfolio=portfolio, signal=signal
        )

        logger.info(
            "sizing %s %s: raw=%s final=%s price=%s notional=%s constrained=%s reason=%s",
            self.config.method,
            sym or "?",
            result.raw_quantity,
            result.quantity,
            price,
            result.notional,
            result.constrained,
            result.reason,
        )

        return result.quantity

    def calculate_with_details(
        self,
        signal: Any = None,
        portfolio: Any = None,
        current_price: Any = None,
        symbol: Optional[str] = None,
        **kwargs: Any,
    ) -> SizingResult:
        """Like calculate_position_size but returns full SizingResult with audit."""
        sym = symbol
        if sym is None and signal is not None:
            sym = getattr(signal, "symbol", None) or (
                signal.get("symbol") if isinstance(signal, dict) else None
            )

        price = _resolve_price(current_price, signal, portfolio)
        raw_qty = self._inner.calculate_position_size(
            signal=signal, portfolio=portfolio, current_price=price, **kwargs
        )
        raw_qty = to_price(raw_qty, "raw_quantity")
        result = self._apply_constraints(
            raw_qty=raw_qty, price=price, symbol=sym, portfolio=portfolio, signal=signal
        )
        return result

    def _apply_constraints(
        self,
        raw_qty: Decimal,
        price: Decimal,
        symbol: Optional[str],
        portfolio: Any,
        signal: Any = None,
    ) -> SizingResult:
        constraints = self.config.constraints
        constrained = False
        reason_parts = []
        qty = raw_qty

        # Round lots
        if constraints.round_lots:
            lot = constraints.lot_size
            if lot > ZERO:
                # floor to nearest lot
                qty = (qty // lot) * lot
                if qty != raw_qty:
                    constrained = True
                    reason_parts.append(f"rounded to lot {lot}")

        # Min trade value
        if constraints.min_trade_value is not None:
            notional = qty * price
            if notional < constraints.min_trade_value:
                # If below min, either return 0 (skip) or min?
                # For now, if raw notional < min, return 0 to avoid dust
                if raw_qty * price < constraints.min_trade_value:
                    return SizingResult(
                        quantity=Decimal("0"),
                        raw_quantity=raw_qty,
                        method=self.config.method,
                        current_price=price,
                        notional=Decimal("0"),
                        constrained=True,
                        reason=f"below min_trade_value {constraints.min_trade_value}",
                        details={"min_trade_value": str(constraints.min_trade_value)},
                    )
                else:
                    # if rounding caused it to go below min, restore min
                    min_qty = (constraints.min_trade_value / price).quantize(Decimal("1"))
                    if constraints.round_lots:
                        min_qty = (min_qty // constraints.lot_size) * constraints.lot_size
                        if min_qty == ZERO:
                            min_qty = constraints.lot_size
                    qty = max(qty, min_qty)

        # Max position value
        if constraints.max_position_value is not None:
            max_qty_by_value = (constraints.max_position_value / price).quantize(Decimal("1"))
            if constraints.round_lots:
                max_qty_by_value = (max_qty_by_value // constraints.lot_size) * constraints.lot_size
            if qty > max_qty_by_value:
                qty = max_qty_by_value
                constrained = True
                reason_parts.append(
                    f"capped to max_position_value {constraints.max_position_value}"
                )

        # Max position % of equity
        if constraints.max_position_pct is not None and portfolio is not None:
            try:
                equity = _resolve_equity(portfolio)
                max_value = equity * constraints.max_position_pct
                max_qty_by_pct = (max_value / price).quantize(Decimal("1"))
                if constraints.round_lots:
                    max_qty_by_pct = (max_qty_by_pct // constraints.lot_size) * constraints.lot_size
                if qty > max_qty_by_pct:
                    qty = max_qty_by_pct
                    constrained = True
                    reason_parts.append(
                        f"capped to max_position_pct {constraints.max_position_pct}"
                    )
            except Exception as exc:
                logger.debug("max_position_pct check failed: %s", exc)

        # Max gross exposure
        if constraints.max_gross_exposure_pct is not None and portfolio is not None and symbol:
            try:
                equity = _resolve_equity(portfolio)
                gross = (
                    portfolio.calculate_gross_exposure()
                    if hasattr(portfolio, "calculate_gross_exposure")
                    else ZERO
                )
                max_gross = equity * constraints.max_gross_exposure_pct
                # If already over, no new position
                if gross >= max_gross:
                    return SizingResult(
                        quantity=Decimal("0"),
                        raw_quantity=raw_qty,
                        method=self.config.method,
                        current_price=price,
                        notional=Decimal("0"),
                        constrained=True,
                        reason=f"gross exposure {gross} >= max {max_gross}",
                        details={"gross": str(gross), "max_gross": str(max_gross)},
                    )
                remaining = max_gross - gross
                max_qty_by_gross = (remaining / price).quantize(Decimal("1"))
                if constraints.round_lots:
                    max_qty_by_gross = (
                        max_qty_by_gross // constraints.lot_size
                    ) * constraints.lot_size
                if qty > max_qty_by_gross:
                    qty = max_qty_by_gross
                    constrained = True
                    reason_parts.append(
                        f"capped to max_gross_exposure_pct {constraints.max_gross_exposure_pct}"
                    )
            except Exception as exc:
                logger.debug("max_gross_exposure check failed: %s", exc)

        # Max open positions
        if constraints.max_open_positions is not None and portfolio is not None and symbol:
            try:
                if hasattr(portfolio, "has_position") and not portfolio.has_position(symbol):
                    if len(portfolio.positions) >= constraints.max_open_positions:
                        return SizingResult(
                            quantity=Decimal("0"),
                            raw_quantity=raw_qty,
                            method=self.config.method,
                            current_price=price,
                            notional=Decimal("0"),
                            constrained=True,
                            reason=f"max_open_positions {constraints.max_open_positions} reached",
                        )
            except Exception as exc:
                logger.debug("max_open_positions check failed: %s", exc)

        # Ensure not negative
        if qty < ZERO:
            qty = ZERO

        notional = quantize_money(qty * price)

        return SizingResult(
            quantity=qty,
            raw_quantity=raw_qty,
            method=self.config.method,
            current_price=price,
            notional=notional,
            constrained=constrained,
            reason="; ".join(reason_parts) if reason_parts else "ok",
            details={
                "symbol": symbol,
                "constraints": constraints.to_dict(),
            },
        )

    # -- spec-required helpers (apply_*) -----------------------------------

    def apply_fixed_quantity(self, quantity: Any) -> Decimal:
        self.config = SizingConfig(
            method=SizingMethod.FIXED_QUANTITY,
            fixed_quantity=quantity,
            constraints=self.config.constraints,
        )
        self._inner = FixedQuantitySizer(quantity=quantity)
        return self._inner.quantity

    def apply_fixed_dollar_amount(self, dollar_amount: Any) -> Decimal:
        self.config = SizingConfig(
            method=SizingMethod.FIXED_DOLLAR,
            fixed_dollar_amount=dollar_amount,
            constraints=self.config.constraints,
        )
        self._inner = FixedDollarSizer(dollar_amount=dollar_amount)
        return self._inner.dollar_amount

    def apply_percentage_of_portfolio(self, percentage: Any) -> Decimal:
        self.config = SizingConfig(
            method=SizingMethod.PERCENTAGE_PORTFOLIO,
            percentage=percentage,
            constraints=self.config.constraints,
        )
        self._inner = PercentagePortfolioSizer(percentage=percentage)
        return self._inner.percentage

    def apply_risk_percentage(self, risk_per_trade: Any, stop_loss_pct: Any) -> Decimal:
        self.config = SizingConfig(
            method=SizingMethod.RISK_BASED,
            risk_per_trade=risk_per_trade,
            stop_loss_pct=stop_loss_pct,
            constraints=self.config.constraints,
        )
        self._inner = RiskBasedSizer(risk_per_trade=risk_per_trade, stop_loss_pct=stop_loss_pct)
        return self._inner.risk_per_trade

    def apply_kelly_criterion(
        self, win_rate: Any, avg_win: Any, avg_loss: Any, fraction: Any = Decimal("0.5")
    ) -> Decimal:
        self.config = SizingConfig(
            method=SizingMethod.KELLY,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            kelly_fraction=fraction,
            constraints=self.config.constraints,
        )
        self._inner = KellySizer(
            win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss, kelly_fraction=fraction
        )
        return self._inner._kelly_fraction_raw(
            to_decimal(win_rate, "win_rate"),
            to_decimal(avg_win, "avg_win"),
            to_decimal(avg_loss, "avg_loss"),
        )

    def apply_volatility_based(
        self, atr: Any, risk_amount: Any, atr_multiplier: Any = Decimal("1")
    ) -> Decimal:
        self.config = SizingConfig(
            method=SizingMethod.ATR_BASED,
            atr=atr,
            risk_amount=risk_amount,
            atr_multiplier=atr_multiplier,
            constraints=self.config.constraints,
        )
        self._inner = VolatilitySizer(
            risk_amount=risk_amount, atr=atr, atr_multiplier=atr_multiplier
        )
        return self._inner.atr

    # -- factory -----------------------------------------------------------

    @classmethod
    def from_config(cls, config: SizingConfig | Mapping[str, Any] | str | Path) -> "PositionSizer":
        if isinstance(config, (str, Path)):
            cfg = load_position_sizing_config(config)
            return cls(cfg)
        if isinstance(config, dict):
            return cls(SizingConfig(**config))
        return cls(config)

    def to_dict(self) -> Dict[str, Any]:
        return self.config.to_dict()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_price(current_price: Any, signal: Any, portfolio: Any) -> Decimal:
    """Resolve current price from many sources."""
    # explicit price
    if current_price is not None:
        try:
            return to_price(current_price, "current_price")
        except Exception:
            pass

    # from signal
    if signal is not None:
        # signal could be Signal dataclass
        if hasattr(signal, "indicators") and isinstance(signal.indicators, dict):
            for key in ("close", "last", "price", "ask", "bid"):
                val = signal.indicators.get(key)
                if val is not None:
                    try:
                        return to_price(val, "price")
                    except Exception:
                        continue
        if hasattr(signal, "limit_price") and signal.limit_price is not None:
            try:
                return to_price(signal.limit_price, "limit_price")
            except Exception:
                pass
        # dict signal
        if isinstance(signal, dict):
            for key in ("close", "last", "price", "limit_price"):
                if key in signal and signal[key] is not None:
                    try:
                        return to_price(signal[key], key)
                    except Exception:
                        continue
            ind = signal.get("indicators", {})
            if isinstance(ind, dict):
                for key in ("close", "last", "price"):
                    if key in ind and ind[key] is not None:
                        try:
                            return to_price(ind[key], key)
                        except Exception:
                            continue

    # from portfolio? no price there
    # fallback
    return to_price(100, "price")


def _resolve_equity(portfolio: Any) -> Decimal:
    """Resolve equity from portfolio or default."""
    if portfolio is None:
        return money(100000)
    try:
        if hasattr(portfolio, "calculate_total_equity"):
            return money(portfolio.calculate_total_equity())
        if hasattr(portfolio, "current_cash"):
            return money(portfolio.current_cash)
    except Exception:
        pass
    return money(100000)


def all_in_size(symbol: str, price: float, portfolio: Any) -> int:
    """Size an entry with the whole bucket (walk-forward convention).

    A 2% safety haircut keeps the next-bar-open fill funded: sizing uses
    this bar's price but the order trades at the NEXT bar's open, and an
    upward gap must not push the notional past the available cash.
    Canonical home (ticket #6); ``backtest.forward.paper_runner`` re-exports
    the historical private name.
    """
    if price <= 0:
        return 0
    equity = float(portfolio.calculate_total_equity())
    return int(equity * 0.98 / price)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_position_sizing_config(
    path: str | Path | None = None, profile: str | None = None
) -> SizingConfig:
    """Load sizing config from YAML.

    Falls back to default fixed quantity when file absent.

    YAML shape:

    .. code-block:: yaml

        active_profile: conservative
        default:
          method: fixed_quantity
          fixed_quantity: 100
          constraints:
            max_position_pct: 0.2
            min_trade_value: 5000
            round_lots: false
            lot_size: 1
        profiles:
          conservative:
            method: risk_based
            risk_per_trade: 0.01
            stop_loss_pct: 0.02
            constraints:
              max_position_pct: 0.1
          aggressive:
            method: kelly
            win_rate: 0.55
            avg_win: 150
            avg_loss: 100
            kelly_fraction: 0.5
    """
    config_path = Path(path) if path else DEFAULT_SIZING_CONFIG_PATH

    if path is not None and not config_path.exists():
        raise ValidationError(f"sizing config not found: {config_path}")

    if not config_path.exists():
        return SizingConfig()

    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise ValidationError(f"{config_path} exists but PyYAML not installed") from exc

    try:
        doc = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:
        raise ValidationError(f"could not parse {config_path}: {exc}") from exc

    merged: Dict[str, Any] = dict(doc.get("default") or {})
    profiles = doc.get("profiles") or {}
    chosen = profile or doc.get("active_profile") or "default"

    if profiles:
        if chosen not in profiles and chosen != "default":
            raise ValidationError(
                f"unknown sizing profile {chosen!r}; available: {sorted(profiles)}"
            )
        if chosen in profiles:
            merged.update(profiles[chosen] or {})

    # handle nested constraints and risk_params
    constraints_data = merged.pop("constraints", None)
    risk_params_data = merged.pop("risk_params", None)

    # known fields
    known = {f.name for f in fields(SizingConfig)}
    # allow constraints and risk_params as nested, already popped
    unknown = set(merged) - known
    if unknown:
        # allow some aliases
        # e.g. fixed_dollar_amount vs dollar_amount
        if "dollar_amount" in unknown:
            merged["fixed_dollar_amount"] = merged.pop("dollar_amount")
            unknown = set(merged) - known

    if unknown:
        raise ValidationError(f"unknown sizing config keys: {sorted(unknown)}")

    if constraints_data:
        merged["constraints"] = (
            SizingConstraints(**constraints_data)
            if isinstance(constraints_data, dict)
            else constraints_data
        )
    if risk_params_data:
        merged["risk_params"] = (
            RiskParams(**risk_params_data)
            if isinstance(risk_params_data, dict)
            else risk_params_data
        )

    return SizingConfig(**merged)
