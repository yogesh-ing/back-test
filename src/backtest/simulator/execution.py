"""Order execution simulation for the forward testing simulator.

:class:`OrderExecutor` is where Steps 5–8 meet: it takes a working
:class:`~backtest.simulator.order.Order` and a market snapshot, decides
whether and how much of it fills, prices the fill through the slippage and
fee engines, and produces a :class:`~backtest.simulator.fill.Fill`.

What "realistic" means here
---------------------------
A naive simulator fills every order instantly, entirely, at the quoted price.
Three things make that a lie, and each is modelled:

**Liquidity.** You cannot buy more than the market is offering. Orders larger
than the available depth fill *partially*, and the remainder rests (or is
cancelled, depending on time-in-force).

**Queue position.** A limit order at the touch does not fill just because the
price reached it — you are behind everyone who got there first. Only when the
market trades *through* your limit are you certain of a fill. At the touch,
:attr:`ExecutionConfig.touch_fill_probability` decides.

**Availability.** Markets close and symbols halt. An order submitted into
either is rejected, not silently filled.

Fill versus rejection
---------------------
These are different outcomes and are kept distinct:

* :attr:`ExecutionStatus.NO_FILL` — nothing happened, the order still rests.
  A limit order away from the market is the normal case.
* :attr:`ExecutionStatus.REJECTED` — terminal. Market closed, symbol halted,
  or a fill-or-kill that could not be filled whole.

Conflating them would either strand orders that should have died, or kill
orders that should still be working.

Determinism
-----------
Every random decision goes through one seeded generator, so a run is exactly
reproducible. An execution simulator that gives different answers on each run
cannot be used to compare two strategies.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from backtest.simulator.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from backtest.simulator.errors import ValidationError
from backtest.simulator.fees import CommissionCalculator, TradeSegment
from backtest.simulator.fill import Fill, LiquidityFlag
from backtest.simulator.money import (
    ZERO,
    price as to_price,
    quantize_price,
    to_decimal,
)
from backtest.simulator.slippage import MarketSnapshot, SlippageCalculator

if TYPE_CHECKING:  # pragma: no cover
    from backtest.simulator.order import Order
    from backtest.simulator.portfolio import Portfolio

__all__ = [
    "RealismLevel",
    "RejectionCode",
    "ExecutionStatus",
    "ExecutionEvent",
    "ExecutionConfig",
    "ExecutionResult",
    "OrderExecutor",
    "load_execution_config",
    "DEFAULT_EXECUTION_CONFIG_PATH",
]

logger = logging.getLogger("backtest.simulator.execution")

_DUST = Decimal("0.00000001")

DEFAULT_EXECUTION_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "execution.yaml"
)


class RealismLevel:
    """How pessimistic the execution assumptions are."""

    OPTIMISTIC = "optimistic"
    """Everything fills, immediately, often with price improvement."""

    REALISTIC = "realistic"
    """Partial fills, queue risk, latency. The default."""

    PESSIMISTIC = "pessimistic"
    """Thin liquidity, poor queue position, no price improvement."""

    ALL = (OPTIMISTIC, REALISTIC, PESSIMISTIC)

    @classmethod
    def validate(cls, level: Any) -> str:
        value = str(level).strip().lower()
        if value not in cls.ALL:
            raise ValidationError(
                f"unknown realism level {level!r}; expected one of {cls.ALL}",
                code="invalid_realism_level",
            )
        return value


class RejectionCode:
    """Machine-readable rejection reasons.

    Written to ``orders.rejection_reason`` and surfaced by the Step 19
    dashboard, so they must stay stable.
    """

    MARKET_CLOSED = "market_closed"
    SYMBOL_HALTED = "symbol_halted"
    NO_LIQUIDITY = "no_liquidity"
    FOK_UNFILLABLE = "fok_unfillable"
    ORDER_NOT_WORKING = "order_not_working"
    STALE_DATA = "stale_data"

    ALL = (
        MARKET_CLOSED,
        SYMBOL_HALTED,
        NO_LIQUIDITY,
        FOK_UNFILLABLE,
        ORDER_NOT_WORKING,
        STALE_DATA,
    )


class ExecutionStatus:
    """Outcome of one execution attempt."""

    FILLED = "filled"
    PARTIAL = "partial"
    NO_FILL = "no_fill"
    """Nothing happened; the order still rests. Not an error."""
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    """IOC/DAY remainder cancelled after a partial fill."""

    ALL = (FILLED, PARTIAL, NO_FILL, REJECTED, CANCELLED)


class ExecutionEvent:
    """Names of the hooks :class:`OrderExecutor` fires."""

    FILL = "on_fill"
    PARTIAL_FILL = "on_partial_fill"
    REJECT = "on_reject"
    NO_FILL = "on_no_fill"
    TRIGGER = "on_trigger"

    ALL = (FILL, PARTIAL_FILL, REJECT, NO_FILL, TRIGGER)


@dataclass(frozen=True)
class ExecutionResult:
    """What happened when an order met the market."""

    order_id: str
    symbol: str
    status: str
    filled_quantity: Decimal = ZERO
    remaining_quantity: Decimal = ZERO
    fill: Fill | None = None
    rejection_code: str | None = None
    reason: str = ""
    latency_ms: Decimal = ZERO
    requested_quantity: Decimal = ZERO
    available_liquidity: Decimal | None = None

    @property
    def is_filled(self) -> bool:
        return self.status == ExecutionStatus.FILLED

    @property
    def is_partial(self) -> bool:
        return self.status == ExecutionStatus.PARTIAL

    @property
    def did_trade(self) -> bool:
        """True when any quantity changed hands."""
        return self.filled_quantity > ZERO

    @property
    def is_rejected(self) -> bool:
        return self.status == ExecutionStatus.REJECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "status": self.status,
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "requested_quantity": str(self.requested_quantity),
            "rejection_code": self.rejection_code,
            "reason": self.reason,
            "latency_ms": str(self.latency_ms),
            "fill_id": self.fill.fill_id if self.fill else None,
            "fill_price": str(self.fill.fill_price) if self.fill else None,
        }

    def __str__(self) -> str:  # pragma: no cover - debug helper
        if self.is_rejected:
            return f"{self.symbol} REJECTED [{self.rejection_code}] {self.reason}"
        if not self.did_trade:
            return f"{self.symbol} no fill: {self.reason}"
        price = self.fill.fill_price if self.fill else "?"
        return f"{self.symbol} {self.status} {self.filled_quantity} @ {price}"


@dataclass
class ExecutionConfig:
    """Execution realism knobs.

    Parameters
    ----------
    max_participation:
        Share of the bar's volume one order may consume. The single most
        important setting here — it is what forces large orders to fill in
        pieces instead of magically absorbing the whole book.
    touch_fill_probability:
        Chance a limit order fills when the market merely *touches* the limit
        rather than trading through it. Models queue position.
    price_improvement_probability:
        Chance of filling better than the expected price.
    enforce_market_hours:
        Reject orders outside the session. NSE 09:15–15:30 IST by default.
    seed:
        Fixes every random decision so runs are reproducible.
    """

    realism: str = RealismLevel.REALISTIC

    # ---- latency ----
    min_latency_ms: Decimal = Decimal("50")
    max_latency_ms: Decimal = Decimal("500")

    # ---- liquidity ----
    max_participation: Decimal = Decimal("0.1")
    allow_partial_fills: bool = True
    require_volume: bool = False
    """Reject when the snapshot has no volume, instead of assuming plenty."""

    # ---- queue and price improvement ----
    touch_fill_probability: Decimal = Decimal("0.5")
    price_improvement_probability: Decimal = Decimal("0.1")
    price_improvement_bps: Decimal = Decimal("1")

    # ---- availability ----
    enforce_market_hours: bool = False
    session_open: dtime = dtime(9, 15)
    session_close: dtime = dtime(15, 30)
    session_timezone: str = "Asia/Kolkata"
    halted_symbols: frozenset[str] = field(default_factory=frozenset)

    # ---- execution context ----
    segment: str = TradeSegment.EQUITY_DELIVERY
    seed: int | None = 42

    def __post_init__(self) -> None:
        self.realism = RealismLevel.validate(self.realism)
        self.segment = TradeSegment.validate(self.segment)
        for name in (
            "min_latency_ms", "max_latency_ms", "max_participation",
            "touch_fill_probability", "price_improvement_probability",
            "price_improvement_bps",
        ):
            value = to_decimal(getattr(self, name), name)
            if value < ZERO:
                raise ValidationError(
                    f"{name} must not be negative", code="invalid_execution_config"
                )
            setattr(self, name, value)

        if self.max_latency_ms < self.min_latency_ms:
            raise ValidationError(
                "max_latency_ms must be >= min_latency_ms",
                code="invalid_execution_config",
            )
        if self.max_participation <= ZERO:
            raise ValidationError(
                "max_participation must be positive", code="invalid_execution_config"
            )
        for name in ("touch_fill_probability", "price_improvement_probability"):
            if getattr(self, name) > Decimal("1"):
                raise ValidationError(
                    f"{name} is a probability and must be between 0 and 1",
                    code="invalid_execution_config",
                )
        self.halted_symbols = frozenset(
            str(s).strip().upper() for s in self.halted_symbols
        )

    @classmethod
    def preset(cls, level: str, **overrides: Any) -> "ExecutionConfig":
        """Build one of the three realism presets."""
        level = RealismLevel.validate(level)
        base: dict[str, Any] = {"realism": level}
        if level == RealismLevel.OPTIMISTIC:
            base.update(
                min_latency_ms=Decimal("0"),
                max_latency_ms=Decimal("0"),
                max_participation=Decimal("1"),
                touch_fill_probability=Decimal("1"),
                price_improvement_probability=Decimal("0.5"),
                price_improvement_bps=Decimal("2"),
            )
        elif level == RealismLevel.PESSIMISTIC:
            base.update(
                min_latency_ms=Decimal("200"),
                max_latency_ms=Decimal("1000"),
                max_participation=Decimal("0.02"),
                touch_fill_probability=Decimal("0.1"),
                price_improvement_probability=Decimal("0"),
                require_volume=True,
            )
        base.update(overrides)
        return cls(**base)

    def to_dict(self) -> dict[str, Any]:
        return {
            "realism": self.realism,
            "min_latency_ms": str(self.min_latency_ms),
            "max_latency_ms": str(self.max_latency_ms),
            "max_participation": str(self.max_participation),
            "allow_partial_fills": self.allow_partial_fills,
            "touch_fill_probability": str(self.touch_fill_probability),
            "price_improvement_probability": str(self.price_improvement_probability),
            "enforce_market_hours": self.enforce_market_hours,
            "segment": self.segment,
            "seed": self.seed,
        }


def _parse_time(value: Any, field_name: str) -> dtime:
    if isinstance(value, dtime):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValidationError(
        f"{field_name} must look like HH:MM, got {value!r}",
        code="invalid_execution_config",
    )


def load_execution_config(
    path: str | Path | None = None, profile: str | None = None
) -> ExecutionConfig:
    """Load :class:`ExecutionConfig` from ``config/execution.yaml``.

    Falls back to the built-in preset when the file is absent, so the presets
    work with no configuration at all.
    """
    config_path = Path(path) if path else DEFAULT_EXECUTION_CONFIG_PATH
    if path is not None and not config_path.exists():
        raise ValidationError(
            f"execution config not found: {config_path}", code="config_not_found"
        )
    if not config_path.exists():
        return ExecutionConfig.preset(profile or RealismLevel.REALISTIC)

    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ValidationError(
            f"{config_path} exists but PyYAML is not installed", code="missing_pyyaml"
        ) from exc
    try:
        document = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:
        raise ValidationError(
            f"could not parse {config_path}: {exc}", code="invalid_execution_config"
        ) from exc

    merged: dict[str, Any] = dict(document.get("default") or {})
    profiles = document.get("profiles") or {}
    chosen = profile or document.get("active_profile") or RealismLevel.REALISTIC
    if profiles:
        if chosen not in profiles:
            raise ValidationError(
                f"unknown execution profile {chosen!r}; available: {sorted(profiles)}",
                code="unknown_execution_profile",
            )
        merged.update(profiles[chosen] or {})
    merged.setdefault("realism", chosen if chosen in RealismLevel.ALL else RealismLevel.REALISTIC)

    for key in ("session_open", "session_close"):
        if key in merged:
            merged[key] = _parse_time(merged[key], key)
    if "halted_symbols" in merged and merged["halted_symbols"]:
        merged["halted_symbols"] = frozenset(merged["halted_symbols"])

    known = set(ExecutionConfig.__dataclass_fields__)
    unknown = set(merged) - known
    if unknown:
        raise ValidationError(
            f"unknown execution config keys: {sorted(unknown)}",
            code="invalid_execution_config",
        )
    return ExecutionConfig(**merged)


class OrderExecutor:
    """Simulates order execution against market data.

    Examples
    --------
    >>> executor = OrderExecutor()                            # doctest: +SKIP
    >>> result = executor.execute(order, {"bid": 99, "ask": 101, "volume": 10000})
    >>> result.status                                          # doctest: +SKIP
    'filled'
    """

    def __init__(
        self,
        config: ExecutionConfig | None = None,
        slippage: SlippageCalculator | None = None,
        fees: CommissionCalculator | None = None,
        portfolio: "Portfolio | None" = None,
    ) -> None:
        self.config = config or ExecutionConfig()
        self.slippage = slippage or SlippageCalculator()
        self.fees = fees or CommissionCalculator()
        self.portfolio = portfolio
        self._rng = random.Random(self.config.seed)
        self._callbacks: dict[str, list[Callable[..., None]]] = {}
        self._results: list[ExecutionResult] = []

    @classmethod
    def for_realism(cls, level: str, **kwargs: Any) -> "OrderExecutor":
        """Build with one of the three realism presets."""
        return cls(config=ExecutionConfig.preset(level), **kwargs)

    @classmethod
    def from_config(
        cls, path: str | Path | None = None, profile: str | None = None, **kwargs: Any
    ) -> "OrderExecutor":
        """Build from ``config/execution.yaml``."""
        return cls(config=load_execution_config(path, profile), **kwargs)

    def reset(self) -> None:
        """Clear results and re-seed, so a replay is identical."""
        self._results.clear()
        self._rng = random.Random(self.config.seed)

    # -- availability ------------------------------------------------------

    def is_market_open(self, when: datetime | None) -> bool:
        """Whether the session is open at ``when``.

        Returns ``True`` when enforcement is off or no timestamp is supplied —
        a backtest over daily bars has no meaningful clock, and refusing to
        trade would make it useless.
        """
        if not self.config.enforce_market_hours or when is None:
            return True
        cfg = self.config
        try:
            from zoneinfo import ZoneInfo

            local = (
                when.astimezone(ZoneInfo(cfg.session_timezone))
                if when.tzinfo is not None
                else when
            )
        except Exception:  # pragma: no cover - missing tzdata
            local = when
        minutes = local.hour * 60 + local.minute
        return (
            cfg.session_open.hour * 60 + cfg.session_open.minute
            <= minutes
            <= cfg.session_close.hour * 60 + cfg.session_close.minute
        )

    def is_halted(self, symbol: str) -> bool:
        return str(symbol).strip().upper() in self.config.halted_symbols

    def halt(self, *symbols: str) -> None:
        """Halt trading in one or more symbols."""
        self.config.halted_symbols = self.config.halted_symbols | {
            str(s).strip().upper() for s in symbols
        }

    def resume(self, *symbols: str) -> None:
        """Lift a halt."""
        self.config.halted_symbols = self.config.halted_symbols - {
            str(s).strip().upper() for s in symbols
        }

    # -- helpers -----------------------------------------------------------

    def simulate_latency(
        self, min_ms: Any = None, max_ms: Any = None
    ) -> Decimal:
        """Draw a fill latency in milliseconds.

        Reported rather than slept on: a simulator that actually waited
        500 ms per order would take hours to replay a day. The value feeds
        the Step 18 execution-quality report.
        """
        low = to_decimal(min_ms if min_ms is not None else self.config.min_latency_ms, "min_ms")
        high = to_decimal(max_ms if max_ms is not None else self.config.max_latency_ms, "max_ms")
        if high < low:
            raise ValidationError(
                "max_ms must be >= min_ms", code="invalid_latency_range"
            )
        if high == low:
            return low
        span = high - low
        return (low + span * Decimal(str(self._rng.random()))).quantize(Decimal("0.001"))

    def available_liquidity(self, snapshot: MarketSnapshot) -> Decimal | None:
        """How much of the bar's volume one order may take.

        ``None`` when the snapshot carries no volume — the caller then decides
        whether to assume plenty or reject.
        """
        if snapshot.volume is None:
            return None
        return quantize_price(abs(snapshot.volume) * self.config.max_participation)

    def check_order_fillable(
        self, order: "Order", market_data: Mapping[str, Any] | Any
    ) -> tuple[bool, str | None, str]:
        """Pre-trade checks.

        Returns ``(fillable, rejection_code, reason)``. A ``rejection_code``
        of ``None`` with ``fillable=False`` means "no fill this tick" — the
        order stays working rather than dying.
        """
        if not getattr(order, "is_working", False):
            return (
                False,
                RejectionCode.ORDER_NOT_WORKING,
                f"order is {order.status}, not working",
            )

        snapshot = MarketSnapshot.from_market_data(market_data)

        if self.is_halted(order.symbol):
            return False, RejectionCode.SYMBOL_HALTED, f"{order.symbol} is halted"

        if not self.is_market_open(snapshot.timestamp):
            return False, RejectionCode.MARKET_CLOSED, "market is closed"

        if self.config.require_volume and snapshot.volume is None:
            return (
                False,
                RejectionCode.NO_LIQUIDITY,
                "market data carries no volume",
            )
        if snapshot.volume is not None and snapshot.volume <= ZERO:
            return False, RejectionCode.NO_LIQUIDITY, "no volume traded"

        if not order.is_fillable(market_data):
            # Not an error: a limit away from the market simply rests.
            return False, None, "price condition not met"

        return True, None, ""

    # -- per-type processing ----------------------------------------------

    def process_market_order(
        self, order: "Order", market_data: Mapping[str, Any] | Any
    ) -> ExecutionResult:
        """Fill at the far touch plus slippage."""
        if order.order_type is not OrderType.MARKET:
            raise ValidationError(
                f"process_market_order got a {order.order_type} order",
                code="wrong_order_type",
            )
        return self.execute(order, market_data)

    def process_limit_order(
        self, order: "Order", market_data: Mapping[str, Any] | Any
    ) -> ExecutionResult:
        """Fill only if the market reaches the limit, subject to queue risk."""
        if order.order_type not in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            raise ValidationError(
                f"process_limit_order got a {order.order_type} order",
                code="wrong_order_type",
            )
        return self.execute(order, market_data)

    def process_stop_order(
        self, order: "Order", market_data: Mapping[str, Any] | Any
    ) -> ExecutionResult:
        """Trigger, then behave as a market (or limit) order."""
        if not order.order_type.is_stop_family:
            raise ValidationError(
                f"process_stop_order got a {order.order_type} order",
                code="wrong_order_type",
            )
        return self.execute(order, market_data)

    # -- main entry point --------------------------------------------------

    def execute(
        self, order: "Order", market_data: Mapping[str, Any] | Any
    ) -> ExecutionResult:
        """Attempt to execute ``order`` against one market snapshot.

        Never raises for ordinary outcomes — a rejection or a no-fill is
        returned as a result, because the Step 20 loop must keep running.
        """
        snapshot = MarketSnapshot.from_market_data(market_data)
        requested = order.remaining_quantity

        fillable, code, reason = self.check_order_fillable(order, market_data)
        if not fillable:
            if code is not None:
                return self._reject(order, code, reason, requested)
            return self._no_fill(order, reason, requested)

        was_triggered = order.triggered
        if order.order_type.is_stop_family and order.triggered and not was_triggered:
            self._fire(ExecutionEvent.TRIGGER, order)  # pragma: no cover

        # --- how much can actually trade ---
        liquidity = self.available_liquidity(snapshot)
        fill_qty = requested
        if liquidity is not None and liquidity < requested:
            if not self.config.allow_partial_fills:
                return self._no_fill(
                    order,
                    f"only {liquidity} available, partial fills disabled",
                    requested,
                    liquidity,
                )
            fill_qty = liquidity

        if fill_qty <= ZERO:
            return self._no_fill(order, "no liquidity available", requested, liquidity)

        # Fill-or-kill is all or nothing. The exchange *cancels* such an
        # order rather than rejecting it, so the result says CANCELLED while
        # still carrying the code that explains why.
        if order.time_in_force is TimeInForce.FOK and fill_qty < requested - _DUST:
            reason = f"fill-or-kill needed {requested}, only {fill_qty} available"
            order.cancel(f"{RejectionCode.FOK_UNFILLABLE}: {reason}")
            result = ExecutionResult(
                order_id=order.order_id,
                symbol=order.symbol,
                status=ExecutionStatus.CANCELLED,
                remaining_quantity=order.remaining_quantity,
                rejection_code=RejectionCode.FOK_UNFILLABLE,
                reason=reason,
                requested_quantity=requested,
                available_liquidity=liquidity,
            )
            self._record(result)
            logger.warning("cancelled %s: [%s] %s", order.symbol,
                           RejectionCode.FOK_UNFILLABLE, reason)
            self._fire(ExecutionEvent.REJECT, order, result)
            return result

        # --- queue position for resting limit orders ---
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if not self._limit_fills(order, snapshot):
                return self._no_fill(
                    order, "limit touched but not traded through (queue position)",
                    requested, liquidity,
                )

        # --- price ---
        reference = order.calculate_fill_price(market_data)
        estimate = self.slippage.calculate_slippage(
            order, market_data, reference_price=reference, quantity=fill_qty
        )
        price = self._apply_price_improvement(order, estimate.executed_price)

        # A limit order can never fill worse than its limit, whatever the
        # slippage model says. Belt and braces: the calculator caps too.
        if order.limit_price is not None and order.order_type in (
            OrderType.LIMIT, OrderType.STOP_LIMIT
        ):
            price = (
                min(price, order.limit_price)
                if order.is_buy
                else max(price, order.limit_price)
            )

        # --- fees ---
        breakdown = self.fees.calculate(
            quantity=fill_qty,
            fill_price=price,
            side=order.side,
            segment=self.config.segment,
        )

        latency = self.simulate_latency()
        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            fill_price=price,
            order_id=order.order_id,
            reference_price=reference,
            liquidity_flag=(
                LiquidityFlag.MAKER
                if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
                else LiquidityFlag.TAKER
            ),
            strategy_name=order.strategy_name,
            **breakdown.as_fill_kwargs(),
        )
        order.add_fill(fill)

        if self.portfolio is not None:
            self.portfolio.apply_fill(fill)

        complete = order.status is OrderStatus.FILLED
        status = ExecutionStatus.FILLED if complete else ExecutionStatus.PARTIAL

        # Immediate-or-cancel kills whatever did not fill.
        if not complete and order.time_in_force is TimeInForce.IOC:
            order.cancel("IOC: remainder cancelled")
            status = ExecutionStatus.CANCELLED

        result = ExecutionResult(
            order_id=order.order_id,
            symbol=order.symbol,
            status=status,
            filled_quantity=fill_qty,
            remaining_quantity=order.remaining_quantity,
            fill=fill,
            latency_ms=latency,
            requested_quantity=requested,
            available_liquidity=liquidity,
        )
        self._record(result)
        logger.info(
            "%s %s %s @ %s (%s of %s, %.0fms)",
            status, order.side, fill_qty, price, fill_qty, requested, float(latency),
        )
        self._fire(
            ExecutionEvent.FILL if complete else ExecutionEvent.PARTIAL_FILL,
            order, fill, result,
        )
        return result

    def execute_all(
        self,
        orders: Iterable["Order"],
        market_data: Mapping[str, Any] | Mapping[str, Mapping[str, Any]],
        by_symbol: bool = False,
    ) -> list[ExecutionResult]:
        """Execute many orders against one tick.

        Parameters
        ----------
        by_symbol:
            When ``True``, ``market_data`` maps symbol to snapshot, so a
            multi-symbol portfolio can be swept in one call. Orders whose
            symbol is missing are skipped rather than failing the batch — a
            feed dropping one symbol must not stop the others trading.
        """
        results: list[ExecutionResult] = []
        for order in orders:
            if by_symbol:
                snapshot = market_data.get(order.symbol)  # type: ignore[union-attr]
                if snapshot is None:
                    results.append(
                        self._no_fill(order, "no market data for symbol", order.remaining_quantity)
                    )
                    continue
            else:
                snapshot = market_data
            results.append(self.execute(order, snapshot))
        return results

    # -- internals ---------------------------------------------------------

    def _limit_fills(self, order: "Order", snapshot: MarketSnapshot) -> bool:
        """Decide whether a resting limit order gets filled.

        If the market traded *through* the limit, the fill is certain. If it
        only *touched*, you are in a queue behind everyone who was there
        first, and only a fraction of such orders fill.
        """
        limit = order.limit_price
        if limit is None:  # pragma: no cover - guarded by validation
            return True

        reference = snapshot.ask if order.is_buy else snapshot.bid
        if reference is None:
            reference = snapshot.last

        traded_through = reference < limit if order.is_buy else reference > limit
        if traded_through:
            return True

        probability = float(self.config.touch_fill_probability)
        if probability >= 1:
            return True
        if probability <= 0:
            return False
        return self._rng.random() < probability

    def _apply_price_improvement(self, order: "Order", price: Decimal) -> Decimal:
        """Occasionally fill better than expected, as a real venue might."""
        chance = float(self.config.price_improvement_probability)
        if chance <= 0 or self._rng.random() >= chance:
            return price
        delta = price * self.config.price_improvement_bps / Decimal("10000")
        improved = price - delta if order.is_buy else price + delta
        return quantize_price(max(improved, _DUST))

    def _reject(
        self,
        order: "Order",
        code: str,
        reason: str,
        requested: Decimal,
        liquidity: Decimal | None = None,
    ) -> ExecutionResult:
        if not order.is_terminal:
            order.reject(f"{code}: {reason}")
        result = ExecutionResult(
            order_id=order.order_id,
            symbol=order.symbol,
            status=ExecutionStatus.REJECTED,
            remaining_quantity=order.remaining_quantity,
            rejection_code=code,
            reason=reason,
            requested_quantity=requested,
            available_liquidity=liquidity,
        )
        self._record(result)
        logger.warning("rejected %s: [%s] %s", order.symbol, code, reason)
        self._fire(ExecutionEvent.REJECT, order, result)
        return result

    def _no_fill(
        self,
        order: "Order",
        reason: str,
        requested: Decimal,
        liquidity: Decimal | None = None,
    ) -> ExecutionResult:
        result = ExecutionResult(
            order_id=order.order_id,
            symbol=order.symbol,
            status=ExecutionStatus.NO_FILL,
            remaining_quantity=requested,
            reason=reason,
            requested_quantity=requested,
            available_liquidity=liquidity,
        )
        self._record(result)
        logger.debug("no fill %s: %s", order.symbol, reason)
        self._fire(ExecutionEvent.NO_FILL, order, result)
        return result

    def _record(self, result: ExecutionResult) -> None:
        self._results.append(result)

    # -- callbacks ---------------------------------------------------------

    def add_callback(self, event: str, handler: Callable[..., None]) -> None:
        """Register a monitoring hook."""
        if event not in ExecutionEvent.ALL:
            raise ValidationError(
                f"unknown execution event {event!r}; expected one of {ExecutionEvent.ALL}",
                code="unknown_event",
            )
        self._callbacks.setdefault(event, []).append(handler)

    def _fire(self, event: str, *args: Any) -> None:
        """Invoke handlers without letting one break execution."""
        for handler in self._callbacks.get(event, []):
            try:
                handler(*args)
            except Exception:  # noqa: BLE001 - deliberate isolation
                logger.exception("execution callback %s failed", event)

    # -- reporting ---------------------------------------------------------

    @property
    def results(self) -> Sequence[ExecutionResult]:
        return tuple(self._results)

    def statistics(self) -> dict[str, Any]:
        """Execution-quality summary for the run."""
        if not self._results:
            return {"count": 0}
        filled = [r for r in self._results if r.is_filled]
        partial = [r for r in self._results if r.is_partial]
        rejected = [r for r in self._results if r.is_rejected]
        traded = [r for r in self._results if r.did_trade]

        # Include cancellations that carry a code (FOK), so nothing that
        # killed an order is invisible in the report.
        by_code: dict[str, int] = {}
        for r in self._results:
            if r.rejection_code:
                by_code[r.rejection_code] = by_code.get(r.rejection_code, 0) + 1

        latencies = [float(r.latency_ms) for r in traded]
        requested = sum((r.requested_quantity for r in self._results), ZERO)
        got = sum((r.filled_quantity for r in self._results), ZERO)

        return {
            "count": len(self._results),
            "filled": len(filled),
            "partial": len(partial),
            "no_fill": sum(1 for r in self._results if r.status == ExecutionStatus.NO_FILL),
            "rejected": len(rejected),
            "cancelled": sum(1 for r in self._results if r.status == ExecutionStatus.CANCELLED),
            "fill_rate": round(len(traded) / len(self._results), 4),
            "quantity_requested": requested,
            "quantity_filled": got,
            "quantity_fill_rate": (
                round(float(got / requested), 4) if requested > ZERO else 0.0
            ),
            "mean_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "rejections_by_code": by_code,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<OrderExecutor {self.config.realism} n={len(self._results)}>"

