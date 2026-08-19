"""Fill (execution) model for the forward testing simulator.

A :class:`Fill` is the record of one execution: how much traded, at what
price, and every cost attached to it. Fills are the ground truth of a run —
positions, cash, trades and the equity curve are all derived from them.

Immutability
------------
:class:`Fill` is a **frozen** dataclass. An execution is a historical fact:
once the venue reports it, nothing should be able to rewrite it. Mutating a
fill after the fact would desynchronise the position, the cash balance and the
equity curve with no audit trail of what changed. Corrections are modelled as
a new offsetting fill, which is what a real broker does too.

Cost model
----------
Four cost components are tracked separately rather than lumped into one
number, because attribution matters when a forward test underperforms its
backtest (Step 22):

======================  ==========================================
``commission``          Broker charge, from a
                        :mod:`~backtest.simulator.commission` model
``exchange_fees``       Venue charges
``regulatory_fees``     Statutory charges (STT, stamp duty, SEBI... in Step 8)
``slippage_amount``     Execution shortfall vs ``reference_price``
======================  ==========================================

Slippage is **not** a fee — it is not paid to anyone, it is already inside
``fill_price``. It is recorded for attribution only, and deliberately excluded
from :meth:`Fill.calculate_total_cost` so cash never gets double-counted.

Sign conventions
----------------
``quantity`` is always positive; direction lives in ``side``. Signed slippage
is positive when **adverse** to the order's side: a buy that paid more than
the reference, or a sell that received less.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from backtest.simulator.commission import CommissionModel, resolve_commission_model
from backtest.simulator.enums import OrderSide
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import (
    ZERO,
    money,
    price as to_price,
    quantize_money,
    quantize_price,
)

if TYPE_CHECKING:  # pragma: no cover
    from backtest.db.manager import DatabaseManager
    from backtest.simulator.order import Order
    from backtest.simulator.position import Position

__all__ = ["Fill", "LiquidityFlag", "PositionImpact", "PositionAction"]

logger = logging.getLogger("backtest.simulator.fill")

_DUST = Decimal("0.00000001")
_BPS = Decimal("10000")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiquidityFlag:
    """Whether the execution added or removed liquidity."""

    MAKER = "maker"
    """Resting order that was hit. Often cheaper, sometimes rebated."""

    TAKER = "taker"
    """Crossed the spread. Market orders are always takers."""

    ALL = (MAKER, TAKER)


class PositionAction:
    """What a fill does to a position."""

    OPEN = "open"
    INCREASE = "increase"
    REDUCE = "reduce"
    CLOSE = "close"
    REVERSE = "reverse"
    """Would cross through zero — refused; see :meth:`Fill.impact_on_position`."""

    ALL = (OPEN, INCREASE, REDUCE, CLOSE, REVERSE)


@dataclass(frozen=True)
class PositionImpact:
    """What a fill does (or would do) to a position.

    Returned by both :meth:`Fill.impact_on_position` (a pure preview) and
    :meth:`Fill.apply_to_position` (which mutates).
    """

    action: str
    quantity: Decimal
    cash_delta: Decimal
    """Signed change to portfolio cash, all fees included."""
    realized_pnl: Decimal = ZERO
    """Gross of commission; only non-zero when reducing."""
    fully_closed: bool = False
    resulting_quantity: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "quantity": str(self.quantity),
            "cash_delta": str(self.cash_delta),
            "realized_pnl": str(self.realized_pnl),
            "fully_closed": self.fully_closed,
            "resulting_quantity": str(self.resulting_quantity),
        }


@dataclass(frozen=True)
class Fill:
    """One execution against an order. Immutable once created.

    Parameters
    ----------
    symbol, side, quantity, fill_price:
        The execution itself. ``quantity`` must be positive.
    reference_price:
        The decision price this fill is measured against — the quote the
        strategy saw. Slippage is derived from the difference. Optional, but
        without it slippage attribution is impossible.
    commission, exchange_fees, regulatory_fees:
        Costs. All must be non-negative, matching ``ck_fills_fees_nonneg``.

    Raises
    ------
    ValidationError
        On non-positive quantity or price, negative fees, or an unknown
        liquidity flag.

    Examples
    --------
    >>> f = Fill(symbol="INFY", side="buy", quantity=10,
    ...          fill_price=1501, reference_price=1500, commission=5)
    >>> f.slippage_bps
    Decimal('6.666667')
    >>> f.calculate_total_cost()
    Decimal('15015.0000')
    """

    symbol: str
    side: OrderSide
    quantity: Decimal
    fill_price: Decimal

    fill_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str | None = None
    position_id: str | None = None

    commission: Decimal = ZERO
    exchange_fees: Decimal = ZERO
    regulatory_fees: Decimal = ZERO
    reference_price: Decimal | None = None
    liquidity_flag: str | None = None

    filled_at: datetime = field(default_factory=_utcnow)
    created_at: datetime = field(default_factory=_utcnow)
    strategy_name: str | None = None

    def __post_init__(self) -> None:
        # frozen=True blocks normal assignment, so normalisation goes through
        # object.__setattr__. This is the standard escape hatch for validating
        # a frozen dataclass at construction time only.
        setattr_ = object.__setattr__

        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise ValidationError("symbol must not be empty", code="invalid_symbol")
        setattr_(self, "symbol", symbol)

        try:
            setattr_(self, "side", OrderSide.parse(self.side))
        except ValueError as exc:
            raise ValidationError(str(exc), code="invalid_side") from exc

        setattr_(self, "quantity", to_price(self.quantity, "quantity"))
        setattr_(self, "fill_price", to_price(self.fill_price, "fill_price"))
        for name in ("commission", "exchange_fees", "regulatory_fees"):
            setattr_(self, name, money(getattr(self, name), name))
        if self.reference_price is not None:
            setattr_(self, "reference_price", to_price(self.reference_price, "reference_price"))

        if self.quantity <= ZERO:
            raise ValidationError(
                "fill quantity must be positive; direction belongs to `side`",
                code="invalid_quantity",
                quantity=str(self.quantity),
            )
        if self.fill_price <= ZERO:
            raise ValidationError(
                "fill_price must be positive", code="invalid_price",
                price=str(self.fill_price),
            )
        if self.reference_price is not None and self.reference_price <= ZERO:
            raise ValidationError(
                "reference_price must be positive when set", code="invalid_price"
            )
        for name in ("commission", "exchange_fees", "regulatory_fees"):
            if getattr(self, name) < ZERO:
                raise ValidationError(
                    f"{name} must not be negative",
                    code="invalid_fee",
                    field=name,
                )
        if self.liquidity_flag is not None and self.liquidity_flag not in LiquidityFlag.ALL:
            raise ValidationError(
                f"unknown liquidity_flag {self.liquidity_flag!r}; "
                f"expected one of {LiquidityFlag.ALL}",
                code="invalid_liquidity_flag",
            )

    # -- derived values ----------------------------------------------------

    @property
    def is_buy(self) -> bool:
        return self.side is OrderSide.BUY

    @property
    def signed_quantity(self) -> Decimal:
        """Positive for a buy, negative for a sell."""
        return quantize_price(self.quantity * self.side.sign)

    @property
    def gross_value(self) -> Decimal:
        """Notional traded, before any fees."""
        return quantize_money(self.quantity * self.fill_price)

    @property
    def total_fees(self) -> Decimal:
        """Commission plus exchange and regulatory charges.

        Excludes slippage, which is already inside ``fill_price``.
        """
        return quantize_money(
            self.commission + self.exchange_fees + self.regulatory_fees
        )

    @property
    def slippage_per_share(self) -> Decimal:
        """Signed execution shortfall per share; positive is adverse.

        A buy filled above the reference and a sell filled below it both give
        a positive number, so "higher is worse" holds for either side.
        """
        if self.reference_price is None:
            return ZERO
        return quantize_price(
            (self.fill_price - self.reference_price) * self.side.sign
        )

    @property
    def slippage_bps(self) -> Decimal:
        """Signed slippage in basis points of the reference price."""
        if self.reference_price is None or self.reference_price == ZERO:
            return ZERO
        return (self.slippage_per_share / self.reference_price * _BPS).quantize(
            Decimal("0.000001")
        )

    @property
    def slippage_amount(self) -> Decimal:
        """Signed slippage cost for the whole fill."""
        return quantize_money(self.slippage_per_share * self.quantity)

    @property
    def total_cost_of_trading(self) -> Decimal:
        """Fees plus slippage — the full drag versus a frictionless fill.

        This is the attribution number for Step 22, **not** a cash movement.
        """
        return quantize_money(self.total_fees + self.slippage_amount)

    # -- required methods --------------------------------------------------

    def calculate_slippage_amount(self) -> Decimal:
        """Signed slippage cost for this fill. See :attr:`slippage_amount`."""
        return self.slippage_amount

    def calculate_total_cost(self) -> Decimal:
        """Total cash outlay for a buy, or net proceeds for a sell.

        Both are returned as **positive magnitudes**; use
        :meth:`calculate_cash_delta` for the signed movement.

        A buy costs ``gross + fees``; a sell yields ``gross − fees``. Slippage
        is excluded — it is already embedded in ``fill_price``, and adding it
        would double-count.
        """
        if self.is_buy:
            return quantize_money(self.gross_value + self.total_fees)
        return quantize_money(self.gross_value - self.total_fees)

    def calculate_cash_delta(self) -> Decimal:
        """Signed change to portfolio cash: negative for a buy."""
        return quantize_money(
            -self.calculate_total_cost() if self.is_buy else self.calculate_total_cost()
        )

    def calculate_net_price(self) -> Decimal:
        """Effective per-share price including every fee.

        The number to compare against a strategy's target price: a buy's
        effective price is above the fill, a sell's is below.
        """
        per_share = self.total_fees / self.quantity
        return quantize_price(
            self.fill_price + per_share if self.is_buy else self.fill_price - per_share
        )

    def impact_on_position(self, position: "Position | None" = None) -> PositionImpact:
        """Preview this fill's effect on ``position`` **without** mutating it.

        Pass ``None`` to describe opening a fresh position. Used by the Step 15
        risk manager to evaluate a fill before committing to it.

        A fill larger than an opposing position is reported as
        :attr:`PositionAction.REVERSE` rather than being silently split, and
        :meth:`apply_to_position` refuses it — flipping long to short in one
        step hides a sizing bug and breaks per-trade P&L attribution.
        """
        if position is None or not position.is_open:
            return PositionImpact(
                action=PositionAction.OPEN,
                quantity=self.quantity,
                cash_delta=self.calculate_cash_delta(),
                resulting_quantity=self.signed_quantity,
            )

        same_direction = (position.quantity > ZERO) == self.is_buy
        if same_direction:
            return PositionImpact(
                action=PositionAction.INCREASE,
                quantity=self.quantity,
                cash_delta=self.calculate_cash_delta(),
                resulting_quantity=quantize_price(
                    position.quantity + self.signed_quantity
                ),
            )

        open_qty = abs(position.quantity)
        if self.quantity > open_qty + _DUST:
            return PositionImpact(
                action=PositionAction.REVERSE,
                quantity=self.quantity,
                cash_delta=self.calculate_cash_delta(),
                resulting_quantity=quantize_price(
                    position.quantity + self.signed_quantity
                ),
            )

        closing = min(self.quantity, open_qty)
        realized = (
            (self.fill_price - position.average_entry_price) * closing
            if position.quantity > ZERO
            else (position.average_entry_price - self.fill_price) * closing
        )
        fully = closing >= open_qty - _DUST
        return PositionImpact(
            action=PositionAction.CLOSE if fully else PositionAction.REDUCE,
            quantity=closing,
            cash_delta=self.calculate_cash_delta(),
            realized_pnl=quantize_money(realized),
            fully_closed=fully,
            resulting_quantity=ZERO
            if fully
            else quantize_price(position.quantity + self.signed_quantity),
        )

    def apply_to_position(self, position: "Position") -> PositionImpact:
        """Apply this fill to ``position``, mutating it.

        Returns the realised impact. Cash is **not** touched — the caller owns
        the balance; use :meth:`backtest.simulator.portfolio.Portfolio.apply_fill`
        to update both together.

        Raises
        ------
        ValidationError
            If the symbols differ, the position is closed, or the fill would
            reverse the position through zero.
        """
        if position.symbol != self.symbol:
            raise ValidationError(
                "fill symbol does not match the position",
                code="symbol_mismatch",
                fill=self.symbol,
                position=position.symbol,
            )
        if not position.is_open:
            raise ValidationError(
                "cannot apply a fill to a closed position",
                code="position_closed",
                symbol=self.symbol,
            )

        preview = self.impact_on_position(position)
        if preview.action is PositionAction.REVERSE:
            raise ValidationError(
                "fill would reverse the position through zero; "
                "close it and open a new one instead",
                code="position_reversal",
                symbol=self.symbol,
                fill_quantity=str(self.quantity),
                open_quantity=str(abs(position.quantity)),
            )

        object.__setattr__(self, "position_id", position.position_id)

        if preview.action is PositionAction.INCREASE:
            cash = position.add_shares(self.quantity, self.fill_price, self.total_fees)
            return PositionImpact(
                action=PositionAction.INCREASE,
                quantity=self.quantity,
                cash_delta=quantize_money(cash),
                resulting_quantity=position.quantity,
            )

        result = position.reduce_shares(self.quantity, self.fill_price, self.total_fees)
        return PositionImpact(
            action=PositionAction.CLOSE if result.fully_closed else PositionAction.REDUCE,
            quantity=result.quantity_closed,
            cash_delta=result.cash_delta,
            realized_pnl=result.realized_pnl,
            fully_closed=result.fully_closed,
            resulting_quantity=position.quantity,
        )

    # -- construction helpers ---------------------------------------------

    @classmethod
    def from_order(
        cls,
        order: "Order",
        quantity: Any | None = None,
        fill_price: Any | None = None,
        commission_model: Any = None,
        reference_price: Any | None = None,
        exchange_fees: Any = ZERO,
        regulatory_fees: Any = ZERO,
        liquidity_flag: str | None = None,
        filled_at: datetime | None = None,
        apply_to_order: bool = True,
    ) -> "Fill":
        """Build a fill for ``order``, pricing commission from a model.

        Defaults ``quantity`` to the order's remaining size and ``fill_price``
        to its average fill price or limit — but an explicit price from the
        Step 9 executor is what you normally pass.

        Validates the fill against the order's remaining quantity **before**
        constructing anything, so an over-fill can never produce a half-applied
        state.

        Parameters
        ----------
        apply_to_order:
            When ``True`` (default) the fill is registered on the order via
            ``order.add_fill``, advancing it to PARTIAL or FILLED.

        Raises
        ------
        ValidationError
            If the order is not working, or the quantity exceeds what remains.
        """
        if not getattr(order, "is_working", False):
            raise ValidationError(
                "cannot fill an order that is not working",
                code="order_not_working",
                order_id=getattr(order, "order_id", None),
                status=str(getattr(order, "status", "?")),
            )

        qty = (
            to_price(quantity, "quantity")
            if quantity is not None
            else order.remaining_quantity
        )
        if qty <= ZERO:
            raise ValidationError(
                "fill quantity must be positive", code="invalid_quantity"
            )
        if qty > order.remaining_quantity + _DUST:
            raise ValidationError(
                "fill would exceed the order's remaining quantity",
                code="overfill",
                order_id=order.order_id,
                requested=str(qty),
                remaining=str(order.remaining_quantity),
            )
        qty = min(qty, order.remaining_quantity)

        px = (
            to_price(fill_price, "fill_price")
            if fill_price is not None
            else (order.average_fill_price or order.limit_price or order.stop_price)
        )
        if px is None:
            raise ValidationError(
                "a fill_price is required for this order type",
                code="missing_fill_price",
                order_id=order.order_id,
            )

        model: CommissionModel = resolve_commission_model(commission_model)
        commission = model.calculate(qty, px, order.side)

        fill = cls(
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            fill_price=px,
            order_id=order.order_id,
            position_id=getattr(order, "position_id", None),
            commission=commission,
            exchange_fees=exchange_fees,
            regulatory_fees=regulatory_fees,
            reference_price=reference_price,
            liquidity_flag=liquidity_flag,
            filled_at=filled_at or _utcnow(),
            strategy_name=getattr(order, "strategy_name", None),
        )

        if apply_to_order:
            order.add_fill(fill, at=fill.filled_at)
        return fill

    def with_position(self, position_id: str) -> "Fill":
        """Return a copy linked to ``position_id``.

        A copy rather than a mutation, because :class:`Fill` is frozen.
        """
        return replace(self, position_id=position_id)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot. Decimals become strings to keep precision."""
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "fill_price": str(self.fill_price),
            "commission": str(self.commission),
            "exchange_fees": str(self.exchange_fees),
            "regulatory_fees": str(self.regulatory_fees),
            "reference_price": (
                str(self.reference_price) if self.reference_price is not None else None
            ),
            "slippage_bps": str(self.slippage_bps),
            "slippage_amount": str(self.slippage_amount),
            "liquidity_flag": self.liquidity_flag,
            "filled_at": self.filled_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "strategy_name": self.strategy_name,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Fill":
        """Rebuild from :meth:`to_dict` output.

        ``slippage_bps`` and ``slippage_amount`` are recomputed from
        ``reference_price`` rather than trusted, so a hand-edited snapshot
        cannot smuggle in inconsistent attribution.
        """
        return cls(
            symbol=payload["symbol"],
            side=payload["side"],
            quantity=payload["quantity"],
            fill_price=payload["fill_price"],
            fill_id=payload.get("fill_id") or str(uuid.uuid4()),
            order_id=payload.get("order_id"),
            position_id=payload.get("position_id"),
            commission=payload.get("commission", ZERO),
            exchange_fees=payload.get("exchange_fees", ZERO),
            regulatory_fees=payload.get("regulatory_fees", ZERO),
            reference_price=payload.get("reference_price"),
            liquidity_flag=payload.get("liquidity_flag"),
            filled_at=(
                datetime.fromisoformat(payload["filled_at"])
                if payload.get("filled_at")
                else _utcnow()
            ),
            created_at=(
                datetime.fromisoformat(payload["created_at"])
                if payload.get("created_at")
                else _utcnow()
            ),
            strategy_name=payload.get("strategy_name"),
        )

    # -- persistence -------------------------------------------------------

    def save_to_db(self, db: "DatabaseManager") -> str:
        """Insert this fill, returning its ``fill_id``.

        Fills are append-only: an existing row is left untouched rather than
        updated, mirroring the immutability of the object. Re-saving the same
        fill is therefore a safe no-op, which matters when a retry replays a
        partially-completed batch.

        Raises
        ------
        ValidationError
            If ``order_id`` is not set — the schema requires it (NOT NULL).
        """
        from backtest.db.models import Fill as FillRow
        from backtest.db.models import Position as PositionRow

        if not self.order_id:
            raise ValidationError(
                "order_id is required to save a fill",
                code="missing_order_id",
                fill_id=self.fill_id,
            )

        with db.session() as session:
            if session.get(FillRow, self.fill_id) is not None:
                logger.debug("fill %s already persisted, skipping", self.fill_id)
                return self.fill_id

            # Fills reference positions. Saving one before its position row
            # exists yields a bare ForeignKeyViolation from the driver, which
            # is a miserable error to read; say what to do instead.
            if self.position_id is not None and session.get(PositionRow, self.position_id) is None:
                raise ValidationError(
                    "the position this fill references has not been saved yet. "
                    "Save in dependency order (portfolio -> positions -> orders "
                    "-> fills), or use Portfolio.save_to_db(), which does it "
                    "atomically",
                    code="position_not_persisted",
                    fill_id=self.fill_id,
                    position_id=self.position_id,
                )

            session.add(
                FillRow(
                    fill_id=self.fill_id,
                    order_id=self.order_id,
                    position_id=self.position_id,
                    symbol=self.symbol,
                    side=self.side.value,
                    quantity=self.quantity,
                    fill_price=self.fill_price,
                    commission=self.commission,
                    slippage_bps=self.slippage_bps,
                    slippage_amount=self.slippage_amount,
                    exchange_fees=self.exchange_fees,
                    regulatory_fees=self.regulatory_fees,
                    liquidity_flag=self.liquidity_flag,
                    reference_price=self.reference_price,
                    filled_at=self.filled_at,
                )
            )
            session.flush()

        logger.debug("fill %s saved", self.fill_id)
        return self.fill_id

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<Fill {self.side} {self.quantity} {self.symbol} @ {self.fill_price} "
            f"fees={self.total_fees}>"
        )
