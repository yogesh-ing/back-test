"""Tax-lot accounting for the forward testing simulator.

A :class:`LotBook` records the individual tranches that make up a position and
decides which ones a partial close consumes. That choice — FIFO, LIFO or
weighted average — changes both the realised P&L of the closing trade and the
cost basis of whatever remains, so it must be explicit rather than implied.

Worked example. Buy 10 @ 100, then 10 @ 120, then sell 10 @ 130:

===========  ==============  ==========================
Method       Realised P&L    Remaining basis
===========  ==============  ==========================
FIFO         300  (130−100)  10 @ 120
LIFO         100  (130−120)  10 @ 100
AVERAGE      200  (130−110)  10 @ 110
===========  ==============  ==========================

Same trades, three different answers. India's tax rules mandate FIFO for
equity delivery, which is why it is offered alongside the average-cost model
the backtest engine uses.

Invariant
---------
:attr:`LotBook.total_quantity` always equals the absolute size of the position
that owns it, and :attr:`LotBook.weighted_average_price` always equals that
position's ``average_entry_price``. Under ``AVERAGE`` the book is collapsed to
a single lot after every mutation, which keeps the second half of that
invariant true by construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence

from backtest.simulator.errors import ValidationError
from backtest.simulator.money import (
    ZERO,
    is_zero,
    price as to_price,
    quantize_price,
    to_decimal,
)

__all__ = ["CostBasisMethod", "Lot", "LotConsumption", "LotBook"]

#: Tolerance for "is this quantity used up?" comparisons — one unit of the
#: 8-dp quantity grid.
_DUST = Decimal("0.00000001")


class CostBasisMethod:
    """How a partial close picks which lots to consume."""

    FIFO = "fifo"
    """Oldest lots first. Required for Indian equity delivery."""

    LIFO = "lifo"
    """Newest lots first."""

    AVERAGE = "average"
    """One pooled cost. Matches the vectorised backtest engine's model."""

    ALL = (FIFO, LIFO, AVERAGE)

    @classmethod
    def validate(cls, method: str) -> str:
        normalised = str(method).strip().lower()
        if normalised not in cls.ALL:
            raise ValidationError(
                f"unknown cost basis method {method!r}; expected one of {cls.ALL}",
                code="invalid_cost_basis_method",
            )
        return normalised


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Lot:
    """One acquisition tranche.

    ``quantity`` is always **positive** — direction belongs to the position,
    not to the lot. A short position's lots record shares sold short.
    """

    quantity: Decimal
    price: Decimal
    acquired_at: datetime = field(default_factory=_utcnow)
    lot_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        self.quantity = to_price(self.quantity, "lot quantity")
        self.price = to_price(self.price, "lot price")
        if self.quantity <= ZERO:
            raise ValidationError(
                "lot quantity must be positive", code="invalid_lot_quantity"
            )
        if self.price <= ZERO:
            raise ValidationError("lot price must be positive", code="invalid_lot_price")

    @property
    def cost(self) -> Decimal:
        """Absolute cost of this tranche."""
        return self.quantity * self.price

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "acquired_at": self.acquired_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Lot":
        return cls(
            quantity=payload["quantity"],
            price=payload["price"],
            acquired_at=datetime.fromisoformat(payload["acquired_at"]),
            lot_id=payload.get("lot_id") or str(uuid.uuid4()),
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Lot {self.quantity} @ {self.price}>"


@dataclass(frozen=True)
class LotConsumption:
    """Record of one lot (or part of one) being closed.

    Feeds the Step 18 trade analyzer, which reports holding period and P&L
    per tax lot rather than per position.
    """

    lot_id: str
    quantity: Decimal
    entry_price: Decimal
    acquired_at: datetime

    def realized_pnl(self, exit_price: Decimal, is_long: bool) -> Decimal:
        """Gross P&L for this slice at ``exit_price``."""
        if is_long:
            return (exit_price - self.entry_price) * self.quantity
        return (self.entry_price - exit_price) * self.quantity

    def holding_period(self, exit_time: datetime) -> Any:
        """``timedelta`` this slice was held."""
        return exit_time - self.acquired_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "acquired_at": self.acquired_at.isoformat(),
        }


