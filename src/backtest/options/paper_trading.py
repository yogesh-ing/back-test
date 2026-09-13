"""Options paper trading — multi-leg order execution and position tracking.

Extends the equity ``PaperBroker`` with option-specific logic:

- Multi-leg atomic execution (all-or-nothing)
- OptionPosition tracking with structure_id grouping
- MTM (mark-to-market) calculation from live quotes
- Lot size validation
- Premium calculation

Usage::

    from backtest.options.paper_trading import OptionPaperBroker, OptionPosition

    broker = OptionPaperBroker(capital=1_000_000)
    fills = broker.execute_structure(trade_intent, quote_provider)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Protocol

from backtest.strategy.intent import (
    OptionLeg,
    TradeIntent,
)

logger = logging.getLogger("backtest.options.paper_trading")

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PositionStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


class OrderFillStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OptionPosition:
    """A single option position (one leg of a structure).

    Positions are grouped by ``structure_id`` to track multi-leg structures
    as a unit for P&L and closing purposes.
    """

    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    structure_id: str = ""
    strategy_name: str = ""

    # Instrument details
    instrument_token: str = ""
    trading_symbol: str = ""
    underlying: str = ""
    option_type: str = ""  # CE or PE
    strike: Decimal = ZERO
    expiry: date | None = None
    lot_size: int = 25

    # Position
    side: str = ""  # BUY or SELL
    quantity: int = 0  # number of lots
    entry_price: Decimal = ZERO  # premium per unit
    current_price: Decimal = ZERO  # latest MTM price
    status: PositionStatus = PositionStatus.OPEN

    # P&L
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    commission: Decimal = ZERO

    # Timestamps
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None
    last_updated: datetime = field(default_factory=datetime.utcnow)

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_quantity(self) -> int:
        """Total units (lots × lot_size)."""
        return self.quantity * self.lot_size

    @property
    def notional_value(self) -> Decimal:
        """Notional = entry_price × total_quantity."""
        return self.entry_price * Decimal(str(self.total_quantity))

    @property
    def is_long(self) -> bool:
        return self.side == "BUY"

    @property
    def is_short(self) -> bool:
        return self.side == "SELL"

    def calculate_unrealized_pnl(self) -> Decimal:
        """Calculate unrealized P&L based on current_price."""
        if self.status != PositionStatus.OPEN:
            return ZERO

        if self.is_long:
            # Long: profit when price goes up
            self.unrealized_pnl = (self.current_price - self.entry_price) * Decimal(
                str(self.total_quantity)
            )
        else:
            # Short: profit when price goes down
            self.unrealized_pnl = (self.entry_price - self.current_price) * Decimal(
                str(self.total_quantity)
            )
        return self.unrealized_pnl

    def close(self, exit_price: Decimal, timestamp: datetime | None = None) -> Decimal:
        """Close the position and calculate realized P&L.

        Returns the realized P&L (gross of commission).
        """
        if self.status != PositionStatus.OPEN:
            return ZERO

        self.current_price = exit_price
        if self.is_long:
            self.realized_pnl = (exit_price - self.entry_price) * Decimal(
                str(self.total_quantity)
            )
        else:
            self.realized_pnl = (self.entry_price - exit_price) * Decimal(
                str(self.total_quantity)
            )

        self.status = PositionStatus.CLOSED
        self.closed_at = timestamp or datetime.utcnow()
        self.last_updated = self.closed_at
        return self.realized_pnl

    def update_mtm(self, price: Decimal) -> Decimal:
        """Update current price and recalculate unrealized P&L."""
        self.current_price = price
        self.last_updated = datetime.utcnow()
        return self.calculate_unrealized_pnl()


@dataclass
class StructurePosition:
    """Groups related OptionPositions by structure_id for multi-leg tracking.

    A structure (e.g. BullCallSpread) has 2+ legs that are opened and
    closed together.  This tracks the aggregate P&L and status.
    """

    structure_id: str
    structure_type: str
    strategy_name: str
    underlying: str
    expiry: date
    legs: list[OptionPosition] = field(default_factory=list)
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return any(leg.status == PositionStatus.OPEN for leg in self.legs)

    @property
    def total_entry_cost(self) -> Decimal:
        """Net cost to open the structure (debit = positive, credit = negative)."""
        cost = ZERO
        for leg in self.legs:
            leg_cost = leg.entry_price * Decimal(str(leg.total_quantity))
            if leg.is_long:
                cost += leg_cost  # BUY = pay premium
            else:
                cost -= leg_cost  # SELL = receive premium
        return cost

    @property
    def total_unrealized_pnl(self) -> Decimal:
        return sum(leg.unrealized_pnl for leg in self.legs)

    @property
    def total_realized_pnl(self) -> Decimal:
        return sum(leg.realized_pnl for leg in self.legs)

    @property
    def total_commission(self) -> Decimal:
        return sum(leg.commission for leg in self.legs)


# ---------------------------------------------------------------------------
# Quote provider protocol
# ---------------------------------------------------------------------------

class QuoteProvider(Protocol):
    """Provides live option quotes for MTM calculation."""

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        """Return ``{"ltp": float, "bid": float, "ask": float, ...}``."""
        ...


class FakeQuoteProvider:
    """Returns a fixed price for all instruments (for testing)."""

    def __init__(self, default_price: float = 100.0) -> None:
        self.default_price = default_price
        self.prices: dict[str, float] = {}

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        price = self.prices.get(instrument_token, self.default_price)
        return {"ltp": price, "bid": price - 0.5, "ask": price + 0.5}

    def set_price(self, instrument_token: str, price: float) -> None:
        self.prices[instrument_token] = price


# ---------------------------------------------------------------------------
# OptionPaperBroker
# ---------------------------------------------------------------------------

class OptionPaperBroker:
    """Paper broker for option trading — handles multi-leg structures.

    Unlike the equity ``PaperBroker`` which handles single orders,
    this broker executes complete ``TradeIntent`` structures atomically:
    either all legs fill or none do.

    Parameters
    ----------
    capital:
        Starting capital in ₹.
    slippage_pct:
        Slippage as a percentage (e.g. 0.001 = 0.1%).
    commission_per_lot:
        Commission per lot in ₹ (flat fee).
    fee_calculator:
        Optional :class:`~backtest.simulator.fees.CommissionCalculator`.
        When supplied, the complete statutory stack (STT, exchange txn,
        SEBI, stamp duty, GST) is computed per leg via
        ``calculate_structure()`` on every execution, alongside the legacy
        ``commission_per_lot`` charge. Costs never block execution: a fee
        calculation error is logged and the trade proceeds.
    """

    def __init__(
        self,
        capital: float = 1_000_000.0,
        slippage_pct: float = 0.001,
        commission_per_lot: float = 20.0,
        fee_calculator: Any | None = None,
        on_structure_opened: Any | None = None,
        on_structure_closed: Any | None = None,
    ) -> None:
        self.capital = Decimal(str(capital))
        self.available_cash = Decimal(str(capital))
        self.slippage_pct = Decimal(str(slippage_pct))
        self.commission_per_lot = Decimal(str(commission_per_lot))
        self.fee_calculator = fee_calculator
        self._statutory_fees_paid = ZERO

        # Gap G4.2 observers — fired after every state transition so the
        # persistence layer (or tests) can mirror the book. Exceptions in an
        # observer never block execution.
        #: ``fn(structure, entry_fees: Decimal)`` after a successful open.
        self.on_structure_opened = on_structure_opened
        #: ``fn(structure, realized_pnl: Decimal)`` after close/settlement.
        self.on_structure_closed = on_structure_closed

        # Position tracking
        self._positions: dict[str, OptionPosition] = {}  # position_id -> position
        self._structures: dict[str, StructurePosition] = {}  # structure_id -> structure
        self._order_history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def execute_structure(
        self,
        intent: TradeIntent,
        quote_provider: QuoteProvider,
        timestamp: datetime | None = None,
    ) -> list[OptionPosition]:
        """Execute a complete option structure atomically.

        All legs fill together or the entire structure is rejected.

        Parameters
        ----------
        intent:
            The trade intent from the expression layer.
        quote_provider:
            Provides live quotes for fill prices.
        timestamp:
            Trade timestamp (defaults to now).

        Returns
        -------
        List of ``OptionPosition`` objects created.

        Raises
        ------
        InsufficientMarginError
            If available cash is insufficient for the net debit.
        LotSizeValidationError
            If any leg has invalid lot size.
        """
        ts = timestamp or datetime.utcnow()

        # Validate lot sizes
        for leg in intent.legs:
            self._validate_lot_size(leg)

        # Get fill prices for all legs
        fills = []
        net_cost = ZERO
        for leg in intent.legs:
            quote = quote_provider.get_quote(leg.instrument_token)
            fill_price = self._apply_slippage(
                Decimal(str(quote.get("ltp", 0))), leg.side
            )
            leg_cost = fill_price * Decimal(str(leg.total_quantity))
            commission = self.commission_per_lot * Decimal(str(leg.quantity))

            if leg.side == "BUY":
                net_cost += leg_cost + commission
            else:
                net_cost -= leg_cost - commission

            fills.append((leg, fill_price, commission))

        # Full statutory fee stack (Phase 8) — computed per leg when a fee
        # calculator is attached. Cash-bearing (Gap G4.1): the total is
        # debited from ``available_cash`` the same way a real broker's
        # contract note hits the ledger, and is excluded from equity via the
        # ``total_statutory_fees_paid`` term in :attr:`total_equity`. Fees
        # never block execution: a calculation error is logged and the trade
        # proceeds without statutory charges.
        statutory_fees = ZERO
        if self.fee_calculator is not None:
            try:
                fee_breakdown = self.fee_calculator.calculate_structure(
                    [
                        {
                            "side": leg.side,
                            "quantity": leg.total_quantity,
                            "price": fill_price,
                        }
                        for leg, fill_price, _ in fills
                    ],
                    when=ts,
                )
                statutory_fees = Decimal(str(fee_breakdown.total))
                self._statutory_fees_paid += statutory_fees
            except Exception:
                logger.exception(
                    "[option-paper] fee calculation failed for %s — "
                    "executing without statutory fees",
                    intent.structure_type,
                )

        # Check margin — net premium + commissions + the statutory stack.
        if net_cost + statutory_fees > self.available_cash:
            raise InsufficientMarginError(
                f"Need ₹{net_cost + statutory_fees:,.2f} "
                f"but only ₹{self.available_cash:,.2f} available"
            )

        # Atomic execution — create all positions
        structure_id = str(uuid.uuid4())
        positions = []

        # Per-leg strike lookup: metadata may carry a single "strike" (single-leg)
        # or a "strikes" dict keyed by trading_symbol (multi-leg spreads).
        strikes_map = intent.metadata.get("strikes", {})
        default_strike = intent.metadata.get("strike", "0")

        for leg, fill_price, commission in fills:
            position = OptionPosition(
                structure_id=structure_id,
                strategy_name=intent.strategy_name,
                instrument_token=leg.instrument_token,
                trading_symbol=leg.trading_symbol,
                underlying=intent.view.underlying,
                option_type=intent.metadata.get("option_type", ""),
                strike=Decimal(str(strikes_map.get(leg.trading_symbol, default_strike))),
                expiry=intent.expiry,
                lot_size=leg.lot_size,
                side=leg.side,
                quantity=leg.quantity,
                entry_price=fill_price,
                current_price=fill_price,
                commission=commission,
                opened_at=ts,
                metadata={
                    "structure_type": intent.structure_type,
                    "fill_price": str(fill_price),
                },
            )
            self._positions[position.position_id] = position
            positions.append(position)

        # Create structure
        structure = StructurePosition(
            structure_id=structure_id,
            structure_type=intent.structure_type,
            strategy_name=intent.strategy_name,
            underlying=intent.view.underlying,
            expiry=intent.expiry,
            legs=positions,
            opened_at=ts,
        )
        self._structures[structure_id] = structure

        # Update cash — full cost including the statutory stack.
        self.available_cash -= net_cost + statutory_fees

        # Log
        self._log_fill(intent, structure_id, fills, ts)

        self._fire_structure_opened(structure, statutory_fees)

        logger.info(
            "[option-paper] Executed %s structure=%s legs=%d cost=₹%.2f cash=₹%.2f",
            intent.structure_type,
            structure_id[:8],
            len(fills),
            float(net_cost),
            float(self.available_cash),
        )

        return positions

    def close_structure(
        self,
        structure_id: str,
        quote_provider: QuoteProvider,
        timestamp: datetime | None = None,
    ) -> Decimal:
        """Close all legs of a structure atomically.

        Returns the total realized P&L (gross of commission).
        """
        structure = self._structures.get(structure_id)
        if structure is None:
            raise ValueError(f"Structure {structure_id} not found")

        ts = timestamp or datetime.utcnow()
        total_pnl = ZERO

        open_legs = [
            position for position in structure.legs
            if position.status == PositionStatus.OPEN
        ]
        exit_fees = ZERO
        closing_fills: list[dict[str, Any]] = []

        for position in open_legs:
            quote = quote_provider.get_quote(position.instrument_token)
            exit_price = Decimal(str(quote.get("ltp", 0)))
            exit_price = self._apply_slippage(exit_price, "SELL" if position.is_long else "BUY")

            pnl = position.close(exit_price, ts)
            total_pnl += pnl

            # Credit back to cash
            if position.is_long:
                # Selling long position → receive cash
                self.available_cash += exit_price * Decimal(str(position.total_quantity))
            else:
                # Buying back short position → pay cash
                self.available_cash -= exit_price * Decimal(str(position.total_quantity))

            closing_fills.append(
                {
                    "side": "SELL" if position.is_long else "BUY",
                    "quantity": position.total_quantity,
                    "price": exit_price,
                }
            )

        # Exit-side statutory stack (Gap G4.1): a real contract note charges
        # brokerage and sell-side STT on closing orders too. Same policy as
        # the entry side — computed when a calculator is attached, cash
        # debited, never blocking the close.
        if self.fee_calculator is not None and closing_fills:
            try:
                fee_breakdown = self.fee_calculator.calculate_structure(
                    closing_fills,
                    when=ts,
                )
                exit_fees = Decimal(str(fee_breakdown.total))
                self._statutory_fees_paid += exit_fees
                self.available_cash -= exit_fees
            except Exception:
                logger.exception(
                    "[option-paper] exit fee calculation failed for %s — "
                    "closing without statutory fees",
                    structure_id[:8],
                )

        structure.closed_at = ts

        self._fire_structure_closed(structure, total_pnl)

        logger.info(
            "[option-paper] Closed structure=%s pnl=₹%.2f fees=₹%.2f cash=₹%.2f",
            structure_id[:8],
            float(total_pnl),
            float(exit_fees),
            float(self.available_cash),
        )

        return total_pnl

    def update_mtm(
        self,
        quote_provider: QuoteProvider,
        timestamp: datetime | None = None,
    ) -> Decimal:
        """Update MTM for all open positions.

        Returns total unrealized P&L across all open positions.

        ``timestamp`` is accepted for interface symmetry with
        :meth:`execute_structure` / :meth:`close_structure`; MTM updates
        always stamp positions with wall-clock time.
        """
        total_unrealized = ZERO

        for position in self._positions.values():
            if position.status != PositionStatus.OPEN:
                continue

            quote = quote_provider.get_quote(position.instrument_token)
            price = Decimal(str(quote.get("ltp", 0)))
            pnl = position.update_mtm(price)
            total_unrealized += pnl

        return total_unrealized

    # ------------------------------------------------------------------
    # Persistence support (Gap G4.2)
    # ------------------------------------------------------------------

    def _fire_structure_opened(self, structure: StructurePosition, entry_fees: Decimal) -> None:
        if self.on_structure_opened is None:
            return
        try:
            self.on_structure_opened(structure, entry_fees)
        except Exception:  # noqa: BLE001 — mirroring must never block trading
            logger.exception(
                "[option-paper] on_structure_opened observer failed for %s",
                structure.structure_id[:8],
            )

    def _fire_structure_closed(self, structure: StructurePosition, realized_pnl: Decimal) -> None:
        if self.on_structure_closed is None:
            return
        try:
            self.on_structure_closed(structure, realized_pnl)
        except Exception:  # noqa: BLE001 — mirroring must never block trading
            logger.exception(
                "[option-paper] on_structure_closed observer failed for %s",
                structure.structure_id[:8],
            )

    def _notify_settlement(self, structure_id: str) -> None:
        """Post-expiry hook (called by the expiry pipeline).

        ``settle_expired`` closes positions leg-by-leg; once no leg of a
        structure remains open, fire the closed observer so persistence can
        stamp the row ``expired``.
        """
        structure = self._structures.get(structure_id)
        if structure is None or structure.is_open:
            return
        if structure.closed_at is None:
            structure.closed_at = datetime.utcnow()
        self._fire_structure_closed(structure, structure.total_realized_pnl)

    def restore_structure(self, structure: StructurePosition, fees_paid: Decimal = ZERO) -> None:
        """Rehydrate a persisted open structure after a restart (G4.2).

        Registers the structure and its legs, then re-applies the opening
        cash debit (net premium + commissions + the entry-side statutory
        stack) so ``available_cash`` matches the pre-restart book.
        """
        self._structures[structure.structure_id] = structure
        for leg in structure.legs:
            self._positions[leg.position_id] = leg
        debit = structure.total_entry_cost + structure.total_commission + Decimal(str(fees_paid))
        self.available_cash -= debit
        logger.info(
            "[option-paper] restored structure=%s legs=%d debit=₹%.2f",
            structure.structure_id[:8],
            len(structure.legs),
            float(debit),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[OptionPosition]:
        """Return all open option positions."""
        return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    def get_positions_by_structure(self, structure_id: str) -> list[OptionPosition]:
        """Return all positions for a given structure."""
        return [
            p for p in self._positions.values()
            if p.structure_id == structure_id
        ]

    def get_open_structures(self) -> list[StructurePosition]:
        """Return all structures that have at least one open leg."""
        return [s for s in self._structures.values() if s.is_open]

    def get_structure(self, structure_id: str) -> StructurePosition | None:
        return self._structures.get(structure_id)

    @property
    def total_commission_paid(self) -> Decimal:
        """Every rupee of brokerage charged since the book opened.

        Commissions leave ``available_cash`` the moment a structure fills,
        so they must be subtracted from equity too — otherwise a paying
        broker's book would look richer than the cash it actually holds.
        """
        return sum(p.commission for p in self._positions.values())

    @property
    def total_statutory_fees_paid(self) -> Decimal:
        """Every statutory rupee (STT, exchange txn, SEBI, stamp, GST) charged.

        Cash-bearing since Gap G4.1: these debits leave ``available_cash``
        at execution, so equity must net them out too — otherwise a book
        with real contract-note costs would overstate its own equity.
        """
        return self._statutory_fees_paid

    @property
    def total_costs_paid(self) -> Decimal:
        """Brokerage + statutory stack — everything trading has cost so far."""
        return self.total_commission_paid + self._statutory_fees_paid

    @property
    def total_equity(self) -> Decimal:
        """Capital + realized + unrealized P&L − commissions − statutory fees.

        NOT ``available_cash + unrealized``: cash is debited the full
        premium at open while unrealized is measured from the entry price,
        so summing those two would make equity drop by the premium the
        moment a long option is bought. Anchoring on starting capital keeps
        equity flat across an at-market open and moving only with P&L —
        matching how the equity P&L reconciliation test (and a real
        broker's funds view) behaves.
        """
        unrealized = sum(
            p.unrealized_pnl for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        )
        return (
            self.capital
            + self.total_realized_pnl
            + unrealized
            - self.total_commission_paid
            - self._statutory_fees_paid
        )

    @property
    def total_realized_pnl(self) -> Decimal:
        """Realized P&L across closed AND expired positions.

        Expiry settlement marks positions ``EXPIRED`` (not ``CLOSED``), so
        filtering on ``CLOSED`` alone would drop settled P&L from the
        summary and break the equity reconciliation.
        """
        return sum(
            p.realized_pnl for p in self._positions.values()
            if p.status in (PositionStatus.CLOSED, PositionStatus.EXPIRED)
        )

    @property
    def total_margin_used(self) -> Decimal:
        """Approximate margin used (sum of sell-side notional values)."""
        margin = ZERO
        for pos in self._positions.values():
            if pos.status == PositionStatus.OPEN and pos.is_short:
                margin += pos.entry_price * Decimal(str(pos.total_quantity))
        return margin

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_lot_size(self, leg: OptionLeg) -> None:
        """Validate that lot size is positive."""
        if leg.lot_size < 1:
            raise LotSizeValidationError(
                f"Invalid lot_size {leg.lot_size} for {leg.trading_symbol}"
            )
        if leg.quantity < 1:
            raise LotSizeValidationError(
                f"Invalid quantity {leg.quantity} for {leg.trading_symbol}"
            )

    def _apply_slippage(self, price: Decimal, side: str) -> Decimal:
        """Apply slippage to fill price."""
        if side == "BUY":
            return (price * (ONE + self.slippage_pct)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            return (price * (ONE - self.slippage_pct)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

    def _log_fill(
        self,
        intent: TradeIntent,
        structure_id: str,
        fills: list[tuple],
        ts: datetime,
    ) -> None:
        """Log fill details for audit trail."""
        record = {
            "timestamp": ts.isoformat(),
            "structure_id": structure_id,
            "structure_type": intent.structure_type,
            "strategy": intent.strategy_name,
            "underlying": intent.view.underlying,
            "expiry": str(intent.expiry),
            "legs": [
                {
                    "symbol": leg.trading_symbol,
                    "side": leg.side,
                    "quantity": leg.quantity,
                    "fill_price": str(fill_price),
                    "commission": str(commission),
                }
                for leg, fill_price, commission in fills
            ],
            "cash_after": str(self.available_cash),
            "statutory_fees": str(self._statutory_fees_paid),
        }
        self._order_history.append(record)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InsufficientMarginError(Exception):
    """Raised when available cash is insufficient for a trade."""
    pass


class LotSizeValidationError(Exception):
    """Raised when lot size or quantity is invalid."""
    pass
