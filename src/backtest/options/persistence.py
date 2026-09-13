"""Option structure persistence (Gap-Analysis G4.2).

Before this module the options paper book was process memory only — a
server restart lost every open structure. This is the durable layer:

* :meth:`StructurePersistence.save_open` — upsert the row when a structure
  executes (status ``open``, legs snapshot, entry cost + entry-side fees).
* :meth:`StructurePersistence.mark_closed` — stamp ``closed_at`` / status /
  realized P&L when the structure is squared off or settles at expiry.
* :meth:`StructurePersistence.load_open` — rehydrate every open structure
  so :meth:`OptionPaperBroker.restore_structure` can rebuild the book on
  startup, debiting cash exactly as the original execution did.

All money round-trips through ``Decimal`` strings in the JSON snapshot so
reconciliation never sees float drift. Naive UTC datetimes from the paper
broker are stamped timezone-aware on the way in (the ORM contract requires
tz-aware UTC).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backtest.db.models import Base, TradeStructure, TradeStructureStatus
from backtest.options.paper_trading import (
    OptionPosition,
    PositionStatus,
    StructurePosition,
)

if TYPE_CHECKING:
    from backtest.db.manager import DatabaseManager

logger = logging.getLogger("backtest.options.persistence")

ZERO = Decimal("0")


def _aware(ts: datetime | None) -> datetime | None:
    """Stamp naive datetimes as UTC (the ORM contract requires tz-aware)."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _leg_to_dict(leg: OptionPosition) -> dict[str, Any]:
    return {
        "position_id": leg.position_id,
        "instrument_token": leg.instrument_token,
        "trading_symbol": leg.trading_symbol,
        "option_type": leg.option_type,
        "strike": str(leg.strike),
        "side": leg.side,
        "quantity": leg.quantity,
        "lot_size": leg.lot_size,
        "entry_price": str(leg.entry_price),
        "current_price": str(leg.current_price),
        "commission": str(leg.commission),
        "opened_at": leg.opened_at.isoformat() if leg.opened_at else None,
    }


def _dict_to_leg(structure_id: str, strategy_name: str, underlying: str,
                 expiry: date | None, raw: dict[str, Any]) -> OptionPosition:
    opened_raw = raw.get("opened_at")
    opened_at = (
        datetime.fromisoformat(opened_raw) if opened_raw else datetime.utcnow()
    )
    return OptionPosition(
        position_id=str(raw["position_id"]),
        structure_id=structure_id,
        strategy_name=strategy_name,
        instrument_token=str(raw.get("instrument_token", "")),
        trading_symbol=str(raw.get("trading_symbol", "")),
        underlying=underlying,
        option_type=str(raw.get("option_type", "")),
        strike=Decimal(str(raw.get("strike", "0"))),
        expiry=expiry,
        lot_size=int(raw.get("lot_size", 25)),
        side=str(raw.get("side", "BUY")),
        quantity=int(raw.get("quantity", 0)),
        entry_price=Decimal(str(raw.get("entry_price", "0"))),
        current_price=Decimal(str(raw.get("current_price", raw.get("entry_price", "0")))),
        commission=Decimal(str(raw.get("commission", "0"))),
        status=PositionStatus.OPEN,
        opened_at=opened_at,
    )


