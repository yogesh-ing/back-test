"""Decimal helpers for the forward testing simulator.

Every monetary value in this package is a :class:`~decimal.Decimal`. Binary
floats cannot represent ``0.1`` exactly, and once a few thousand fills have
accumulated the equity curve stops reconciling with the sum of trade P&L —
a bug that is miserable to track down. The database schema uses ``NUMERIC``
for the same reason (see ``db/DB-IMPLEMENTATION-GUIDE.md`` §5).

Two quantisation levels mirror the schema:

* :data:`MONEY_PLACES` — 4 dp, matching ``NUMERIC(20, 4)`` for cash and P&L
* :data:`PRICE_PLACES` — 8 dp, matching ``NUMERIC(20, 8)`` for prices/quantities
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

__all__ = [
    "ZERO",
    "ONE",
    "MONEY_PLACES",
    "PRICE_PLACES",
    "to_decimal",
    "money",
    "price",
    "quantize_money",
    "quantize_price",
    "is_zero",
]

ZERO = Decimal("0")
ONE = Decimal("1")

#: Cash and P&L precision — ``NUMERIC(20, 4)``.
MONEY_PLACES = Decimal("0.0001")

#: Price and quantity precision — ``NUMERIC(20, 8)``.
PRICE_PLACES = Decimal("0.00000001")


def to_decimal(value: Any, field: str = "value") -> Decimal:
    """Coerce ``value`` to :class:`Decimal` without going through binary float.

    Floats are converted via :func:`repr` so that ``0.1`` becomes
    ``Decimal("0.1")`` rather than the exact binary expansion
    ``0.1000000000000000055511151231257827``.

    Parameters
    ----------
    value:
        A number, numeric string, or ``Decimal``.
    field:
        Field name used in the error message.

    Raises
    ------
    ValueError
        If ``value`` is ``None`` or cannot be interpreted as a number.

    Examples
    --------
    >>> to_decimal(0.1)
    Decimal('0.1')
    >>> to_decimal("1500.50")
    Decimal('1500.50')
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field} must be finite, got {value}")
        return value
    if value is None:
        raise ValueError(f"{field} must not be None")
    if isinstance(value, bool):
        # bool is an int subclass; silently treating True as 1 hides bugs.
        raise ValueError(f"{field} must be numeric, got bool {value!r}")
    try:
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{field} must be finite, got {value}")
            return Decimal(repr(value))
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be numeric, got {value!r}") from exc


def quantize_money(value: Decimal) -> Decimal:
    """Round to 4 decimal places, half-up (what humans expect for currency)."""
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def quantize_price(value: Decimal) -> Decimal:
    """Round to 8 decimal places, half-up."""
    return value.quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)


def money(value: Any, field: str = "amount") -> Decimal:
    """Coerce to a money-precision :class:`Decimal` (4 dp)."""
    return quantize_money(to_decimal(value, field))


def price(value: Any, field: str = "price") -> Decimal:
    """Coerce to a price-precision :class:`Decimal` (8 dp)."""
    return quantize_price(to_decimal(value, field))


def is_zero(value: Decimal, tolerance: Decimal = PRICE_PLACES) -> bool:
    """True when ``value`` is zero within one quantisation step.

    Guards against a residual dust quantity such as ``1E-12`` keeping a
    position technically "open" forever after a full close.
    """
    return abs(value) < tolerance