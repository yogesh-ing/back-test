"""Order model for the forward testing simulator.

An :class:`Order` is the full lifecycle record of a trading instruction:
created, validated, submitted, partially or fully filled, cancelled or
rejected. Every transition is checked against
:data:`~backtest.simulator.enums.VALID_TRANSITIONS` and appended to a
timestamped history, so the audit trail cannot silently go backwards.

Lifecycle
---------
::

    Order(...)            # constructed, status = PENDING, submitted_at = None
      |
      v  submit()         # validate(); on failure -> REJECTED with a reason
    PENDING  ------------------> CANCELLED / REJECTED
      |  add_fill(partial)
      v
    PARTIAL  ------------------> CANCELLED
      |  add_fill(remainder)
      v
    FILLED                       (terminal)

``PENDING`` covers both "created" and "working" because those are the values
the ``orders.status`` CHECK constraint allows. Use :attr:`Order.is_submitted`
to tell them apart — it is ``True`` once :meth:`submit` has stamped
``submitted_at``.

Triggering
----------
Stop, stop-limit and trailing-stop orders must be *triggered* before they can
fill. :meth:`Order.update_trailing` ratchets a trailing stop in the favourable
direction only, and :meth:`Order.is_fillable` checks the trigger before
applying any limit condition.

Prices
------
All prices and quantities are :class:`~decimal.Decimal`. See
:mod:`backtest.simulator.money` for why floats are refused.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from backtest.simulator.enums import (
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ZERO, is_zero, money
from backtest.simulator.money import price as to_price
from backtest.simulator.money import quantize_money, quantize_price

if TYPE_CHECKING:  # pragma: no cover
    from backtest.db.manager import DatabaseManager

__all__ = [
    "Order",
    "OrderEvent",
    "StatusChange",
    "FillLike",
    "InvalidTransitionError",
    "OrderValidationError",
]

logger = logging.getLogger("backtest.simulator.order")

#: One unit of the 8-dp quantity grid, used for dust comparisons.
_DUST = Decimal("0.00000001")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderValidationError(ValidationError):
    """An order's fields are inconsistent with its type."""

    code = "invalid_order"


class InvalidTransitionError(ValidationError):
    """A status change was attempted that the state machine forbids."""

    code = "invalid_transition"


@runtime_checkable
class FillLike(Protocol):
    """The minimal shape :meth:`Order.add_fill` needs.

    Declared as a Protocol so Step 5 does not depend on Step 6: the real
    :class:`~backtest.simulator.fill.Fill` satisfies it structurally, and so
    does any lightweight stand-in used in tests.
    """

    quantity: Decimal
    fill_price: Decimal


class OrderEvent:
    """Names of the callback hooks :class:`Order` fires."""

    SUBMIT = "on_submit"
    FILL = "on_fill"
    PARTIAL_FILL = "on_partial_fill"
    CANCEL = "on_cancel"
    REJECT = "on_reject"
    TRIGGER = "on_trigger"

    ALL = (SUBMIT, FILL, PARTIAL_FILL, CANCEL, REJECT, TRIGGER)


@dataclass(frozen=True)
class StatusChange:
    """One entry in an order's status history."""

    status: OrderStatus
    at: datetime
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "at": self.at.isoformat(), "note": self.note}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StatusChange":
        return cls(
            status=OrderStatus.parse(payload["status"]),
            at=datetime.fromisoformat(payload["at"]),
            note=payload.get("note", ""),
        )