class StructurePersistence:
    """Durable record keeper for option structures over a DatabaseManager."""

    def __init__(self, manager: "DatabaseManager") -> None:
        self.manager = manager

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #

    def ensure_schema(self) -> None:
        """Create ``trade_structures`` if absent (idempotent, table-scoped).

        Production databases get the table from migration 004; this keeps
        fresh SQLite dev/test databases working without a migration run.
        """
        Base.metadata.create_all(
            self.manager.engine, tables=[TradeStructure.__table__]
        )

    # ------------------------------------------------------------------ #
    # Write path
    # ------------------------------------------------------------------ #

    def save_open(self, structure: StructurePosition, entry_fees: Decimal = ZERO) -> None:
        """Persist a freshly executed structure (upsert)."""
        with self.manager.session() as session:
            row = session.get(TradeStructure, structure.structure_id)
            if row is None:
                row = TradeStructure(structure_id=structure.structure_id)
                session.add(row)
            row.strategy_name = structure.strategy_name
            row.structure_type = structure.structure_type
            row.underlying = structure.underlying
            row.expiry = structure.expiry
            row.status = TradeStructureStatus.OPEN.value
            row.opened_at = _aware(structure.opened_at) or datetime.now(timezone.utc)
            row.closed_at = None
            row.entry_cost = structure.total_entry_cost
            row.fees_paid = Decimal(str(entry_fees))
            row.realized_pnl = ZERO
            row.legs = {
                "items": [_leg_to_dict(leg) for leg in structure.legs],
            }
        logger.info(
            "[options-persist] saved open structure %s (%s on %s)",
            structure.structure_id[:8],
            structure.structure_type,
            structure.underlying,
        )

    def mark_closed(
        self,
        structure_id: str,
        realized_pnl: Decimal,
        closed_at: datetime | None = None,
        status: str = "closed",
    ) -> None:
        """Stamp a structure closed/expired with its gross realized P&L."""
        if status not in (
            TradeStructureStatus.CLOSED.value,
            TradeStructureStatus.EXPIRED.value,
        ):
            status = TradeStructureStatus.CLOSED.value
        with self.manager.session() as session:
            row = session.get(TradeStructure, structure_id)
            if row is None:
                logger.warning(
                    "[options-persist] close for unknown structure %s — skipped",
                    structure_id[:8],
                )
                return
            row.status = status
            row.closed_at = _aware(closed_at) or datetime.now(timezone.utc)
            row.realized_pnl = Decimal(str(realized_pnl))
        logger.info(
            "[options-persist] structure %s marked %s (pnl=₹%s)",
            structure_id[:8],
            status,
            realized_pnl,
        )

    # ------------------------------------------------------------------ #
    # Read path
    # ------------------------------------------------------------------ #

    def load_open(self) -> list[tuple[StructurePosition, Decimal]]:
        """Rehydrate every open structure (oldest first).

        Returns ``(structure, entry_fees)`` pairs — the fee figure is the
        entry-side statutory stack persisted at open time, needed to debit
        cash exactly on restore.
        """
        with self.manager.session() as session:
            rows = (
                session.query(TradeStructure)
                .filter(TradeStructure.status == TradeStructureStatus.OPEN.value)
                .order_by(TradeStructure.opened_at.asc())
                .all()
            )
            return [
                (self._to_structure(row), Decimal(str(row.fees_paid or 0)))
                for row in rows
            ]

    @staticmethod
    def _to_structure(row: TradeStructure) -> StructurePosition:
        opened_at = row.opened_at
        if opened_at is not None and opened_at.tzinfo is not None:
            opened_at = opened_at.replace(tzinfo=None)  # broker uses naive UTC
        structure = StructurePosition(
            structure_id=row.structure_id,
            structure_type=row.structure_type,
            strategy_name=row.strategy_name,
            underlying=row.underlying,
            expiry=row.expiry,
            opened_at=opened_at or datetime.utcnow(),
        )
        items = (row.legs or {}).get("items", [])
        structure.legs = [
            _dict_to_leg(
                row.structure_id,
                row.strategy_name,
                row.underlying,
                row.expiry,
                raw,
            )
            for raw in items
        ]
        return structure

    def get_row(self, structure_id: str) -> TradeStructure | None:
        """Fetch the raw row (mostly for tests and the audit trail)."""
        with self.manager.session() as session:
            row = session.get(TradeStructure, structure_id)
            if row is not None:
                # Detach so the caller can read attributes after close.
                session.expunge(row)
            return row
