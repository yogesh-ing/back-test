"""Position model for the forward testing simulator.

.. note::
   This is the **base** implementation introduced in Step 3, covering what
   :class:`~backtest.simulator.portfolio.Portfolio` needs: signed quantity,
   weighted-average entry price, mark-to-market, partial reduction and
   realised P&L. Step 4 extends it with explicit FIFO/LIFO lot accounting and
   split/dividend adjustment. Nothing here is throwaway — Step 4 is additive.

Sign convention
---------------
``quantity`` is **signed**: positive is long, negative is short, zero means
closed. One field therefore encodes both size and direction, which keeps the
P&L arithmetic identical for both sides and matches the ``positions`` table.

P&L convention
--------------
``unrealized_pnl`` and ``realized_pnl`` are **gross of commission**.
Commissions accumulate separately in ``commission_total``. This mirrors the
schema, where ``trades.gross_pnl`` and ``trades.commission_total`` are
distinct columns, and keeps cost attribution possible in Step 18.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from backtest.simulator.errors import ValidationError
from backtest.simulator.money import (
    ZERO,
    is_zero,
    money,
    price as to_price,
    quantize_money,
    to_decimal,
)

__all__ = ["Position", "PositionType", "ReduceResult"]

logger = logging.getLogger("backtest.simulator.position")


class PositionType:
    """Direction constants, matching ``positions.position_type`` in the schema."""

    LONG = "long"
    SHORT = "short"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ReduceResult:
    """Outcome of reducing or closing part of a position."""

    quantity_closed: Decimal
    realized_pnl: Decimal
    """Gross of commission."""
    commission: Decimal
    cash_delta: Decimal
    """Signed change to portfolio cash, commission already applied."""
    fully_closed: bool


@dataclass
class Position:
    """An open (or historical) exposure in one symbol.

    Parameters
    ----------
    symbol:
        Instrument identifier, e.g. ``"INFY"``.
    quantity:
        Signed size. Positive long, negative short. Must not be zero at
        construction — a position with no size is not a position.
    average_entry_price:
        Weighted-average cost of the current exposure. Must be positive.

    Raises
    ------
    ValidationError
        If quantity is zero, price is non-positive, or the declared
        ``position_type`` contradicts the sign of ``quantity``.

    Examples
    --------
    >>> p = Position(symbol="INFY", quantity=10, average_entry_price=1500)
    >>> p.update_price(1512)
    >>> p.unrealized_pnl
    Decimal('120.0000')
    """

    symbol: str
    quantity: Decimal
    average_entry_price: Decimal

    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    portfolio_id: str | None = None
    exchange: str = "NSE"
    current_price: Decimal | None = None
    realized_pnl: Decimal = ZERO
    commission_total: Decimal = ZERO
    opened_at: datetime = field(default_factory=_utcnow)
    closed_at: datetime | None = None
    last_updated: datetime = field(default_factory=_utcnow)
    strategy_name: str | None = None

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol).strip().upper()
        if not self.symbol:
            raise ValidationError("symbol must not be empty", code="invalid_symbol")

        self.quantity = to_price(self.quantity, "quantity")
        self.average_entry_price = to_price(self.average_entry_price, "average_entry_price")
        self.realized_pnl = money(self.realized_pnl, "realized_pnl")
        self.commission_total = money(self.commission_total, "commission_total")
        if self.current_price is not None:
            self.current_price = to_price(self.current_price, "current_price")

        if is_zero(self.quantity):
            raise ValidationError(
                "cannot create a position with zero quantity",
                code="zero_quantity",
                symbol=self.symbol,
            )
        if self.average_entry_price <= ZERO:
            raise ValidationError(
                "average_entry_price must be positive",
                code="invalid_price",
                symbol=self.symbol,
                price=str(self.average_entry_price),
            )

    # -- direction ---------------------------------------------------------

    @property
    def position_type(self) -> str:
        """``"long"`` or ``"short"``, derived from the sign of ``quantity``."""
        return PositionType.LONG if self.quantity > ZERO else PositionType.SHORT

    @property
    def is_long(self) -> bool:
        return self.quantity > ZERO

    @property
    def is_short(self) -> bool:
        return self.quantity < ZERO

    @property
    def is_open(self) -> bool:
        return not is_zero(self.quantity)

    @property
    def status(self) -> str:
        """``"open"`` or ``"closed"``, matching ``positions.status``."""
        return "open" if self.is_open else "closed"

    # -- valuation ---------------------------------------------------------

    @property
    def effective_price(self) -> Decimal:
        """Latest mark, falling back to entry price before the first update."""
        return self.current_price if self.current_price is not None else self.average_entry_price

    @property
    def market_value(self) -> Decimal:
        """Signed mark-to-market value.

        Negative for shorts, which is what makes
        ``equity = cash + position_value`` work for both directions.
        """
        return quantize_money(self.quantity * self.effective_price)

    @property
    def cost_basis(self) -> Decimal:
        """Signed value at entry. Negative for shorts."""
        return quantize_money(self.quantity * self.average_entry_price)

    @property
    def notional(self) -> Decimal:
        """Absolute exposure, ignoring direction. Always non-negative."""
        return abs(self.market_value)

    @property
    def unrealized_pnl(self) -> Decimal:
        """Open P&L at the current mark, gross of commission.

        The signed-quantity convention makes one formula serve both
        directions: a short has negative quantity, so a fall in price yields
        a positive result.
        """
        if not self.is_open:
            return ZERO
        return quantize_money(self.quantity * (self.effective_price - self.average_entry_price))

    @property
    def unrealized_pnl_percentage(self) -> Decimal:
        """Open P&L as a fraction of absolute cost basis (``0.05`` = +5%)."""
        basis = abs(self.cost_basis)
        if basis == ZERO:
            return ZERO
        return (self.unrealized_pnl / basis).quantize(Decimal("0.000001"))

    @property
    def total_pnl(self) -> Decimal:
        """Realised plus unrealised, gross of commission."""
        return quantize_money(self.realized_pnl + self.unrealized_pnl)

    @property
    def net_pnl(self) -> Decimal:
        """Total P&L after all commission paid on this position."""
        return quantize_money(self.total_pnl - self.commission_total)

    def is_profitable(self) -> bool:
        """True when net P&L is positive."""
        return self.net_pnl > ZERO

    def get_pnl_at_price(self, candidate: Any) -> Decimal:
        """Unrealised P&L if the mark were ``candidate``, without mutating state.

        Used by the Step 16 stop/target manager to evaluate exit levels.
        """
        target = to_price(candidate, "price")
        if target <= ZERO:
            raise ValidationError("price must be positive", code="invalid_price")
        if not self.is_open:
            return ZERO
        return quantize_money(self.quantity * (target - self.average_entry_price))

    # -- mutation ----------------------------------------------------------

    def update_price(self, new_price: Any) -> Decimal:
        """Mark the position to ``new_price`` and return unrealised P&L."""
        candidate = to_price(new_price, "price")
        if candidate <= ZERO:
            raise ValidationError(
                "price must be positive",
                code="invalid_price",
                symbol=self.symbol,
                price=str(candidate),
            )
        self.current_price = candidate
        self.last_updated = _utcnow()
        return self.unrealized_pnl

    def add_shares(self, quantity: Any, at_price: Any, commission: Any = ZERO) -> Decimal:
        """Increase exposure in the current direction, re-averaging entry price.

        Parameters
        ----------
        quantity:
            Magnitude to add. Sign is ignored; direction comes from the
            existing position, so a short grows more negative.

        Returns
        -------
        Decimal
            Signed cash delta for the portfolio (negative for a long buy,
            positive for a short sell), commission included.

        Raises
        ------
        ValidationError
            If the position is closed, or quantity/price are invalid.
        """
        if not self.is_open:
            raise ValidationError(
                "cannot add to a closed position",
                code="position_closed",
                symbol=self.symbol,
            )
        qty = abs(to_price(quantity, "quantity"))
        px = to_price(at_price, "price")
        fee = money(commission, "commission")
        if is_zero(qty):
            raise ValidationError("quantity must be non-zero", code="zero_quantity")
        if px <= ZERO:
            raise ValidationError("price must be positive", code="invalid_price")
        if fee < ZERO:
            raise ValidationError("commission must not be negative", code="invalid_commission")

        signed = qty if self.is_long else -qty
        old_qty = self.quantity

        # Weighted average over absolute sizes; signs cancel otherwise.
        total_cost = (abs(old_qty) * self.average_entry_price) + (qty * px)
        new_abs = abs(old_qty) + qty
        self.average_entry_price = to_price(total_cost / new_abs, "average_entry_price")
        self.quantity = to_price(old_qty + signed, "quantity")
        self.commission_total = quantize_money(self.commission_total + fee)
        self.last_updated = _utcnow()

        # Long: pay out. Short: receive proceeds. Commission always a cost.
        cash_delta = quantize_money((-qty * px if self.is_long else qty * px) - fee)
        logger.debug(
            "add_shares %s %s @ %s -> qty=%s avg=%s",
            self.symbol, qty, px, self.quantity, self.average_entry_price,
        )
        return cash_delta

    def reduce_shares(
        self, quantity: Any, at_price: Any, commission: Any = ZERO
    ) -> ReduceResult:
        """Close part (or all) of the position, realising P&L.

        The average entry price is deliberately **unchanged** by a partial
        close: the remaining shares keep their original cost basis. Only the
        closed portion realises P&L.

        Raises
        ------
        ValidationError
            If the position is closed, or the requested size exceeds what is
            open. Over-reducing is rejected rather than silently flipping the
            position to the opposite direction, which would hide a bug in the
            caller.
        """
        if not self.is_open:
            raise ValidationError(
                "cannot reduce a closed position",
                code="position_closed",
                symbol=self.symbol,
            )
        qty = abs(to_price(quantity, "quantity"))
        px = to_price(at_price, "price")
        fee = money(commission, "commission")
        if is_zero(qty):
            raise ValidationError("quantity must be non-zero", code="zero_quantity")
        if px <= ZERO:
            raise ValidationError("price must be positive", code="invalid_price")
        if fee < ZERO:
            raise ValidationError("commission must not be negative", code="invalid_commission")

        open_qty = abs(self.quantity)
        if qty > open_qty + Decimal("0.00000001"):
            raise ValidationError(
                "cannot reduce more than the open quantity",
                code="over_reduce",
                symbol=self.symbol,
                requested=str(qty),
                open=str(open_qty),
            )
        qty = min(qty, open_qty)

        was_long = self.is_long
        # Direction-aware realised P&L.
        realized = quantize_money(
            (px - self.average_entry_price) * qty
            if was_long
            else (self.average_entry_price - px) * qty
        )

        signed_remaining = self.quantity - (qty if was_long else -qty)
        self.quantity = to_price(signed_remaining, "quantity")
        self.realized_pnl = quantize_money(self.realized_pnl + realized)
        self.commission_total = quantize_money(self.commission_total + fee)
        self.current_price = px
        self.last_updated = _utcnow()

        fully_closed = is_zero(self.quantity)
        if fully_closed:
            self.quantity = ZERO
            self.closed_at = self.last_updated

        # Long: receive proceeds. Short: pay to buy back.
        cash_delta = quantize_money((qty * px if was_long else -qty * px) - fee)
        logger.debug(
            "reduce_shares %s %s @ %s -> realized=%s remaining=%s",
            self.symbol, qty, px, realized, self.quantity,
        )
        return ReduceResult(
            quantity_closed=qty,
            realized_pnl=realized,
            commission=fee,
            cash_delta=cash_delta,
            fully_closed=fully_closed,
        )

    def close(self, at_price: Any, commission: Any = ZERO) -> ReduceResult:
        """Close the entire position at ``at_price``."""
        return self.reduce_shares(abs(self.quantity), at_price, commission)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot. Decimals become strings to preserve precision."""
        return {
            "position_id": self.position_id,
            "portfolio_id": self.portfolio_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "position_type": self.position_type,
            "quantity": str(self.quantity),
            "average_entry_price": str(self.average_entry_price),
            "current_price": str(self.current_price) if self.current_price is not None else None,
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "commission_total": str(self.commission_total),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "last_updated": self.last_updated.isoformat(),
            "status": self.status,
            "strategy_name": self.strategy_name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Position":
        """Rebuild from :meth:`to_dict` output.

        Closed positions are reconstructed by restoring ``quantity`` after
        construction, since the constructor rejects zero quantity.
        """
        raw_qty = to_decimal(payload["quantity"], "quantity")
        bootstrap = raw_qty if not is_zero(raw_qty) else Decimal("1")

        pos = cls(
            symbol=payload["symbol"],
            quantity=bootstrap,
            average_entry_price=payload["average_entry_price"],
            position_id=payload.get("position_id") or str(uuid.uuid4()),
            portfolio_id=payload.get("portfolio_id"),
            exchange=payload.get("exchange", "NSE"),
            current_price=payload.get("current_price"),
            realized_pnl=payload.get("realized_pnl", ZERO),
            commission_total=payload.get("commission_total", ZERO),
            strategy_name=payload.get("strategy_name"),
        )
        if is_zero(raw_qty):
            pos.quantity = ZERO
        for key in ("opened_at", "closed_at", "last_updated"):
            value = payload.get(key)
            if value:
                setattr(pos, key, datetime.fromisoformat(value))
        return pos

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<Position {self.symbol} {self.position_type} qty={self.quantity} "
            f"avg={self.average_entry_price} pnl={self.total_pnl}>"
        )