def _quote(market_data: Mapping[str, Any] | Any) -> dict[str, Decimal | None]:
    """Normalise a market-data mapping (or bare price) into bid/ask/last.

    Accepts the Step 10 dict shape — ``bid``, ``ask``, ``last``, ``close`` —
    and falls back sensibly when only some keys are present. A bare number is
    treated as the last traded price.
    """
    if market_data is None:
        raise ValidationError("market data is required", code="missing_market_data")

    if not isinstance(market_data, Mapping):
        last = to_price(market_data, "price")
        return {"bid": last, "ask": last, "last": last}

    def get(key: str) -> Decimal | None:
        value = market_data.get(key)
        return to_price(value, key) if value is not None else None

    last = get("last") or get("close") or get("price")
    bid = get("bid") or last
    ask = get("ask") or last
    if last is None:
        last = bid or ask
    if last is None:
        raise ValidationError(
            "market data must contain at least one of last/close/price/bid/ask",
            code="missing_market_data",
        )
    return {"bid": bid or last, "ask": ask or last, "last": last}


@dataclass
class Order:
    """A trading instruction and its lifecycle.

    Parameters
    ----------
    symbol:
        Instrument identifier; upper-cased on construction.
    side:
        :class:`~backtest.simulator.enums.OrderSide` or the string
        ``"buy"``/``"sell"``.
    quantity:
        Absolute size. Must be positive — direction lives in ``side``, never
        in the sign of the quantity, which is what the database's
        ``ck_orders_qty_pos`` constraint also enforces.
    limit_price / stop_price / trailing_amount:
        Required for the order types that use them; see :meth:`validate`.

    Raises
    ------
    OrderValidationError
        On construction if quantity or prices are structurally impossible.
        Type-specific consistency is checked by :meth:`validate`, which
        :meth:`submit` calls.

    Examples
    --------
    >>> o = Order(symbol="INFY", side="buy", order_type="limit",
    ...           quantity=10, limit_price=1500)
    >>> o.submit()
    >>> o.is_fillable({"ask": 1495})
    True
    """

    symbol: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET

    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    portfolio_id: str | None = None
    exchange: str = "NSE"

    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trailing_amount: Decimal | None = None

    time_in_force: TimeInForce = TimeInForce.DAY
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: Decimal = ZERO
    average_fill_price: Decimal | None = None

    reason_for_rejection: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    strategy_name: str | None = None

    created_at: datetime = field(default_factory=_utcnow)
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    updated_at: datetime = field(default_factory=_utcnow)

    triggered: bool = False
    """Whether a stop-family order has breached its trigger."""

    triggered_at: datetime | None = None
    extreme_price: Decimal | None = field(default=None, repr=False)
    """High/low water mark used to ratchet a trailing stop."""

    fills: list[Any] = field(default_factory=list, repr=False)
    status_history: list[StatusChange] = field(default_factory=list, repr=False)
    _callbacks: dict[str, list[Callable[..., None]]] = field(
        default_factory=dict, repr=False, compare=False
    )

    # -- construction ------------------------------------------------------

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol).strip().upper()
        if not self.symbol:
            raise OrderValidationError("symbol must not be empty", code="invalid_symbol")

        try:
            self.side = OrderSide.parse(self.side)
            self.order_type = OrderType.parse(self.order_type)
            self.time_in_force = TimeInForce.parse(self.time_in_force)
            self.status = OrderStatus.parse(self.status)
        except ValueError as exc:
            raise OrderValidationError(str(exc), code="invalid_enum") from exc

        self.quantity = to_price(self.quantity, "quantity")
        self.filled_quantity = to_price(self.filled_quantity, "filled_quantity")

        for name in ("limit_price", "stop_price", "trailing_amount", "extreme_price"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, to_price(value, name))
        if self.average_fill_price is not None:
            self.average_fill_price = to_price(self.average_fill_price, "average_fill_price")

        if self.quantity <= ZERO:
            raise OrderValidationError(
                "quantity must be positive; direction belongs to `side`",
                code="invalid_quantity",
                quantity=str(self.quantity),
            )
        if self.filled_quantity < ZERO:
            raise OrderValidationError(
                "filled_quantity must not be negative", code="invalid_filled_quantity"
            )
        if self.filled_quantity > self.quantity + _DUST:
            raise OrderValidationError(
                "filled_quantity cannot exceed quantity",
                code="overfilled",
                filled=str(self.filled_quantity),
                quantity=str(self.quantity),
            )

        if not self.status_history:
            self.status_history.append(StatusChange(self.status, self.created_at, "created"))

    # -- derived properties ------------------------------------------------

    @property
    def remaining_quantity(self) -> Decimal:
        """Unfilled balance. Never negative."""
        return max(ZERO, quantize_price(self.quantity - self.filled_quantity))

    @property
    def is_submitted(self) -> bool:
        """True once :meth:`submit` has run."""
        return self.submitted_at is not None

    @property
    def is_working(self) -> bool:
        """True when the order is submitted and can still fill."""
        return self.is_submitted and self.status.is_working

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def is_complete(self) -> bool:
        return self.status is OrderStatus.FILLED

    @property
    def is_buy(self) -> bool:
        return self.side is OrderSide.BUY

    @property
    def signed_quantity(self) -> Decimal:
        """Order size signed by side: positive to buy, negative to sell."""
        return quantize_price(self.quantity * self.side.sign)

    @property
    def signed_filled_quantity(self) -> Decimal:
        return quantize_price(self.filled_quantity * self.side.sign)

    @property
    def notional(self) -> Decimal:
        """Best available estimate of the order's value."""
        reference = self.average_fill_price or self.limit_price or self.stop_price or ZERO
        return quantize_money(self.quantity * reference)

    @property
    def total_commission(self) -> Decimal:
        """Sum of commission across recorded fills."""
        return quantize_money(
            sum((money(getattr(f, "commission", ZERO)) for f in self.fills), ZERO)
        )

    # -- validation --------------------------------------------------------

    def validate(self) -> None:
        """Check the order is internally consistent for its type.

        Mirrors the database's CHECK constraints so a bad order is caught in
        memory rather than by an ``IntegrityError`` several layers down.

        Raises
        ------
        OrderValidationError
            Describing the first problem found.
        """
        if self.quantity <= ZERO:
            raise OrderValidationError("quantity must be positive", code="invalid_quantity")

        if self.order_type.needs_limit_price and self.limit_price is None:
            raise OrderValidationError(
                f"{self.order_type} orders require a limit_price",
                code="missing_limit_price",
                order_type=self.order_type.value,
            )
        if self.order_type.needs_stop_price and self.stop_price is None:
            raise OrderValidationError(
                f"{self.order_type} orders require a stop_price",
                code="missing_stop_price",
                order_type=self.order_type.value,
            )
        if self.order_type is OrderType.TRAILING_STOP and self.trailing_amount is None:
            raise OrderValidationError(
                "trailing_stop orders require a trailing_amount",
                code="missing_trailing_amount",
            )

        for name in ("limit_price", "stop_price", "trailing_amount"):
            value = getattr(self, name)
            if value is not None and value <= ZERO:
                raise OrderValidationError(
                    f"{name} must be positive when set",
                    code="invalid_price",
                    field=name,
                    value=str(value),
                )

        # A market order carrying a limit price is almost always a caller bug:
        # the price would be silently ignored at execution time.
        if self.order_type is OrderType.MARKET and (
            self.limit_price is not None or self.stop_price is not None
        ):
            raise OrderValidationError(
                "market orders must not carry a limit_price or stop_price",
                code="unexpected_price",
            )
        if self.order_type is not OrderType.TRAILING_STOP and self.trailing_amount is not None:
            raise OrderValidationError(
                "trailing_amount is only valid on trailing_stop orders",
                code="unexpected_trailing_amount",
            )

        if self.order_type is OrderType.STOP_LIMIT:
            # A buy stop-limit triggers on the way up, so a limit below the
            # trigger can never fill. Same logic mirrored for sells.
            assert self.limit_price is not None and self.stop_price is not None
            if self.is_buy and self.limit_price < self.stop_price:
                raise OrderValidationError(
                    "buy stop-limit needs limit_price >= stop_price, "
                    "otherwise it can never fill",
                    code="unfillable_stop_limit",
                    limit=str(self.limit_price),
                    stop=str(self.stop_price),
                )
            if not self.is_buy and self.limit_price > self.stop_price:
                raise OrderValidationError(
                    "sell stop-limit needs limit_price <= stop_price, "
                    "otherwise it can never fill",
                    code="unfillable_stop_limit",
                    limit=str(self.limit_price),
                    stop=str(self.stop_price),
                )

    # -- state machine -----------------------------------------------------

    def update_status(
        self, new_status: OrderStatus | str, note: str = "", at: datetime | None = None
    ) -> OrderStatus:
        """Transition to ``new_status``, enforcing the state machine.

        Raises
        ------
        InvalidTransitionError
            If the move is not in
            :data:`~backtest.simulator.enums.VALID_TRANSITIONS`.
        """
        target = OrderStatus.parse(new_status)
        allowed = VALID_TRANSITIONS.get(self.status, frozenset())

        if target not in allowed:
            raise InvalidTransitionError(
                f"cannot move order from {self.status} to {target}",
                code="invalid_transition",
                order_id=self.order_id,
                current=self.status.value,
                requested=target.value,
                allowed=sorted(s.value for s in allowed) or "none (terminal)",
            )

        stamp = at or _utcnow()
        self.status = target
        self.updated_at = stamp
        self.status_history.append(StatusChange(target, stamp, note))
        logger.debug("order %s -> %s (%s)", self.order_id, target, note)
        return target

    def submit(self, at: datetime | None = None) -> "Order":
        """Validate and mark the order as live.

        On validation failure the order is moved to ``REJECTED`` with the
        reason recorded, and the error is re-raised. Rejecting *and* raising
        is deliberate: the audit trail keeps the reason while the caller is
        still forced to handle the failure.

        Raises
        ------
        OrderValidationError
            If :meth:`validate` fails.
        InvalidTransitionError
            If the order was already submitted or is terminal.
        """
        if self.is_submitted:
            raise InvalidTransitionError(
                "order has already been submitted",
                code="already_submitted",
                order_id=self.order_id,
            )
        if self.is_terminal:
            raise InvalidTransitionError(
                f"cannot submit a {self.status} order",
                code="invalid_transition",
                order_id=self.order_id,
            )

        try:
            self.validate()
        except OrderValidationError as exc:
            self.reject(str(exc), at=at)
            raise

        self.submitted_at = at or _utcnow()
        self.updated_at = self.submitted_at
        self.status_history.append(StatusChange(self.status, self.submitted_at, "submitted"))
        logger.info("submitted %s %s %s %s", self.side, self.quantity, self.symbol, self.order_type)
        self._fire(OrderEvent.SUBMIT, self)
        return self

    def cancel(self, reason: str = "", at: datetime | None = None) -> "Order":
        """Cancel a working order, keeping any fills already recorded.

        Raises
        ------
        InvalidTransitionError
            If the order is already terminal.
        """
        stamp = at or _utcnow()
        self.update_status(OrderStatus.CANCELLED, reason or "cancelled", at=stamp)
        self.cancelled_at = stamp
        logger.info("cancelled order %s (%s)", self.order_id, reason or "no reason given")
        self._fire(OrderEvent.CANCEL, self, reason)
        return self

    def reject(self, reason: str, at: datetime | None = None) -> "Order":
        """Reject the order with a mandatory reason.

        The reason is required because the database enforces the same thing
        (``ck_orders_rejection_reason``): a rejection nobody can explain is
        useless when debugging a live run.
        """
        if not reason or not str(reason).strip():
            raise OrderValidationError(
                "a rejection reason is required", code="missing_rejection_reason"
            )
        stamp = at or _utcnow()
        self.update_status(OrderStatus.REJECTED, reason, at=stamp)
        self.reason_for_rejection = str(reason)
        logger.warning("rejected order %s: %s", self.order_id, reason)
        self._fire(OrderEvent.REJECT, self, reason)
        return self

    # -- fills -------------------------------------------------------------

    def add_fill(
        self,
        fill: FillLike | None = None,
        quantity: Any = None,
        fill_price: Any = None,
        at: datetime | None = None,
    ) -> OrderStatus:
        """Apply an execution, updating quantity, average price and status.

        Accepts either a fill object (anything matching :class:`FillLike`, so
        Step 6's ``Fill`` slots straight in) or explicit ``quantity`` and
        ``fill_price``.

        The average fill price is recomputed as a true quantity-weighted mean
        across all fills, so several partial fills at different prices report
        correctly.

        Returns
        -------
        OrderStatus
            The status after applying the fill: ``PARTIAL`` or ``FILLED``.

        Raises
        ------
        InvalidTransitionError
            If the order is not working (terminal, or never submitted).
        OrderValidationError
            If the fill is non-positive or would overfill the order.
        """
        if fill is not None:
            quantity = getattr(fill, "quantity", None)
            fill_price = getattr(fill, "fill_price", None)
            if quantity is None or fill_price is None:
                raise OrderValidationError(
                    "fill must expose `quantity` and `fill_price`",
                    code="invalid_fill",
                )

        qty = to_price(quantity, "fill quantity")
        px = to_price(fill_price, "fill price")

        if not self.is_submitted:
            raise InvalidTransitionError(
                "cannot fill an order that was never submitted",
                code="not_submitted",
                order_id=self.order_id,
            )
        if self.is_terminal:
            raise InvalidTransitionError(
                f"cannot fill a {self.status} order",
                code="invalid_transition",
                order_id=self.order_id,
                current=self.status.value,
            )
        if qty <= ZERO:
            raise OrderValidationError(
                "fill quantity must be positive", code="invalid_fill_quantity"
            )
        if px <= ZERO:
            raise OrderValidationError("fill price must be positive", code="invalid_fill_price")
        if qty > self.remaining_quantity + _DUST:
            raise OrderValidationError(
                "fill would exceed the order quantity",
                code="overfill",
                order_id=self.order_id,
                fill=str(qty),
                remaining=str(self.remaining_quantity),
            )
        qty = min(qty, self.remaining_quantity)

        # Weighted average across every fill so far.
        prior_value = (self.average_fill_price or ZERO) * self.filled_quantity
        self.filled_quantity = quantize_price(self.filled_quantity + qty)
        self.average_fill_price = to_price(
            (prior_value + qty * px) / self.filled_quantity, "average_fill_price"
        )

        if fill is not None:
            self.fills.append(fill)

        stamp = at or _utcnow()
        complete = self.remaining_quantity <= _DUST
        if complete:
            self.update_status(OrderStatus.FILLED, "fully filled", at=stamp)
            self.filled_at = stamp
            self._fire(OrderEvent.FILL, self, fill)
        else:
            self.update_status(OrderStatus.PARTIAL, f"filled {qty}", at=stamp)
            self._fire(OrderEvent.PARTIAL_FILL, self, fill)

        logger.info(
            "fill %s %s @ %s -> %s/%s (%s)",
            self.symbol,
            qty,
            px,
            self.filled_quantity,
            self.quantity,
            self.status,
        )
        return self.status

    # -- triggering and fill logic ----------------------------------------

    def update_trailing(self, current_price: Any) -> Decimal | None:
        """Ratchet a trailing stop toward the favourable direction.

        A trailing **sell** (protecting a long) tracks the high-water mark and
        its stop only ever rises. A trailing **buy** (protecting a short)
        tracks the low-water mark and its stop only ever falls. The stop never
        moves against the position — that is the whole point of a trailing
        stop, and letting it loosen would quietly widen the risk.

        Returns
        -------
        Decimal | None
            The new stop price, or ``None`` for non-trailing orders.
        """
        if self.order_type is not OrderType.TRAILING_STOP:
            return None
        if self.trailing_amount is None:
            raise OrderValidationError(
                "trailing_stop orders require a trailing_amount",
                code="missing_trailing_amount",
            )

        px = to_price(current_price, "price")
        if px <= ZERO:
            raise OrderValidationError("price must be positive", code="invalid_price")

        if self.extreme_price is None:
            self.extreme_price = px
        elif self.is_buy:
            self.extreme_price = min(self.extreme_price, px)
        else:
            self.extreme_price = max(self.extreme_price, px)

        candidate = (
            quantize_price(self.extreme_price + self.trailing_amount)
            if self.is_buy
            else quantize_price(self.extreme_price - self.trailing_amount)
        )

        if self.stop_price is None:
            self.stop_price = candidate
        elif self.is_buy:
            self.stop_price = min(self.stop_price, candidate)
        else:
            self.stop_price = max(self.stop_price, candidate)

        self.updated_at = _utcnow()
        return self.stop_price

    def check_trigger(self, current_price: Any) -> bool:
        """Test whether a stop-family order has breached its trigger.

        Triggering is **sticky**: once fired it stays fired, even if price
        retraces. A stop that un-triggered would behave like a limit order and
        silently change the strategy's risk profile.

        Non-stop orders are considered permanently triggered.
        """
        if not self.order_type.is_stop_family:
            return True
        if self.triggered:
            return True

        px = to_price(current_price, "price")
        if self.order_type is OrderType.TRAILING_STOP:
            self.update_trailing(px)
        if self.stop_price is None:
            return False

        # Buy stops fire on the way up, sell stops on the way down.
        fired = px >= self.stop_price if self.is_buy else px <= self.stop_price
        if fired:
            self.triggered = True
            self.triggered_at = _utcnow()
            logger.info("order %s triggered at %s (stop %s)", self.order_id, px, self.stop_price)
            self._fire(OrderEvent.TRIGGER, self, px)
        return fired

    def is_fillable(self, market_data: Mapping[str, Any] | Any) -> bool:
        """Whether the order could execute against the given market data.

        Accepts the Step 10 quote dict or a bare price. Checks, in order:
        the order is working, the trigger has fired (stop family), and the
        limit condition is satisfied.
        """
        if not self.is_working:
            return False

        quote = _quote(market_data)
        # Buys lift the ask, sells hit the bid.
        reference = quote["ask"] if self.is_buy else quote["bid"]
        assert reference is not None

        if self.order_type.is_stop_family and not self.check_trigger(quote["last"]):
            return False

        if self.order_type is OrderType.MARKET:
            return True
        if self.order_type is OrderType.STOP:
            return True  # becomes a market order once triggered
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            assert self.limit_price is not None
            return reference <= self.limit_price if self.is_buy else reference >= self.limit_price
        if self.order_type is OrderType.TRAILING_STOP:
            return True  # market order once triggered
        return False  # pragma: no cover - exhaustive above

    def calculate_fill_price(self, market_data: Mapping[str, Any] | Any) -> Decimal:
        """The price this order would execute at, before slippage.

        Market and triggered stop orders cross the spread. Limit orders fill
        at the better of the limit and the market — modelling price
        improvement, which is what a real venue gives you.

        Slippage and commission are applied later by Steps 7 and 8; this is
        the clean reference price they adjust.

        Raises
        ------
        ValidationError
            If the order is not fillable against this data, so a caller
            cannot accidentally book a fill that should not have happened.
        """
        quote = _quote(market_data)
        reference = quote["ask"] if self.is_buy else quote["bid"]
        assert reference is not None

        if not self.is_fillable(market_data):
            raise ValidationError(
                f"{self.order_type} order is not fillable at {reference}",
                code="not_fillable",
                order_id=self.order_id,
                reference=str(reference),
            )

        if self.order_type in (OrderType.MARKET, OrderType.STOP, OrderType.TRAILING_STOP):
            return reference

        assert self.limit_price is not None
        return min(reference, self.limit_price) if self.is_buy else max(reference, self.limit_price)

    # -- callbacks ---------------------------------------------------------

    def add_callback(self, event: str, handler: Callable[..., None]) -> None:
        """Register a handler for one of :class:`OrderEvent`'s names.

        Raises
        ------
        ValidationError
            For an unknown event name — a silently-never-called callback is a
            miserable bug to find.
        """
        if event not in OrderEvent.ALL:
            raise ValidationError(
                f"unknown order event {event!r}; expected one of {OrderEvent.ALL}",
                code="unknown_event",
            )
        self._callbacks.setdefault(event, []).append(handler)

    def _fire(self, event: str, *args: Any) -> None:
        """Invoke handlers, never letting one break the order lifecycle.

        A monitoring or alerting callback that raises must not roll back a
        fill that genuinely happened, so exceptions are logged and swallowed.
        """
        for handler in self._callbacks.get(event, []):
            try:
                handler(*args)
            except Exception:  # noqa: BLE001 - deliberate isolation
                logger.exception("order callback %s failed for order %s", event, self.order_id)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot. Decimals become strings to keep precision."""

        def opt(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        def stamp(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "order_id": self.order_id,
            "portfolio_id": self.portfolio_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": str(self.quantity),
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "limit_price": opt(self.limit_price),
            "stop_price": opt(self.stop_price),
            "trailing_amount": opt(self.trailing_amount),
            "average_fill_price": opt(self.average_fill_price),
            "time_in_force": self.time_in_force.value,
            "status": self.status.value,
            "reason_for_rejection": self.reason_for_rejection,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "strategy_name": self.strategy_name,
            "created_at": stamp(self.created_at),
            "submitted_at": stamp(self.submitted_at),
            "filled_at": stamp(self.filled_at),
            "cancelled_at": stamp(self.cancelled_at),
            "updated_at": stamp(self.updated_at),
            "triggered": self.triggered,
            "triggered_at": stamp(self.triggered_at),
            "extreme_price": opt(self.extreme_price),
            "status_history": [change.to_dict() for change in self.status_history],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Order":
        """Rebuild an order from :meth:`to_dict` output.

        ``status`` is restored directly rather than replayed through the state
        machine, so a terminal order round-trips without tripping the
        transition guard.
        """

        def when(key: str) -> datetime | None:
            value = payload.get(key)
            return datetime.fromisoformat(value) if value else None

        order = cls(
            symbol=payload["symbol"],
            side=payload["side"],
            quantity=payload["quantity"],
            order_type=payload.get("order_type", OrderType.MARKET),
            order_id=payload.get("order_id") or str(uuid.uuid4()),
            portfolio_id=payload.get("portfolio_id"),
            exchange=payload.get("exchange", "NSE"),
            limit_price=payload.get("limit_price"),
            stop_price=payload.get("stop_price"),
            trailing_amount=payload.get("trailing_amount"),
            time_in_force=payload.get("time_in_force", TimeInForce.DAY),
            status=payload.get("status", OrderStatus.PENDING),
            filled_quantity=payload.get("filled_quantity", ZERO),
            average_fill_price=payload.get("average_fill_price"),
            reason_for_rejection=payload.get("reason_for_rejection"),
            client_order_id=payload.get("client_order_id"),
            broker_order_id=payload.get("broker_order_id"),
            strategy_name=payload.get("strategy_name"),
            created_at=when("created_at") or _utcnow(),
            triggered=bool(payload.get("triggered", False)),
            extreme_price=payload.get("extreme_price"),
            status_history=[
                StatusChange.from_dict(raw) for raw in payload.get("status_history", [])
            ],
        )
        order.submitted_at = when("submitted_at")
        order.filled_at = when("filled_at")
        order.cancelled_at = when("cancelled_at")
        order.triggered_at = when("triggered_at")
        order.updated_at = when("updated_at") or order.created_at
        return order

    # -- persistence -------------------------------------------------------

    def save_to_db(self, db: "DatabaseManager", portfolio_id: str | None = None) -> str:
        """Upsert this order, returning its ``order_id``.

        ``status_history``, ``triggered`` and ``extreme_price`` are **not**
        persisted — the ``orders`` table has no columns for them. They survive
        in the :meth:`to_dict` JSON snapshot, which is what Step 20 state
        persistence should use. Recorded in the task tracker.

        Raises
        ------
        ValidationError
            If no portfolio id is available.
        """
        from backtest.db.models import Order as OrderRow

        owner = portfolio_id or self.portfolio_id
        if not owner:
            raise ValidationError(
                "portfolio_id is required to save an order",
                code="missing_portfolio_id",
                order_id=self.order_id,
            )
        self.portfolio_id = owner

        with db.session() as session:
            row = session.get(OrderRow, self.order_id)
            if row is None:
                row = OrderRow(order_id=self.order_id)
                session.add(row)
            row.portfolio_id = owner
            row.symbol = self.symbol
            row.exchange = self.exchange
            row.side = self.side.value
            row.order_type = self.order_type.value
            row.quantity = self.quantity
            row.filled_quantity = self.filled_quantity
            row.limit_price = self.limit_price
            row.stop_price = self.stop_price
            row.trailing_amount = self.trailing_amount
            row.average_fill_price = self.average_fill_price
            row.time_in_force = self.time_in_force.value
            row.status = self.status.value
            row.rejection_reason = self.reason_for_rejection
            row.client_order_id = self.client_order_id
            row.broker_order_id = self.broker_order_id
            # submitted_at is NOT NULL in the schema; an unsubmitted order
            # falls back to its creation time rather than failing the insert.
            row.submitted_at = self.submitted_at or self.created_at
            row.filled_at = self.filled_at
            row.cancelled_at = self.cancelled_at
            session.flush()

        logger.debug("order %s saved", self.order_id)
        return self.order_id

    # -- convenience constructors -----------------------------------------

    @classmethod
    def market(cls, symbol: str, side: Any, quantity: Any, **kwargs: Any) -> "Order":
        """Build a market order."""
        return cls(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            **kwargs,
        )

    @classmethod
    def limit(
        cls, symbol: str, side: Any, quantity: Any, limit_price: Any, **kwargs: Any
    ) -> "Order":
        """Build a limit order."""
        return cls(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            **kwargs,
        )

    @classmethod
    def stop(cls, symbol: str, side: Any, quantity: Any, stop_price: Any, **kwargs: Any) -> "Order":
        """Build a stop (stop-loss) order."""
        return cls(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.STOP,
            stop_price=stop_price,
            **kwargs,
        )

    @classmethod
    def stop_limit(
        cls,
        symbol: str,
        side: Any,
        quantity: Any,
        stop_price: Any,
        limit_price: Any,
        **kwargs: Any,
    ) -> "Order":
        """Build a stop-limit order."""
        return cls(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.STOP_LIMIT,
            stop_price=stop_price,
            limit_price=limit_price,
            **kwargs,
        )

    @classmethod
    def trailing_stop(
        cls, symbol: str, side: Any, quantity: Any, trailing_amount: Any, **kwargs: Any
    ) -> "Order":
        """Build a trailing-stop order."""
        return cls(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.TRAILING_STOP,
            trailing_amount=trailing_amount,
            **kwargs,
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<Order {self.side} {self.quantity} {self.symbol} {self.order_type} "
            f"{self.status} filled={self.filled_quantity}>"
        )
