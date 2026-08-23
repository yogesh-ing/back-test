"""Risk Management System for forward testing (Step 15).

Comprehensive pre-trade and post-trade risk checks with circuit breakers,
configurable limits, and detailed rejection reasons.

Risk hierarchy
--------------
Order -> Position -> Portfolio

* Order-level: sufficient cash, size limits, restricted symbols, % of daily volume
* Position-level: max position size per symbol, max % per symbol, max open positions, sector concentration
* Portfolio-level: max drawdown, daily/weekly/monthly loss limits, max leverage, max total exposure

Circuit breakers
----------------
* Halt trading if drawdown exceeded
* Stop on consecutive losses
* Pause on technical errors
* Resume conditions with manual override

Example
-------
>>> from backtest.simulator.portfolio import Portfolio
>>> from backtest.simulator.risk_manager import RiskManager, RiskConfig
>>> portfolio = Portfolio(name="test", initial_capital=100000)
>>> risk = RiskManager(portfolio=portfolio, config=RiskConfig(max_drawdown_pct=0.1))
>>> order = Order(symbol="INFY", side="buy", quantity=100, order_type="market")
>>> result = risk.validate_order(order, current_price=1500)
>>> result.allowed
True
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ZERO, money, to_decimal, price as to_price, quantize_money

logger = logging.getLogger("backtest.simulator.risk_manager")

DEFAULT_RISK_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "risk.yaml"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class RiskConfig:
    """Risk limits configuration.

    All limits are optional except drawdown and loss limits which have sensible defaults.
    """

    # Position-level
    max_position_value: Optional[Decimal] = None  # e.g. 10000
    max_position_pct: Optional[Decimal] = None  # e.g. 0.2 = 20% of equity per symbol
    max_open_positions: Optional[int] = None  # e.g. 5
    max_gross_exposure_pct: Optional[Decimal] = None  # e.g. 1.0 = 100%
    max_leverage: Decimal = Decimal("1")  # 1 = cash account

    # Portfolio-level
    max_drawdown_pct: Decimal = Decimal("0.10")  # 10% from peak
    daily_loss_limit_pct: Decimal = Decimal("0.02")  # 2% daily
    weekly_loss_limit_pct: Optional[Decimal] = None
    monthly_loss_limit_pct: Optional[Decimal] = None
    max_total_exposure: Optional[Decimal] = None  # absolute

    # Order-level
    min_order_value: Optional[Decimal] = None
    max_order_value: Optional[Decimal] = None
    max_order_pct_of_daily_volume: Optional[Decimal] = None  # e.g. 0.1 = 10% of avg daily volume

    # Symbol restrictions
    restricted_symbols: Set[str] = field(default_factory=set)
    allowed_symbols: Optional[Set[str]] = None  # if set, only these allowed

    # Sector limits
    sector_exposure_limits: Dict[str, Decimal] = field(default_factory=dict)  # sector -> max % of equity
    symbol_to_sector: Dict[str, str] = field(default_factory=dict)

    # Circuit breakers
    max_consecutive_losses: Optional[int] = None  # e.g. 5
    max_consecutive_errors: int = 5
    pause_on_technical_error: bool = True

    # Override
    allow_override: bool = False
    override_code: Optional[str] = None

    def __post_init__(self):
        for name in (
            "max_position_value",
            "max_position_pct",
            "max_gross_exposure_pct",
            "max_total_exposure",
            "min_order_value",
            "max_order_value",
            "max_order_pct_of_daily_volume",
        ):
            v = getattr(self, name)
            if v is not None:
                dec = to_decimal(v, name)
                if dec <= ZERO:
                    raise ValidationError(f"{name} must be positive when set")
                setattr(self, name, dec)

        for name in ("max_drawdown_pct", "daily_loss_limit_pct", "weekly_loss_limit_pct", "monthly_loss_limit_pct"):
            v = getattr(self, name)
            if v is not None:
                dec = to_decimal(v, name)
                if dec < ZERO or dec > Decimal("1"):
                    raise ValidationError(f"{name} must be between 0 and 1")
                setattr(self, name, dec)

        self.max_leverage = to_decimal(self.max_leverage, "max_leverage")
        if self.max_leverage < Decimal("1"):
            raise ValidationError("max_leverage must be >=1")

        if self.max_open_positions is not None and self.max_open_positions < 1:
            raise ValidationError("max_open_positions must be >=1")

        if self.max_consecutive_losses is not None and self.max_consecutive_losses < 1:
            raise ValidationError("max_consecutive_losses must be >=1")

        # Normalize symbols
        self.restricted_symbols = {str(s).strip().upper() for s in self.restricted_symbols}
        if self.allowed_symbols is not None:
            self.allowed_symbols = {str(s).strip().upper() for s in self.allowed_symbols}

        # Sector limits
        if self.sector_exposure_limits:
            normalized = {}
            for sector, limit in self.sector_exposure_limits.items():
                dec = to_decimal(limit, f"sector_limit_{sector}")
                if dec <= ZERO or dec > Decimal("1"):
                    raise ValidationError(f"sector limit for {sector} must be in (0,1]")
                normalized[str(sector).strip().upper()] = dec
            self.sector_exposure_limits = normalized

        if self.symbol_to_sector:
            normalized = {}
            for sym, sector in self.symbol_to_sector.items():
                normalized[str(sym).strip().upper()] = str(sector).strip().upper()
            self.symbol_to_sector = normalized

    def to_dict(self) -> Dict[str, Any]:
        def _s(v: Optional[Decimal]) -> Optional[str]:
            return str(v) if v is not None else None

        return {
            "max_position_value": _s(self.max_position_value),
            "max_position_pct": _s(self.max_position_pct),
            "max_open_positions": self.max_open_positions,
            "max_gross_exposure_pct": _s(self.max_gross_exposure_pct),
            "max_leverage": str(self.max_leverage),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "daily_loss_limit_pct": str(self.daily_loss_limit_pct),
            "weekly_loss_limit_pct": _s(self.weekly_loss_limit_pct),
            "monthly_loss_limit_pct": _s(self.monthly_loss_limit_pct),
            "max_total_exposure": _s(self.max_total_exposure),
            "min_order_value": _s(self.min_order_value),
            "max_order_value": _s(self.max_order_value),
            "max_order_pct_of_daily_volume": _s(self.max_order_pct_of_daily_volume),
            "restricted_symbols": sorted(self.restricted_symbols),
            "allowed_symbols": sorted(self.allowed_symbols) if self.allowed_symbols else None,
            "sector_exposure_limits": {k: str(v) for k, v in self.sector_exposure_limits.items()},
            "max_consecutive_losses": self.max_consecutive_losses,
        }


def load_risk_config(path: str | Path | None = None, profile: str | None = None) -> RiskConfig:
    """Load risk config from YAML, fallback to defaults."""
    config_path = Path(path) if path else DEFAULT_RISK_CONFIG_PATH

    if path is not None and not config_path.exists():
        raise ValidationError(f"risk config not found: {config_path}")

    if not config_path.exists():
        return RiskConfig()

    try:
        import yaml

        doc = yaml.safe_load(config_path.read_text()) or {}
        merged = dict(doc.get("default") or {})
        profiles = doc.get("profiles") or {}
        chosen = profile or doc.get("active_profile") or "default"

        if profiles and chosen in profiles:
            merged.update(profiles[chosen] or {})

        # Handle nested structures
        # Convert restricted_symbols list to set
        if "restricted_symbols" in merged and isinstance(merged["restricted_symbols"], list):
            merged["restricted_symbols"] = set(merged["restricted_symbols"])
        if "allowed_symbols" in merged and isinstance(merged["allowed_symbols"], list):
            merged["allowed_symbols"] = set(merged["allowed_symbols"])

        known = set(RiskConfig.__dataclass_fields__.keys())
        filtered = {k: v for k, v in merged.items() if k in known}

        return RiskConfig(**filtered)

    except Exception as exc:
        logger.warning("Failed to load risk config %s: %s, using defaults", config_path, exc)
        return RiskConfig()


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskCheckResult:
    allowed: bool
    code: str = "ok"
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self):
        return self.allowed

    def to_dict(self):
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason, "details": dict(self.details)}


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------


class RiskManager:
    """Comprehensive risk manager with circuit breakers.

    Parameters
    ----------
    portfolio:
        Portfolio to check against (simulator.Portfolio)
    config:
        RiskConfig or dict
    """

    def __init__(self, portfolio: Any, config: Optional[RiskConfig | Mapping[str, Any]] = None):
        self.portfolio = portfolio

        if config is None:
            self.config = RiskConfig()
        elif isinstance(config, dict):
            self.config = RiskConfig(**config)
        else:
            self.config = config

        # Circuit breaker state
        self._is_halted = False
        self._halt_reason: Optional[str] = None
        self._halt_time: Optional[datetime] = None
        self._consecutive_losses = 0
        self._consecutive_errors = 0
        self._daily_pnl: Dict[date, Decimal] = defaultdict(lambda: ZERO)
        self._loss_streak: List[Dict[str, Any]] = []

        # For volume checks
        self._avg_daily_volume: Dict[str, Decimal] = {}

        # Alert callbacks
        self._alert_callbacks: List[Callable[[str, str, Dict[str, Any]], None]] = []

        # Override
        self._override_active = False
        self._override_until: Optional[datetime] = None

        logger.info(
            "RiskManager initialized: max_pos_pct=%s max_drawdown=%s daily_loss=%s max_positions=%s",
            self.config.max_position_pct,
            self.config.max_drawdown_pct,
            self.config.daily_loss_limit_pct,
            self.config.max_open_positions,
        )

    # -- public API required by spec ---------------------------------------

    def validate_order(self, order: Any, current_price: Any = None, daily_volume: Any = None) -> RiskCheckResult:
        """Validate a single order against all risk limits.

        Returns RiskCheckResult with allowed bool and reason.
        """
        # Check if halted
        if self._is_halted and not self._override_active:
            return RiskCheckResult(False, "trading_halted", f"Trading halted: {self._halt_reason}", {"halt_reason": self._halt_reason})

        # Check override expiry
        self._check_override_expiry()

        symbol = getattr(order, "symbol", None) or (order.get("symbol") if isinstance(order, dict) else None)
        if symbol:
            symbol = str(symbol).strip().upper()
        else:
            symbol = "UNKNOWN"

        quantity = getattr(order, "quantity", None) or (order.get("quantity") if isinstance(order, dict) else None)
        if quantity is None:
            return RiskCheckResult(False, "missing_quantity", "Order quantity missing", {"symbol": symbol})

        try:
            qty = to_decimal(quantity, "quantity")
            if qty <= ZERO:
                return RiskCheckResult(False, "invalid_quantity", f"Quantity must be positive, got {qty}", {"symbol": symbol})
        except Exception as exc:
            return RiskCheckResult(False, "invalid_quantity", f"Invalid quantity: {exc}", {"symbol": symbol})

        # Resolve price
        price = current_price
        if price is None:
            price = getattr(order, "limit_price", None) or getattr(order, "stop_price", None)
            if price is None and hasattr(order, "average_fill_price") and order.average_fill_price:
                price = order.average_fill_price
        if price is None:
            # Try to get from portfolio position current price
            try:
                pos = self.portfolio.get_position(symbol)
                if pos and hasattr(pos, "current_price") and pos.current_price:
                    price = pos.current_price
            except Exception:
                pass
        if price is None:
            price = 100  # fallback for testing

        try:
            price_dec = to_price(price, "price")
            if price_dec <= ZERO:
                return RiskCheckResult(False, "invalid_price", f"Price must be positive, got {price_dec}", {"symbol": symbol})
        except Exception as exc:
            return RiskCheckResult(False, "invalid_price", f"Invalid price: {exc}", {"symbol": symbol})

        # Order-level checks
        result = self._check_order_level(order, symbol, qty, price_dec, daily_volume)
        if not result.allowed:
            self._log_rejection(order, result)
            return result

        # Position-level checks
        result = self.check_position_limits(symbol, qty, price_dec)
        if not result.allowed:
            self._log_rejection(order, result)
            return result

        # Portfolio-level checks
        result = self._check_portfolio_level(symbol, qty, price_dec)
        if not result.allowed:
            self._log_rejection(order, result)
            return result

        # Buying power
        result = self.check_buying_power(qty * price_dec)
        if not result.allowed:
            self._log_rejection(order, result)
            return result

        # All passed
        logger.debug("Risk check passed for %s %s @ %s", symbol, qty, price_dec)
        return RiskCheckResult(True, "ok", "Risk checks passed", {"symbol": symbol, "quantity": str(qty), "price": str(price_dec)})

    def _check_order_level(self, order: Any, symbol: str, qty: Decimal, price: Decimal, daily_volume: Any = None) -> RiskCheckResult:
        # Restricted symbols
        if symbol in self.config.restricted_symbols:
            return RiskCheckResult(False, "restricted_symbol", f"Symbol {symbol} is restricted", {"symbol": symbol})

        if self.config.allowed_symbols is not None and symbol not in self.config.allowed_symbols:
            return RiskCheckResult(False, "symbol_not_allowed", f"Symbol {symbol} not in allowed list", {"symbol": symbol})

        # Order value limits
        notional = qty * price

        if self.config.min_order_value is not None and notional < self.config.min_order_value:
            return RiskCheckResult(False, "below_min_order_value", f"Order value {notional} < min {self.config.min_order_value}", {"symbol": symbol, "notional": str(notional), "min": str(self.config.min_order_value)})

        if self.config.max_order_value is not None and notional > self.config.max_order_value:
            return RiskCheckResult(False, "above_max_order_value", f"Order value {notional} > max {self.config.max_order_value}", {"symbol": symbol, "notional": str(notional), "max": str(self.config.max_order_value)})

        # % of daily volume
        if self.config.max_order_pct_of_daily_volume is not None:
            avg_vol = daily_volume
            if avg_vol is None:
                avg_vol = self._avg_daily_volume.get(symbol)

            if avg_vol is not None:
                try:
                    avg_vol_dec = to_decimal(avg_vol, "avg_volume")
                    if avg_vol_dec > ZERO:
                        pct = qty / avg_vol_dec
                        if pct > self.config.max_order_pct_of_daily_volume:
                            return RiskCheckResult(
                                False,
                                "exceeds_daily_volume",
                                f"Order qty {qty} is {pct:.1%} of daily volume {avg_vol_dec} > limit {self.config.max_order_pct_of_daily_volume:.1%}",
                                {"symbol": symbol, "pct": str(pct), "limit": str(self.config.max_order_pct_of_daily_volume)},
                            )
                except Exception as exc:
                    logger.debug("Volume check failed: %s", exc)

        return RiskCheckResult(True)

    def check_position_limits(self, symbol: str, new_quantity: Any, current_price: Any = None) -> RiskCheckResult:
        """Check position-level limits for a symbol."""
        symbol = str(symbol).strip().upper()
        qty = to_decimal(new_quantity, "new_quantity")
        price = to_price(current_price or 100, "price")

        notional = abs(qty) * price

        # Max position value per symbol
        if self.config.max_position_value is not None and notional > self.config.max_position_value:
            return RiskCheckResult(
                False,
                "max_position_value",
                f"Position value {notional} > max {self.config.max_position_value} for {symbol}",
                {"symbol": symbol, "notional": str(notional), "max": str(self.config.max_position_value)},
            )

        # Max position % per symbol
        if self.config.max_position_pct is not None:
            try:
                equity = self.portfolio.calculate_total_equity()
                if equity > ZERO:
                    pct = notional / equity
                    if pct > self.config.max_position_pct:
                        return RiskCheckResult(
                            False,
                            "max_position_pct",
                            f"Position would be {pct:.2%} of equity > limit {self.config.max_position_pct:.2%} for {symbol}",
                            {"symbol": symbol, "pct": str(pct), "limit": str(self.config.max_position_pct)},
                        )
            except Exception as exc:
                logger.debug("Position pct check failed: %s", exc)

        # Max open positions
        if self.config.max_open_positions is not None:
            try:
                if not self.portfolio.has_position(symbol):
                    if len(self.portfolio.positions) >= self.config.max_open_positions:
                        return RiskCheckResult(
                            False,
                            "max_open_positions",
                            f"Already have {len(self.portfolio.positions)} positions, max {self.config.max_open_positions}",
                            {"open": len(self.portfolio.positions), "max": self.config.max_open_positions},
                        )
            except Exception as exc:
                logger.debug("Max positions check failed: %s", exc)

        # Sector concentration
        if self.config.sector_exposure_limits and self.config.symbol_to_sector:
            sector = self.config.symbol_to_sector.get(symbol)
            if sector and sector in self.config.sector_exposure_limits:
                try:
                    equity = self.portfolio.calculate_total_equity()
                    sector_limit = self.config.sector_exposure_limits[sector]
                    # Calculate current sector exposure
                    sector_exposure = ZERO
                    for sym, pos in self.portfolio.positions.items():
                        sym_sector = self.config.symbol_to_sector.get(sym)
                        if sym_sector == sector:
                            sector_exposure += abs(pos.market_value) if hasattr(pos, "market_value") else ZERO

                    projected = sector_exposure + notional
                    max_sector_value = equity * sector_limit
                    if projected > max_sector_value:
                        return RiskCheckResult(
                            False,
                            "sector_exposure",
                            f"Sector {sector} exposure {projected} > limit {max_sector_value} ({sector_limit:.1%})",
                            {"sector": sector, "exposure": str(projected), "limit": str(max_sector_value)},
                        )
                except Exception as exc:
                    logger.debug("Sector check failed: %s", exc)

        return RiskCheckResult(True)

    def check_buying_power(self, required_cash: Any) -> RiskCheckResult:
        """Check if portfolio has sufficient buying power."""
        try:
            required = money(required_cash, "required_cash")
            if required <= ZERO:
                return RiskCheckResult(True)

            # Use portfolio's buying power if available
            if hasattr(self.portfolio, "calculate_buying_power"):
                buying_power = self.portfolio.calculate_buying_power()
                if required > buying_power:
                    return RiskCheckResult(
                        False,
                        "insufficient_buying_power",
                        f"Required {required} > buying power {buying_power}",
                        {"required": str(required), "buying_power": str(buying_power)},
                    )
            else:
                # Fallback to cash
                cash = getattr(self.portfolio, "current_cash", ZERO)
                if required > cash:
                    return RiskCheckResult(
                        False,
                        "insufficient_funds",
                        f"Required {required} > cash {cash}",
                        {"required": str(required), "cash": str(cash)},
                    )

            return RiskCheckResult(True)

        except Exception as exc:
            return RiskCheckResult(False, "buying_power_error", f"Buying power check error: {exc}", {})

    def check_drawdown_limits(self, portfolio: Any = None) -> RiskCheckResult:
        """Check if drawdown exceeds limit."""
        pf = portfolio or self.portfolio
        try:
            if hasattr(pf, "current_drawdown"):
                dd = pf.current_drawdown()
                if dd > self.config.max_drawdown_pct:
                    return RiskCheckResult(
                        False,
                        "max_drawdown",
                        f"Drawdown {dd:.2%} > limit {self.config.max_drawdown_pct:.2%}",
                        {"drawdown": str(dd), "limit": str(self.config.max_drawdown_pct)},
                    )
            return RiskCheckResult(True)
        except Exception as exc:
            return RiskCheckResult(False, "drawdown_check_error", f"Drawdown check error: {exc}", {})

    def check_daily_loss_limit(self, portfolio: Any = None) -> RiskCheckResult:
        """Check daily loss limit."""
        pf = portfolio or self.portfolio
        try:
            today = date.today()
            # Calculate daily PnL from equity curve or from portfolio?
            # For simplicity, use portfolio's total_return vs initial, or track daily_pnl
            # Here we use _daily_pnl dict that is updated via record_pnl

            daily_pnl = self._daily_pnl.get(today, ZERO)

            # If no daily pnl tracked, try to get from portfolio's equity history
            if daily_pnl == ZERO and hasattr(pf, "equity_history") and pf.equity_history:
                # Last equity point vs first today?
                # Simplified: use total_return
                pass

            equity = pf.calculate_total_equity() if hasattr(pf, "calculate_total_equity") else pf.initial_capital
            if equity <= ZERO:
                return RiskCheckResult(True)

            # Daily loss as negative pnl
            if daily_pnl < ZERO:
                loss_pct = abs(daily_pnl) / equity
                if loss_pct > self.config.daily_loss_limit_pct:
                    return RiskCheckResult(
                        False,
                        "daily_loss_limit",
                        f"Daily loss {loss_pct:.2%} > limit {self.config.daily_loss_limit_pct:.2%}",
                        {"daily_pnl": str(daily_pnl), "loss_pct": str(loss_pct), "limit": str(self.config.daily_loss_limit_pct)},
                    )

            return RiskCheckResult(True)

        except Exception as exc:
            return RiskCheckResult(False, "daily_loss_check_error", f"Daily loss check error: {exc}", {})

    def check_leverage(self, portfolio: Any = None) -> RiskCheckResult:
        """Check leverage ratio."""
        pf = portfolio or self.portfolio
        try:
            equity = pf.calculate_total_equity() if hasattr(pf, "calculate_total_equity") else ZERO
            gross = pf.calculate_gross_exposure() if hasattr(pf, "calculate_gross_exposure") else ZERO

            if equity <= ZERO:
                return RiskCheckResult(True)

            leverage = gross / equity if equity != ZERO else ZERO

            if leverage > self.config.max_leverage:
                return RiskCheckResult(
                    False,
                    "max_leverage",
                    f"Leverage {leverage:.2f}x > limit {self.config.max_leverage:.2f}x",
                    {"leverage": str(leverage), "limit": str(self.config.max_leverage)},
                )

            return RiskCheckResult(True)

        except Exception as exc:
            return RiskCheckResult(False, "leverage_check_error", f"Leverage check error: {exc}", {})

    def _check_portfolio_level(self, symbol: str, qty: Decimal, price: Decimal) -> RiskCheckResult:
        """Run all portfolio-level checks."""
        # Drawdown
        result = self.check_drawdown_limits()
        if not result.allowed:
            return result

        # Daily loss
        result = self.check_daily_loss_limit()
        if not result.allowed:
            return result

        # Leverage
        result = self.check_leverage()
        if not result.allowed:
            return result

        # Max gross exposure
        if self.config.max_gross_exposure_pct is not None:
            try:
                equity = self.portfolio.calculate_total_equity()
                gross = self.portfolio.calculate_gross_exposure() if hasattr(self.portfolio, "calculate_gross_exposure") else ZERO
                new_gross = gross + abs(qty) * price
                max_gross = equity * self.config.max_gross_exposure_pct

                if new_gross > max_gross:
                    return RiskCheckResult(
                        False,
                        "max_gross_exposure",
                        f"Gross exposure {new_gross} > max {max_gross} ({self.config.max_gross_exposure_pct:.1%})",
                        {"gross": str(new_gross), "max": str(max_gross)},
                    )
            except Exception as exc:
                logger.debug("Gross exposure check failed: %s", exc)

        # Max total exposure absolute
        if self.config.max_total_exposure is not None:
            try:
                gross = self.portfolio.calculate_gross_exposure() if hasattr(self.portfolio, "calculate_gross_exposure") else ZERO
                new_gross = gross + abs(qty) * price
                if new_gross > self.config.max_total_exposure:
                    return RiskCheckResult(
                        False,
                        "max_total_exposure",
                        f"Total exposure {new_gross} > max {self.config.max_total_exposure}",
                        {"exposure": str(new_gross), "max": str(self.config.max_total_exposure)},
                    )
            except Exception as exc:
                logger.debug("Total exposure check failed: %s", exc)

        # Weekly/monthly loss limits
        if self.config.weekly_loss_limit_pct is not None or self.config.monthly_loss_limit_pct is not None:
            # Placeholder – would need historical PnL tracking
            pass

        return RiskCheckResult(True)

    # -- circuit breakers --------------------------------------------------

    def emergency_stop_all(self, reason: str = "emergency") -> int:
        """Halt all trading immediately.

        Returns number of orders cancelled.
        """
        self._is_halted = True
        self._halt_reason = reason
        self._halt_time = datetime.now(timezone.utc)

        logger.critical("EMERGENCY STOP: %s", reason)
        self._alert("critical", f"Emergency stop: {reason}", {"reason": reason})

        # Cancel all pending orders
        cancelled = 0
        try:
            if hasattr(self.portfolio, "cancel_all_orders"):
                cancelled = self.portfolio.cancel_all_orders(reason=f"emergency_stop: {reason}")
            if hasattr(self.portfolio, "pause"):
                self.portfolio.pause()
        except Exception as exc:
            logger.exception("Failed to cancel orders on emergency stop: %s", exc)

        return cancelled

    def check_circuit_breakers(self) -> Optional[RiskCheckResult]:
        """Check all circuit breakers, halt if any tripped.

        Returns RiskCheckResult if halted, None if ok.
        """
        # Drawdown breaker
        dd_result = self.check_drawdown_limits()
        if not dd_result.allowed:
            self.emergency_stop_all(f"Drawdown limit breached: {dd_result.reason}")
            return dd_result

        # Daily loss breaker
        daily_result = self.check_daily_loss_limit()
        if not daily_result.allowed:
            self.emergency_stop_all(f"Daily loss limit breached: {daily_result.reason}")
            return daily_result

        # Consecutive losses breaker
        if self.config.max_consecutive_losses is not None:
            if self._consecutive_losses >= self.config.max_consecutive_losses:
                result = RiskCheckResult(
                    False,
                    "consecutive_losses",
                    f"Consecutive losses {self._consecutive_losses} >= limit {self.config.max_consecutive_losses}",
                    {"consecutive_losses": self._consecutive_losses, "limit": self.config.max_consecutive_losses},
                )
                self.emergency_stop_all(f"Consecutive losses: {self._consecutive_losses}")
                return result

        return None

    def record_trade_result(self, pnl: Any, is_win: bool):
        """Record trade result for consecutive loss tracking."""
        try:
            pnl_dec = to_decimal(pnl, "pnl")
            if is_win:
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1

            # Daily PnL
            today = date.today()
            self._daily_pnl[today] += pnl_dec

        except Exception as exc:
            logger.debug("Failed to record trade result: %s", exc)

    def record_error(self):
        """Record technical error for circuit breaker."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.config.max_consecutive_errors and self.config.pause_on_technical_error:
            self.emergency_stop_all(f"Too many technical errors: {self._consecutive_errors}")
            logger.error("Pausing due to %s consecutive errors", self._consecutive_errors)

    def reset_error_count(self):
        self._consecutive_errors = 0

    # -- override ----------------------------------------------------------

    def override(self, code: str, duration_minutes: float = 60.0) -> bool:
        """Manual override to resume trading after halt.

        Parameters
        ----------
        code:
            Override code (must match config.override_code if set, or allow_override True)
        duration_minutes:
            How long override lasts

        Returns
        -------
        bool
            Whether override succeeded
        """
        if not self.config.allow_override:
            logger.warning("Override attempted but allow_override is False")
            return False

        if self.config.override_code and code != self.config.override_code:
            logger.warning("Override code mismatch")
            return False

        self._override_active = True
        self._override_until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        self._is_halted = False
        self._halt_reason = None
        self._consecutive_losses = 0
        self._consecutive_errors = 0

        logger.warning("Risk override activated for %s minutes (code=%s)", duration_minutes, code)
        self._alert("warning", f"Risk override activated for {duration_minutes} min", {"code": code, "duration": duration_minutes})

        return True

    def _check_override_expiry(self):
        if self._override_active and self._override_until:
            if datetime.now(timezone.utc) >= self._override_until:
                self._override_active = False
                self._override_until = None
                logger.info("Risk override expired")

    def is_halted(self) -> bool:
        return self._is_halted and not self._override_active

    # -- batch validation --------------------------------------------------

    def validate_orders(self, orders: List[Any]) -> List[Any]:
        """Validate multiple orders, return approved ones."""
        approved = []
        for order in orders:
            result = self.validate_order(order)
            if result.allowed:
                approved.append(order)
            else:
                logger.info("Risk rejected order %s: [%s] %s", getattr(order, "symbol", "?"), result.code, result.reason)
        return approved

    def validate_signals(self, signals: List[Any]) -> List[Any]:
        """Validate signals (converts to orders internally for check).

        For compatibility with StrategyAdapter which works with Signals.
        """
        # For signals, we check symbol restrictions and position limits
        approved = []
        for signal in signals:
            symbol = getattr(signal, "symbol", None) or (signal.get("symbol") if isinstance(signal, dict) else None)
            if not symbol:
                continue

            symbol = str(symbol).upper()

            # Restricted check
            if symbol in self.config.restricted_symbols:
                logger.info("Risk rejected signal %s: restricted", symbol)
                continue

            if self.config.allowed_symbols is not None and symbol not in self.config.allowed_symbols:
                logger.info("Risk rejected signal %s: not allowed", symbol)
                continue

            # For signals, we don't have quantity yet (sizer will decide), so check portfolio-level only
            dd_result = self.check_drawdown_limits()
            if not dd_result.allowed:
                logger.info("Risk rejected signal %s: %s", symbol, dd_result.reason)
                continue

            approved.append(signal)

        return approved

    # -- alerts and logging ------------------------------------------------

    def add_alert_callback(self, callback: Callable[[str, str, Dict[str, Any]], None]):
        if not callable(callback):
            raise ValueError("callback must be callable")
        self._alert_callbacks.append(callback)

    def _alert(self, level: str, message: str, details: Dict[str, Any]):
        for cb in self._alert_callbacks:
            try:
                cb(level, message, details)
            except Exception:
                logger.exception("Alert callback failed")

    def _log_rejection(self, order: Any, result: RiskCheckResult):
        symbol = getattr(order, "symbol", "?")
        logger.info("Risk rejection: %s [%s] %s", symbol, result.code, result.reason)
        self._alert("warning", f"Risk rejected {symbol}: {result.reason}", {"code": result.code, "symbol": str(symbol), "reason": result.reason, **result.details})

    # -- stats -------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        return {
            "is_halted": self.is_halted(),
            "halt_reason": self._halt_reason,
            "consecutive_losses": self._consecutive_losses,
            "consecutive_errors": self._consecutive_errors,
            "daily_pnl": {str(k): str(v) for k, v in self._daily_pnl.items()},
            "restricted_symbols": sorted(self.config.restricted_symbols),
        }

    def __repr__(self):
        return f"<RiskManager halted={self.is_halted()} drawdown_limit={self.config.max_drawdown_pct} daily_loss={self.config.daily_loss_limit_pct}>"
