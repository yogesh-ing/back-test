"""Enumerations for the forward testing simulator.

Defined here rather than imported from :mod:`backtest.db.models` so the
simulator package stays free of any ORM dependency — the layering rule in
:mod:`backtest.simulator`. The cost of that independence is a risk of drift,
so ``tests/test_simulator_order.py`` asserts these values match both the ORM
enums and the SQL ``CHECK`` constraints exactly.

Every value is the lowercase string the database stores, so an enum member can
be written straight to a column without conversion.
"""

from __future__ import annotations

import enum

__all__ = [
    "StrEnum",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "TimeInForce",
    "TERMINAL_STATUSES",
    "WORKING_STATUSES",
    "VALID_TRANSITIONS",
]


class StrEnum(str, enum.Enum):
    """String-valued enum that compares and serialises as its plain value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def parse(cls, value: object) -> "StrEnum":
        """Coerce a string or member to a member, case-insensitively.

        Raises
        ------
        ValueError
            With the list of accepted values, so a typo is self-diagnosing.
        """
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(
            f"invalid {cls.__name__} {value!r}; expected one of {cls.values()}"
        )


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """``+1`` for a buy, ``-1`` for a sell.

        Lets fill handling compute a signed quantity without branching.
        """
        return 1 if self is OrderSide.BUY else -1

    @property
    def opposite(self) -> "OrderSide":
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"

    @property
    def needs_limit_price(self) -> bool:
        return self in (OrderType.LIMIT, OrderType.STOP_LIMIT)

    @property
    def needs_stop_price(self) -> bool:
        """Trailing stops derive their stop, so they are excluded here."""
        return self in (OrderType.STOP, OrderType.STOP_LIMIT)

    @property
    def is_stop_family(self) -> bool:
        """True when the order must be triggered before it can fill."""
        return self in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP)


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        """A terminal order will never change again."""
        return self in TERMINAL_STATUSES

    @property
    def is_working(self) -> bool:
        """A working order is still eligible to fill."""
        return self in WORKING_STATUSES


class TimeInForce(StrEnum):
    DAY = "day"
    """Cancelled at the end of the session if unfilled."""

    GTC = "gtc"
    """Good till cancelled."""

    IOC = "ioc"
    """Immediate or cancel: take what is available, cancel the rest."""

    FOK = "fok"
    """Fill or kill: all at once, or nothing."""

    @property
    def is_immediate(self) -> bool:
        """True for order types that must not rest on the book."""
        return self in (TimeInForce.IOC, TimeInForce.FOK)


#: Statuses from which no transition is allowed.
TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
)

#: Statuses in which an order can still receive fills.
WORKING_STATUSES = frozenset({OrderStatus.PENDING, OrderStatus.PARTIAL})

#: The order lifecycle, as an explicit adjacency map.
#:
#: Encoding it as data rather than scattering ``if`` statements means the
#: legal moves can be inspected, tested and documented in one place — and an
#: illegal transition (say FILLED back to PENDING) fails loudly instead of
#: silently corrupting the audit trail.
VALID_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIAL: frozenset(
        {
            OrderStatus.PARTIAL,   # further partial fills
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}
