"""Stop Loss & Take Profit Manager for forward testing (Step 16).

Automated management of stop losses and take profits with support for:

* Fixed price stops
* Percentage stops
* ATR-based stops
* Trailing stops (fixed amount & percentage)
* Time-based stops
* Fixed price / percentage / risk-reward / trailing take profits
* Breakeven moves, scale-out, OCO orders

Reconciles with ``forward/broker.py`` SimulatedBroker which already has
stop_loss/take_profit in its step() – this manager is the richer, multi-stop,
portfolio-aware version that creates real ``Order`` objects and integrates
with ``OrderExecutor``.

Example
-------
>>> from backtest.simulator.portfolio import Portfolio
>>> from backtest.simulator.stop_manager import StopManager, StopConfig
>>> portfolio = Portfolio(name="test", initial_capital=100000)
>>> pos = portfolio.open_position("INFY", 100, 1500)
>>> manager = StopManager(portfolio=portfolio)
>>> manager.add_stop_loss(pos, stop_type="percentage", params={"pct": 0.02})
>>> manager.add_take_profit(pos, target_type="percentage", params={"pct": 0.05})
>>> hits = manager.check_stops({"INFY": {"close": 1450, "last": 1450}})
>>> hits[0].action
'SELL'
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set

from backtest.simulator.enums import OrderSide, OrderType
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ONE, ZERO
from backtest.simulator.money import price as to_price
from backtest.simulator.money import quantize_price, to_decimal

logger = logging.getLogger("backtest.simulator.stop_manager")

DEFAULT_STOP_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "stops.yaml"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StopType:
    FIXED_PRICE = "fixed_price"
    PERCENTAGE = "percentage"
    ATR_BASED = "atr_based"
    TRAILING_FIXED = "trailing_fixed"
    TRAILING_PERCENTAGE = "trailing_percentage"
    TIME_BASED = "time_based"

    ALL = (
        FIXED_PRICE,
        PERCENTAGE,
        ATR_BASED,
        TRAILING_FIXED,
        TRAILING_PERCENTAGE,
        TIME_BASED,
    )

    @classmethod
    def validate(cls, value: Any) -> str:
        v = str(value).strip().lower()
        aliases = {
            "fixed": cls.FIXED_PRICE,
            "percent": cls.PERCENTAGE,
            "pct": cls.PERCENTAGE,
            "atr": cls.ATR_BASED,
            "trailing": cls.TRAILING_FIXED,
            "trailing_fixed_amount": cls.TRAILING_FIXED,
            "trailing_pct": cls.TRAILING_PERCENTAGE,
            "trailing_percent": cls.TRAILING_PERCENTAGE,
            "time": cls.TIME_BASED,
            "time_stop": cls.TIME_BASED,
        }
        if v in aliases:
            v = aliases[v]
        if v not in cls.ALL:
            raise ValidationError(f"unknown stop type {value!r}; expected one of {cls.ALL}")
        return v


class TakeProfitType:
    FIXED_PRICE = "fixed_price"
    PERCENTAGE = "percentage"
    RISK_REWARD = "risk_reward"
    RESISTANCE = "resistance"
    TRAILING = "trailing"

    ALL = (FIXED_PRICE, PERCENTAGE, RISK_REWARD, RESISTANCE, TRAILING)

    @classmethod
    def validate(cls, value: Any) -> str:
        v = str(value).strip().lower()
        aliases = {
            "fixed": cls.FIXED_PRICE,
            "percent": cls.PERCENTAGE,
            "pct": cls.PERCENTAGE,
            "risk_reward_ratio": cls.RISK_REWARD,
            "rr": cls.RISK_REWARD,
            "resistance_level": cls.RESISTANCE,
            "trailing_take_profit": cls.TRAILING,
        }
        if v in aliases:
            v = aliases[v]
        if v not in cls.ALL:
            raise ValidationError(f"unknown take profit type {value!r}; expected one of {cls.ALL}")
        return v


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class StopConfig:
    """Configuration for a single stop loss or take profit."""

    stop_type: str = StopType.PERCENTAGE
    # Common params
    price: Optional[Decimal] = None
    pct: Optional[Decimal] = None  # e.g. 0.02 = 2%
    atr: Optional[Decimal] = None
    atr_multiplier: Decimal = Decimal("2")
    trailing_amount: Optional[Decimal] = None
    trailing_pct: Optional[Decimal] = None
    bars: Optional[int] = None  # for time-based
    risk_reward_ratio: Optional[Decimal] = None
    # Features
    move_to_breakeven: bool = False
    breakeven_trigger_pct: Optional[Decimal] = None
    scale_out_pct: Optional[Decimal] = None  # e.g. 0.5 = close 50% at target
    oco_group: Optional[str] = None  # OCO group id

    def __post_init__(self):
        self.stop_type = (
            StopType.validate(self.stop_type)
            if self.stop_type in StopType.ALL
            or self.stop_type.lower() in [t.lower() for t in StopType.ALL]
            else TakeProfitType.validate(self.stop_type)
        )

        # Normalize via try for both enums
        try:
            self.stop_type = StopType.validate(self.stop_type)
        except ValidationError:
            try:
                self.stop_type = TakeProfitType.validate(self.stop_type)
            except ValidationError:
                raise ValidationError(f"invalid stop_type {self.stop_type}")

        for name in ("price", "atr", "trailing_amount"):
            v = getattr(self, name)
            if v is not None:
                dec = to_price(v, name)
                if dec <= ZERO:
                    raise ValidationError(f"{name} must be positive")
                setattr(self, name, dec)

        for name in (
            "pct",
            "trailing_pct",
            "risk_reward_ratio",
            "breakeven_trigger_pct",
            "scale_out_pct",
        ):
            v = getattr(self, name)
            if v is not None:
                dec = to_decimal(v, name)
                if dec <= ZERO or dec > Decimal("10"):
                    # Allow >1 for risk_reward, but pct should be <=1
                    if name in (
                        "pct",
                        "trailing_pct",
                        "breakeven_trigger_pct",
                        "scale_out_pct",
                    ) and dec > Decimal("1"):
                        raise ValidationError(f"{name} must be between 0 and 1")
                setattr(self, name, dec)

        self.atr_multiplier = to_decimal(self.atr_multiplier, "atr_multiplier")
        if self.atr_multiplier <= ZERO:
            raise ValidationError("atr_multiplier must be positive")

        if self.bars is not None and self.bars < 1:
            raise ValidationError("bars must be >=1 for time-based stops")


def load_stop_config(path: str | Path | None = None, profile: str | None = None) -> Dict[str, Any]:
    """Load stop config from YAML."""
    config_path = Path(path) if path else DEFAULT_STOP_CONFIG_PATH

    if not config_path.exists():
        return {}

    try:
        import yaml

        doc = yaml.safe_load(config_path.read_text()) or {}
        return doc
    except Exception as exc:
        logger.warning("Failed to load stop config %s: %s", config_path, exc)
        return {}


# ---------------------------------------------------------------------------
# Stop dataclass
# ---------------------------------------------------------------------------


@dataclass
class Stop:
    """Single stop loss or take profit instance.

    Attributes
    ----------
    stop_id:
        Unique id
    position_id:
        Position it protects
    symbol:
        Symbol
    stop_type:
        Type from StopType or TakeProfitType
    side:
        Side of exit order (opposite of position)
    quantity:
        Quantity to close (None = full position, or fraction for scale-out)
    price:
        Current stop price (for fixed, percentage, ATR, trailing)
    original_price:
        Original calculated stop price
    is_take_profit:
        Whether this is take profit (vs stop loss)
    is_trailing:
        Whether trailing
    is_active:
        Whether still active
    created_at:
        When created
    triggered_at:
        When triggered
    extreme_price:
        For trailing: high-water mark (long) or low-water mark (short)
    bars_held:
        For time-based: bars since entry
    params:
        Original params dict
    oco_group:
        OCO group id
    move_to_breakeven:
        Whether to move to breakeven after trigger
    scale_out_pct:
        Fraction to close at this stop
    """

    stop_id: str
    position_id: str
    symbol: str
    stop_type: str
    side: OrderSide
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None
    original_price: Optional[Decimal] = None
    is_take_profit: bool = False
    is_trailing: bool = False
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    triggered_at: Optional[datetime] = None
    extreme_price: Optional[Decimal] = None
    bars_held: int = 0
    params: Dict[str, Any] = field(default_factory=dict)
    oco_group: Optional[str] = None
    move_to_breakeven: bool = False
    breakeven_trigger_pct: Optional[Decimal] = None
    scale_out_pct: Optional[Decimal] = None
    entry_price: Optional[Decimal] = None

    def __post_init__(self):
        self.symbol = str(self.symbol).strip().upper()
        if self.price is not None:
            self.price = to_price(self.price, "price")
        if self.original_price is not None:
            self.original_price = to_price(self.original_price, "original_price")
        if self.extreme_price is not None:
            self.extreme_price = to_price(self.extreme_price, "extreme_price")
        if self.entry_price is not None:
            self.entry_price = to_price(self.entry_price, "entry_price")
        if self.quantity is not None:
            self.quantity = to_price(self.quantity, "quantity")

    def to_dict(self):
        return {
            "stop_id": self.stop_id,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "stop_type": self.stop_type,
            "side": str(self.side),
            "quantity": str(self.quantity) if self.quantity else None,
            "price": str(self.price) if self.price else None,
            "original_price": str(self.original_price) if self.original_price else None,
            "is_take_profit": self.is_take_profit,
            "is_trailing": self.is_trailing,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
            "extreme_price": str(self.extreme_price) if self.extreme_price else None,
            "bars_held": self.bars_held,
            "oco_group": self.oco_group,
            "move_to_breakeven": self.move_to_breakeven,
            "scale_out_pct": str(self.scale_out_pct) if self.scale_out_pct else None,
        }


@dataclass
class StopHit:
    """Result when a stop is triggered."""

    symbol: str
    stop_id: str
    position_id: str
    stop_type: str
    is_take_profit: bool
    trigger_price: Decimal
    current_price: Decimal
    action: str  # BUY/SELL
    quantity: Decimal
    reason: str
    oco_group: Optional[str] = None
    scale_out_pct: Optional[Decimal] = None

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "stop_id": self.stop_id,
            "position_id": self.position_id,
            "stop_type": self.stop_type,
            "is_take_profit": self.is_take_profit,
            "trigger_price": str(self.trigger_price),
            "current_price": str(self.current_price),
            "action": self.action,
            "quantity": str(self.quantity),
            "reason": self.reason,
            "oco_group": self.oco_group,
        }


# ---------------------------------------------------------------------------
# StopManager
# ---------------------------------------------------------------------------


class StopManager:
    """Manages stop losses and take profits for all positions.

    Parameters
    ----------
    portfolio:
        Portfolio containing positions
    db_manager:
        Optional DB manager for persistence
    backtest_mode:
        If True, logs what would have happened but doesn't create orders
    """

    def __init__(self, portfolio: Any, db_manager: Any = None, backtest_mode: bool = False):
        self.portfolio = portfolio
        self.db_manager = db_manager
        self.backtest_mode = bool(backtest_mode)

        # position_id -> list of stops
        self._stops: Dict[str, List[Stop]] = defaultdict(list)
        # symbol -> list of stops (for fast lookup by market data)
        self._stops_by_symbol: Dict[str, List[Stop]] = defaultdict(list)
        # OCO groups
        self._oco_groups: Dict[str, Set[str]] = defaultdict(set)

        self._stats = {
            "stops_added": 0,
            "stops_triggered": 0,
            "stops_cancelled": 0,
            "trailing_updates": 0,
        }

        logger.info(
            "StopManager initialized: portfolio=%s backtest_mode=%s",
            getattr(portfolio, "name", "?"),
            backtest_mode,
        )

    # -- add stops ---------------------------------------------------------

    def add_stop_loss(
        self, position: Any, stop_type: str = "percentage", params: Optional[Dict[str, Any]] = None
    ) -> Stop:
        """Add stop loss to a position.

        Parameters
        ----------
        position:
            Position object with symbol, quantity, average_entry_price, position_id
        stop_type:
            One of StopType: fixed_price, percentage, atr_based, trailing_fixed,
            trailing_percentage, time_based
        params:
            Dict with keys: price, pct, atr, atr_multiplier, trailing_amount, trailing_pct, bars

        Returns
        -------
        Stop
        """
        return self._add_stop(position, stop_type, params, is_take_profit=False)

    def add_take_profit(
        self,
        position: Any,
        target_type: str = "percentage",
        params: Optional[Dict[str, Any]] = None,
    ) -> Stop:
        """Add take profit to a position."""
        return self._add_stop(position, target_type, params, is_take_profit=True)

    def _add_stop(
        self, position: Any, stop_type: str, params: Optional[Dict[str, Any]], is_take_profit: bool
    ) -> Stop:
        import uuid

        params = params or {}
        symbol = getattr(position, "symbol", None) or params.get("symbol")
        if not symbol:
            raise ValidationError("symbol required for stop")
        symbol = str(symbol).strip().upper()

        position_id = (
            getattr(position, "position_id", None) or params.get("position_id") or str(uuid.uuid4())
        )
        quantity = getattr(position, "quantity", None)
        entry_price = (
            getattr(position, "average_entry_price", None)
            or getattr(position, "entry_price", None)
            or params.get("entry_price")
        )

        if entry_price is None:
            raise ValidationError("entry_price required to calculate stop")

        entry_price_dec = to_price(entry_price, "entry_price")

        # Determine side: opposite of position
        # Position quantity signed: positive long, negative short
        qty = to_decimal(quantity, "quantity") if quantity is not None else Decimal("100")
        is_long = qty > ZERO
        side = OrderSide.SELL if is_long else OrderSide.BUY

        # Validate stop type
        if is_take_profit:
            try:
                validated_type = TakeProfitType.validate(stop_type)
            except ValidationError:
                validated_type = StopType.validate(stop_type)
        else:
            validated_type = StopType.validate(stop_type)

        # Calculate stop price based on type
        stop_price = self._calculate_stop_price(
            stop_type=validated_type,
            entry_price=entry_price_dec,
            is_long=is_long,
            params=params,
            is_take_profit=is_take_profit,
            current_price=params.get("current_price"),
        )

        is_trailing = validated_type in (
            StopType.TRAILING_FIXED,
            StopType.TRAILING_PERCENTAGE,
            TakeProfitType.TRAILING,
        )

        # Quantity: for scale-out, use fraction
        scale_out_pct = params.get("scale_out_pct")
        if scale_out_pct is not None:
            scale_out_pct = to_decimal(scale_out_pct, "scale_out_pct")
            if scale_out_pct <= ZERO or scale_out_pct > Decimal("1"):
                raise ValidationError("scale_out_pct must be in (0,1]")

        # For full close, quantity = abs(position qty) if available, else from params
        order_qty = params.get("quantity")
        if order_qty is not None:
            order_qty_dec = to_price(order_qty, "quantity")
        else:
            order_qty_dec = abs(qty) if qty != ZERO else None

        if scale_out_pct is not None and order_qty_dec is not None:
            order_qty_dec = (order_qty_dec * scale_out_pct).quantize(Decimal("1"))
            order_qty_dec = max(Decimal("1"), order_qty_dec)

        stop = Stop(
            stop_id=str(uuid.uuid4()),
            position_id=position_id,
            symbol=symbol,
            stop_type=validated_type,
            side=side,
            quantity=order_qty_dec,
            price=stop_price,
            original_price=stop_price,
            is_take_profit=is_take_profit,
            is_trailing=is_trailing,
            created_at=datetime.now(timezone.utc),
            extreme_price=entry_price_dec if is_trailing else None,
            params=dict(params),
            oco_group=params.get("oco_group"),
            move_to_breakeven=bool(params.get("move_to_breakeven", False)),
            breakeven_trigger_pct=(
                to_decimal(params["breakeven_trigger_pct"], "breakeven_trigger_pct")
                if params.get("breakeven_trigger_pct")
                else None
            ),
            scale_out_pct=scale_out_pct,
            entry_price=entry_price_dec,
        )

        self._stops[position_id].append(stop)
        self._stops_by_symbol[symbol].append(stop)

        if stop.oco_group:
            self._oco_groups[stop.oco_group].add(stop.stop_id)

        self._stats["stops_added"] += 1

        logger.info(
            "Stop added: %s %s %s %s @ %s (entry %s) oco=%s scale_out=%s",
            "TP" if is_take_profit else "SL",
            stop.stop_type,
            symbol,
            side,
            stop_price,
            entry_price_dec,
            stop.oco_group,
            scale_out_pct,
        )

        return stop

    def _calculate_stop_price(
        self,
        stop_type: str,
        entry_price: Decimal,
        is_long: bool,
        params: Dict[str, Any],
        is_take_profit: bool,
        current_price: Any = None,
    ) -> Optional[Decimal]:
        """Calculate stop price based on type and params."""
        if stop_type == StopType.FIXED_PRICE or stop_type == TakeProfitType.FIXED_PRICE:
            price = params.get("price") or params.get("stop_price") or params.get("target_price")
            if price is None:
                raise ValidationError(f"{stop_type} requires price param")
            return to_price(price, "price")

        elif stop_type == StopType.PERCENTAGE or stop_type == TakeProfitType.PERCENTAGE:
            pct = params.get("pct") or params.get("percent") or params.get("percentage")
            if pct is None:
                raise ValidationError(f"{stop_type} requires pct param")
            pct_dec = to_decimal(pct, "pct")
            if is_long:
                if is_take_profit:
                    # Long take profit above entry
                    return quantize_price(entry_price * (ONE + pct_dec))
                else:
                    # Long stop below entry
                    return quantize_price(entry_price * (ONE - pct_dec))
            else:
                # Short
                if is_take_profit:
                    # Short take profit below entry
                    return quantize_price(entry_price * (ONE - pct_dec))
                else:
                    # Short stop above entry
                    return quantize_price(entry_price * (ONE + pct_dec))

        elif stop_type == StopType.ATR_BASED:
            atr = params.get("atr")
            if atr is None:
                raise ValidationError("atr_based requires atr param")
            atr_dec = to_price(atr, "atr")
            mult = to_decimal(params.get("atr_multiplier", 2), "atr_multiplier")
            if is_long:
                if is_take_profit:
                    return quantize_price(entry_price + atr_dec * mult)
                else:
                    return quantize_price(entry_price - atr_dec * mult)
            else:
                if is_take_profit:
                    return quantize_price(entry_price - atr_dec * mult)
                else:
                    return quantize_price(entry_price + atr_dec * mult)

        elif stop_type == StopType.TRAILING_FIXED:
            trailing_amount = params.get("trailing_amount") or params.get("amount")
            if trailing_amount is None:
                raise ValidationError("trailing_fixed requires trailing_amount")
            trailing_dec = to_price(trailing_amount, "trailing_amount")
            # Initial stop price
            if is_long:
                return (
                    quantize_price(entry_price - trailing_dec)
                    if not is_take_profit
                    else quantize_price(entry_price + trailing_dec)
                )
            else:
                return (
                    quantize_price(entry_price + trailing_dec)
                    if not is_take_profit
                    else quantize_price(entry_price - trailing_dec)
                )

        elif stop_type == StopType.TRAILING_PERCENTAGE:
            trailing_pct = params.get("trailing_pct") or params.get("pct")
            if trailing_pct is None:
                raise ValidationError("trailing_percentage requires trailing_pct")
            trailing_pct_dec = to_decimal(trailing_pct, "trailing_pct")
            if is_long:
                if not is_take_profit:
                    return quantize_price(entry_price * (ONE - trailing_pct_dec))
                else:
                    return quantize_price(entry_price * (ONE + trailing_pct_dec))
            else:
                if not is_take_profit:
                    return quantize_price(entry_price * (ONE + trailing_pct_dec))
                else:
                    return quantize_price(entry_price * (ONE - trailing_pct_dec))

        elif stop_type == StopType.TIME_BASED:
            # Time-based doesn't have fixed price, it's triggered by bars held
            return None

        elif stop_type == TakeProfitType.RISK_REWARD:
            rr = params.get("risk_reward_ratio") or params.get("rr")
            if rr is None:
                raise ValidationError("risk_reward requires risk_reward_ratio")
            rr_dec = to_decimal(rr, "risk_reward_ratio")
            # Need stop loss pct to calculate risk
            stop_pct = params.get("stop_pct") or params.get("stop_loss_pct") or params.get("pct")
            if stop_pct is None:
                # Try to find existing stop for same position to get risk
                stop_pct = Decimal("0.02")  # default 2%
            else:
                stop_pct = to_decimal(stop_pct, "stop_pct")

            if is_long:
                # Long: risk is entry * stop_pct below, reward is risk * rr above
                return quantize_price(entry_price * (ONE + stop_pct * rr_dec))
            else:
                return quantize_price(entry_price * (ONE - stop_pct * rr_dec))

        elif stop_type == TakeProfitType.RESISTANCE:
            price = params.get("price") or params.get("resistance") or params.get("target_price")
            if price is None:
                raise ValidationError("resistance requires price")
            return to_price(price, "price")

        elif stop_type == TakeProfitType.TRAILING:
            # Similar to trailing stop but for take profit
            trailing_amount = (
                params.get("trailing_amount") or params.get("amount") or params.get("pct")
            )
            if trailing_amount is None:
                raise ValidationError("trailing take profit requires trailing_amount or pct")
            # If pct, treat as percentage
            if (
                isinstance(trailing_amount, (float, int, str))
                and str(trailing_amount).replace(".", "").replace("-", "").isdigit()
            ):
                # Could be pct or amount – try to infer
                try:
                    val = to_decimal(trailing_amount, "trailing_amount")
                    if val < ONE:  # treat as pct
                        if is_long:
                            return quantize_price(entry_price * (ONE + val))
                        else:
                            return quantize_price(entry_price * (ONE - val))
                except Exception:
                    pass
            return to_price(trailing_amount, "trailing_amount")

        else:
            raise ValidationError(f"unsupported stop type {stop_type}")

    # -- update trailing ---------------------------------------------------

    def update_trailing_stops(self, current_prices: Mapping[str, Any]) -> List[Stop]:
        """Update trailing stops based on current prices.

        For long positions, trailing stop tracks high-water mark and only moves up.
        For short, tracks low-water mark and only moves down.

        Returns list of updated stops.
        """
        updated = []

        for symbol, price_raw in current_prices.items():
            symbol = str(symbol).strip().upper()
            try:
                current_price = to_price(price_raw, "price")
            except Exception:
                # If dict with close/last
                if isinstance(price_raw, dict):
                    p = price_raw.get("close") or price_raw.get("last") or price_raw.get("price")
                    if p is None:
                        continue
                    try:
                        current_price = to_price(p, "price")
                    except Exception:
                        continue
                else:
                    continue

            stops = self._stops_by_symbol.get(symbol, [])
            for stop in stops:
                if not stop.is_active:
                    continue

                # Update extreme price for trailing stops
                if stop.is_trailing:
                    if stop.extreme_price is None:
                        stop.extreme_price = current_price
                    else:
                        is_long = stop.side == OrderSide.SELL
                        if is_long:
                            stop.extreme_price = max(stop.extreme_price, current_price)
                        else:
                            stop.extreme_price = min(stop.extreme_price, current_price)

                # Calculate new stop price for trailing
                new_stop_price = None
                params = stop.params

                if stop.is_trailing:
                    if stop.stop_type == StopType.TRAILING_FIXED:
                        trailing_amount = params.get("trailing_amount") or params.get("amount")
                        if trailing_amount is None:
                            # Skip trailing update but still check breakeven below
                            pass
                        else:
                            trailing_dec = to_price(trailing_amount, "trailing_amount")
                            if stop.side == OrderSide.SELL:  # long
                                new_stop_price = quantize_price(stop.extreme_price - trailing_dec)
                                if stop.price is None or new_stop_price > stop.price:
                                    stop.price = new_stop_price
                                    updated.append(stop)
                                    self._stats["trailing_updates"] += 1
                                    logger.info(
                                        "Trailing stop updated for %s: new stop %s (extreme %s)",
                                        symbol,
                                        new_stop_price,
                                        stop.extreme_price,
                                    )
                            else:  # short
                                new_stop_price = quantize_price(stop.extreme_price + trailing_dec)
                                if stop.price is None or new_stop_price < stop.price:
                                    stop.price = new_stop_price
                                    updated.append(stop)
                                    self._stats["trailing_updates"] += 1
                                    logger.info(
                                        "Trailing stop updated for %s: new stop %s (extreme %s)",
                                        symbol,
                                        new_stop_price,
                                        stop.extreme_price,
                                    )

                    elif stop.stop_type == StopType.TRAILING_PERCENTAGE:
                        trailing_pct = params.get("trailing_pct") or params.get("pct")
                        if trailing_pct is None:
                            pass
                        else:
                            trailing_pct_dec = to_decimal(trailing_pct, "trailing_pct")
                            if stop.side == OrderSide.SELL:  # long
                                new_stop_price = quantize_price(
                                    stop.extreme_price * (ONE - trailing_pct_dec)
                                )
                                if stop.price is None or new_stop_price > stop.price:
                                    stop.price = new_stop_price
                                    updated.append(stop)
                                    self._stats["trailing_updates"] += 1
                                    logger.info(
                                        "Trailing pct stop updated for %s: %s (extreme %s)",
                                        symbol,
                                        new_stop_price,
                                        stop.extreme_price,
                                    )
                            else:
                                new_stop_price = quantize_price(
                                    stop.extreme_price * (ONE + trailing_pct_dec)
                                )
                                if stop.price is None or new_stop_price < stop.price:
                                    stop.price = new_stop_price
                                    updated.append(stop)
                                    self._stats["trailing_updates"] += 1
                                    logger.info(
                                        "Trailing pct stop updated for %s: %s (extreme %s)",
                                        symbol,
                                        new_stop_price,
                                        stop.extreme_price,
                                    )

                # Check breakeven move for any stop with move_to_breakeven
                if stop.move_to_breakeven and stop.entry_price and stop.breakeven_trigger_pct:
                    try:
                        if stop.side == OrderSide.SELL:  # long
                            trigger_price = stop.entry_price * (ONE + stop.breakeven_trigger_pct)
                            if current_price >= trigger_price:
                                if stop.price is None or (stop.price < stop.entry_price):
                                    old_price = stop.price
                                    stop.price = stop.entry_price
                                    # Avoid duplicate in updated list
                                    if stop not in updated:
                                        updated.append(stop)
                                    logger.info(
                                        "Stop moved to breakeven for %s: %s -> %s (trigger %s)",
                                        symbol,
                                        old_price,
                                        stop.entry_price,
                                        trigger_price,
                                    )
                        else:  # short
                            trigger_price = stop.entry_price * (ONE - stop.breakeven_trigger_pct)
                            if current_price <= trigger_price:
                                if stop.price is None or (stop.price > stop.entry_price):
                                    old_price = stop.price
                                    stop.price = stop.entry_price
                                    if stop not in updated:
                                        updated.append(stop)
                                    logger.info(
                                        "Stop moved to breakeven for %s: %s -> %s",
                                        symbol,
                                        old_price,
                                        stop.entry_price,
                                    )
                    except Exception as exc:
                        logger.debug("Breakeven check failed for %s: %s", symbol, exc)

        return updated

    # -- check stops -------------------------------------------------------

    def check_stops(self, market_data: Mapping[str, Any]) -> List[StopHit]:
        """Check if any stops are triggered by current market data.

        Parameters
        ----------
        market_data:
            Dict mapping symbol -> price or bar dict {close, last, low, high}

        Returns
        -------
        List[StopHit]
            List of triggered stops with exit info
        """
        hits: List[StopHit] = []

        if not market_data:
            return hits

        # Normalize market_data to symbol->price dict
        prices: Dict[str, Dict[str, Any]] = {}

        if isinstance(market_data, dict):
            if "symbol" in market_data and ("close" in market_data or "last" in market_data):
                # Single bar
                sym = str(market_data.get("symbol", "")).upper()
                if sym:
                    prices[sym] = market_data
            else:
                # Mapping symbol->bar or symbol->price
                for sym, data in market_data.items():
                    sym = str(sym).strip().upper()
                    if isinstance(data, dict):
                        prices[sym] = data
                    else:
                        try:
                            prices[sym] = {"close": float(data), "last": float(data)}
                        except (ValueError, TypeError):
                            continue

        for symbol, bar in prices.items():
            symbol = symbol.upper()
            stops = self._stops_by_symbol.get(symbol, [])
            if not stops:
                continue

            # Extract current price info
            current_price = None
            low = None
            high = None

            if isinstance(bar, dict):
                current_price = bar.get("close") or bar.get("last") or bar.get("price")
                low = bar.get("low")
                high = bar.get("high")
            else:
                current_price = bar

            if current_price is None:
                continue

            try:
                curr_price_dec = to_price(current_price, "current_price")
                low_dec = to_price(low, "low") if low is not None else curr_price_dec
                high_dec = to_price(high, "high") if high is not None else curr_price_dec
            except Exception:
                continue

            # Update bars held for time-based stops
            for stop in stops:
                if stop.is_active:
                    stop.bars_held += 1

            # Check each stop
            for stop in list(stops):  # copy for safe removal
                if not stop.is_active:
                    continue

                triggered = False
                trigger_price = stop.price

                # Time-based stop
                if stop.stop_type == StopType.TIME_BASED:
                    bars_needed = stop.params.get("bars") or stop.bars_held
                    if isinstance(bars_needed, int) and stop.bars_held >= bars_needed:
                        triggered = True
                        trigger_price = curr_price_dec
                        logger.info(
                            "Time-based stop triggered for %s after %s bars", symbol, stop.bars_held
                        )

                # For long positions (side SELL): stop triggers when
                # price <= stop price (for SL) or >= for TP?
                # Let's define:
                # Long SL: triggers when low <= stop price (price falls to stop)
                # Long TP: triggers when high >= target price (price rises to target)
                # Short SL: triggers when high >= stop price (price rises to stop)
                # Short TP: triggers when low <= target price (price falls to target)

                elif stop.side == OrderSide.SELL:  # Long position, exit via SELL
                    if stop.is_take_profit:
                        # Take profit: price goes up to target
                        if stop.price is not None and high_dec >= stop.price:
                            triggered = True
                    else:
                        # Stop loss: price goes down to stop
                        if stop.price is not None and low_dec <= stop.price:
                            triggered = True

                else:  # Short position, exit via BUY
                    if stop.is_take_profit:
                        # Short TP: price goes down to target
                        if stop.price is not None and low_dec <= stop.price:
                            triggered = True
                    else:
                        # Short SL: price goes up to stop
                        if stop.price is not None and high_dec >= stop.price:
                            triggered = True

                if triggered:
                    # Create hit
                    qty = stop.quantity
                    if qty is None:
                        # Try to get from portfolio position
                        try:
                            pos = self.portfolio.get_position(symbol)
                            if pos:
                                qty = abs(pos.quantity)
                            else:
                                qty = Decimal("100")
                        except Exception:
                            qty = Decimal("100")

                    hit = StopHit(
                        symbol=symbol,
                        stop_id=stop.stop_id,
                        position_id=stop.position_id,
                        stop_type=stop.stop_type,
                        is_take_profit=stop.is_take_profit,
                        trigger_price=trigger_price or curr_price_dec,
                        current_price=curr_price_dec,
                        action="SELL" if stop.side == OrderSide.SELL else "BUY",
                        quantity=qty,
                        reason=(
                            f"{'Take profit' if stop.is_take_profit else 'Stop loss'} "
                            f"{stop.stop_type} hit for {symbol} at {trigger_price} "
                            f"(current {curr_price_dec})"
                        ),
                        oco_group=stop.oco_group,
                        scale_out_pct=stop.scale_out_pct,
                    )

                    hits.append(hit)
                    stop.is_active = False
                    stop.triggered_at = datetime.now(timezone.utc)
                    self._stats["stops_triggered"] += 1

                    logger.info(
                        "Stop triggered: %s %s %s @ %s (current %s) qty=%s",
                        symbol,
                        "TP" if stop.is_take_profit else "SL",
                        stop.stop_type,
                        trigger_price,
                        curr_price_dec,
                        qty,
                    )

                    # Handle OCO: cancel other stops in same group
                    if stop.oco_group:
                        self._handle_oco(stop.oco_group, triggered_stop_id=stop.stop_id)

                    # In backtest mode, just log
                    if self.backtest_mode:
                        logger.info(
                            "Backtest mode: would have exited %s %s @ %s",
                            symbol,
                            qty,
                            trigger_price,
                        )

        return hits

    def _handle_oco(self, oco_group: str, triggered_stop_id: str):
        """Cancel other stops in OCO group when one triggers."""
        stop_ids = self._oco_groups.get(oco_group, set())
        for stop_id in list(stop_ids):
            if stop_id == triggered_stop_id:
                continue

            # Find and deactivate stop
            for symbol, stops in self._stops_by_symbol.items():
                for stop in stops:
                    if stop.stop_id == stop_id and stop.is_active:
                        stop.is_active = False
                        self._stats["stops_cancelled"] += 1
                        logger.info(
                            "OCO cancelled stop %s for %s (group %s triggered by %s)",
                            stop_id,
                            symbol,
                            oco_group,
                            triggered_stop_id,
                        )

        # Clear group
        self._oco_groups.pop(oco_group, None)

    # -- remove stops ------------------------------------------------------

    def remove_stops(self, position_id: str) -> int:
        """Remove all stops for a position_id.

        Returns number removed.
        """
        stops = self._stops.pop(position_id, [])
        count = len(stops)

        # Also remove from symbol index and OCO groups
        for stop in stops:
            symbol = stop.symbol
            if symbol in self._stops_by_symbol:
                self._stops_by_symbol[symbol] = [
                    s for s in self._stops_by_symbol[symbol] if s.stop_id != stop.stop_id
                ]

            if stop.oco_group and stop.oco_group in self._oco_groups:
                self._oco_groups[stop.oco_group].discard(stop.stop_id)
                if not self._oco_groups[stop.oco_group]:
                    self._oco_groups.pop(stop.oco_group, None)

        self._stats["stops_cancelled"] += count
        logger.info("Removed %s stops for position %s", count, position_id)

        return count

    def remove_stop(self, stop_id: str) -> bool:
        """Remove single stop by id."""
        for position_id, stops in list(self._stops.items()):
            for stop in stops:
                if stop.stop_id == stop_id:
                    self._stops[position_id] = [s for s in stops if s.stop_id != stop_id]
                    # Also from symbol index
                    if stop.symbol in self._stops_by_symbol:
                        self._stops_by_symbol[stop.symbol] = [
                            s for s in self._stops_by_symbol[stop.symbol] if s.stop_id != stop_id
                        ]
                    if stop.oco_group:
                        self._oco_groups.get(stop.oco_group, set()).discard(stop_id)
                    self._stats["stops_cancelled"] += 1
                    logger.info("Removed stop %s", stop_id)
                    return True
        return False

    # -- order creation ----------------------------------------------------

    def create_orders_for_hits(self, hits: List[StopHit]) -> List[Any]:
        """Create Order objects for stop hits.

        Returns list of Orders.
        """
        from backtest.simulator.order import Order

        orders = []

        for hit in hits:
            try:
                side = OrderSide.SELL if hit.action == "SELL" else OrderSide.BUY
                order = Order(
                    symbol=hit.symbol,
                    side=side,
                    quantity=hit.quantity,
                    order_type=OrderType.MARKET,
                    portfolio_id=getattr(self.portfolio, "portfolio_id", None),
                    strategy_name=f"stop_{hit.stop_type}",
                )
                order.submit()
                self.portfolio.add_order(order)
                orders.append(order)
                logger.info(
                    "Created exit order for stop hit %s: %s %s %s",
                    hit.stop_id,
                    hit.action,
                    hit.quantity,
                    hit.symbol,
                )
            except Exception as exc:
                logger.exception("Failed to create order for hit %s: %s", hit.stop_id, exc)

        return orders

    # -- stats and state ---------------------------------------------------

    def get_active_stops(self, symbol: Optional[str] = None) -> List[Stop]:
        if symbol:
            symbol = symbol.upper()
            return [s for s in self._stops_by_symbol.get(symbol, []) if s.is_active]
        else:
            all_stops = []
            for stops in self._stops_by_symbol.values():
                all_stops.extend([s for s in stops if s.is_active])
            return all_stops

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stops": [s.to_dict() for stops in self._stops.values() for s in stops],
            "stats": dict(self._stats),
        }

    def __repr__(self):
        active = len(self.get_active_stops())
        return (
            f"<StopManager active={active} total_added={self._stats['stops_added']} "
            f"triggered={self._stats['stops_triggered']}>"
        )