class LotBook:
    """The open lots of a single position, in acquisition order.

    Parameters
    ----------
    method:
        A :class:`CostBasisMethod` value. Under ``AVERAGE`` the book keeps
        exactly one pooled lot.
    lots:
        Existing lots, oldest first. Used when restoring saved state.
    """

    def __init__(
        self,
        method: str = CostBasisMethod.AVERAGE,
        lots: Iterable[Lot] | None = None,
    ) -> None:
        self.method = CostBasisMethod.validate(method)
        self._lots: list[Lot] = list(lots or [])
        if self.method == CostBasisMethod.AVERAGE and len(self._lots) > 1:
            self.collapse()

    # -- inspection --------------------------------------------------------

    @property
    def lots(self) -> Sequence[Lot]:
        """Open lots, oldest first. Read-only view."""
        return tuple(self._lots)

    def __len__(self) -> int:
        return len(self._lots)

    def __iter__(self) -> Iterator[Lot]:
        return iter(self._lots)

    def __bool__(self) -> bool:
        return bool(self._lots)

    @property
    def total_quantity(self) -> Decimal:
        """Sum of all open lot quantities. Always non-negative."""
        return quantize_price(sum((lot.quantity for lot in self._lots), ZERO))

    @property
    def total_cost(self) -> Decimal:
        """Sum of ``quantity × price`` across open lots."""
        return sum((lot.cost for lot in self._lots), ZERO)

    @property
    def weighted_average_price(self) -> Decimal:
        """Cost-weighted average entry price. ``0`` when the book is empty."""
        total = self.total_quantity
        if is_zero(total):
            return ZERO
        return to_price(self.total_cost / total, "average price")

    # -- mutation ----------------------------------------------------------

    def add(self, quantity: Any, at_price: Any, acquired_at: datetime | None = None) -> Lot:
        """Append a tranche.

        Under ``AVERAGE`` the book is collapsed afterwards, so the returned
        lot is the pooled one rather than the tranche just added.
        """
        lot = Lot(
            quantity=abs(to_price(quantity, "quantity")),
            price=at_price,
            acquired_at=acquired_at or _utcnow(),
        )
        self._lots.append(lot)
        if self.method == CostBasisMethod.AVERAGE:
            return self.collapse()
        return lot

    def consume(self, quantity: Any) -> list[LotConsumption]:
        """Remove ``quantity`` from the book, following the configured method.

        Returns one :class:`LotConsumption` per lot touched, in the order they
        were consumed. Lots are split when the requested size falls partway
        through one.

        Raises
        ------
        ValidationError
            If ``quantity`` exceeds what is open. Over-consumption is refused
            rather than clamped, because silently closing less than asked
            would leave the caller's cash and P&L arithmetic wrong.
        """
        wanted = abs(to_price(quantity, "quantity"))
        if is_zero(wanted):
            raise ValidationError("quantity must be non-zero", code="zero_quantity")

        available = self.total_quantity
        if wanted > available + _DUST:
            raise ValidationError(
                "cannot consume more than the open quantity",
                code="over_consume",
                requested=str(wanted),
                available=str(available),
            )
        wanted = min(wanted, available)

        # FIFO walks forward, LIFO backward. AVERAGE has a single lot, so the
        # order is irrelevant and FIFO's forward walk is used.
        indices = (
            range(len(self._lots) - 1, -1, -1)
            if self.method == CostBasisMethod.LIFO
            else range(len(self._lots))
        )

        consumed: list[LotConsumption] = []
        remaining = wanted
        exhausted: list[int] = []

        for index in indices:
            if remaining <= ZERO:
                break
            lot = self._lots[index]
            take = min(lot.quantity, remaining)
            consumed.append(
                LotConsumption(
                    lot_id=lot.lot_id,
                    quantity=quantize_price(take),
                    entry_price=lot.price,
                    acquired_at=lot.acquired_at,
                )
            )
            leftover = quantize_price(lot.quantity - take)
            if is_zero(leftover):
                exhausted.append(index)
            else:
                lot.quantity = leftover
            remaining = quantize_price(remaining - take)

        for index in sorted(exhausted, reverse=True):
            del self._lots[index]

        return consumed

    def collapse(self) -> Lot:
        """Merge every lot into one priced at the weighted average.

        Used by ``AVERAGE`` after each mutation so that
        :attr:`weighted_average_price` and the owning position's
        ``average_entry_price`` can never drift apart.

        The merged lot keeps the **oldest** acquisition time, which is the
        conservative choice for holding-period reporting.
        """
        if not self._lots:
            raise ValidationError("cannot collapse an empty lot book", code="empty_book")
        if len(self._lots) == 1:
            return self._lots[0]

        merged = Lot(
            quantity=self.total_quantity,
            price=self.weighted_average_price,
            acquired_at=min(lot.acquired_at for lot in self._lots),
        )
        self._lots = [merged]
        return merged

    def apply_split(self, ratio: Any) -> None:
        """Adjust every lot for a stock split.

        ``ratio`` is new shares per old share: ``2`` for a 2-for-1 split,
        ``Decimal("0.5")`` for a 1-for-2 reverse split. Quantities scale up and
        prices scale down, so the total cost of each lot is preserved.
        """
        factor = to_decimal(ratio, "split ratio")
        if factor <= ZERO:
            raise ValidationError(
                "split ratio must be positive", code="invalid_split_ratio", ratio=str(factor)
            )
        for lot in self._lots:
            lot.quantity = quantize_price(lot.quantity * factor)
            lot.price = quantize_price(lot.price / factor)
            if lot.price <= ZERO:  # pragma: no cover - needs an absurd ratio
                raise ValidationError(
                    "split would round a lot price to zero", code="invalid_split_ratio"
                )
        # Drop any lot rounded out of existence by an extreme reverse split.
        self._lots = [lot for lot in self._lots if not is_zero(lot.quantity)]

    def reduce_cost_basis(self, per_share: Any) -> None:
        """Lower every lot's price by ``per_share``, floored at a positive tick.

        Used for the cost-basis treatment of a dividend. Flooring rather than
        allowing zero or negative keeps ``Lot``'s positive-price invariant and
        avoids a divide-by-zero in percentage returns.
        """
        amount = to_decimal(per_share, "dividend")
        if amount < ZERO:
            raise ValidationError(
                "dividend must not be negative", code="invalid_dividend"
            )
        for lot in self._lots:
            lot.price = max(quantize_price(lot.price - amount), _DUST)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "lots": [lot.to_dict() for lot in self._lots]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LotBook":
        return cls(
            method=payload.get("method", CostBasisMethod.AVERAGE),
            lots=[Lot.from_dict(raw) for raw in payload.get("lots", [])],
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<LotBook {self.method} lots={len(self._lots)} qty={self.total_quantity}>"
