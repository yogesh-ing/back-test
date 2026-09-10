"""Live option trading via mStock — multi-leg order orchestration.

Extends the mStock broker with option-specific live trading logic:

- Multi-leg order submission (sequential with rollback)
- Auto-cancel if a leg fails
- Order status polling with timeout
- Fill reconciliation
- Retry with exponential backoff
- Dry-run mode (log but don't execute)

Usage::

    from backtest.options.live_trading import LiveOptionTrader

    trader = LiveOptionTrader(broker=mstock_broker, dry_run=True)
    fills = trader.execute_structure(trade_intent)

V1 scope: sequential leg submission (not atomic at exchange level).
The exchange doesn't support multi-leg orders, so we submit each leg
and monitor for failures.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.mstock import MStockBroker, MStockOrderError
from backtest.strategy.intent import TradeIntent, OptionLeg

logger = logging.getLogger("backtest.options.live_trading")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class LegStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StructureExecStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    FILLED = "filled"
    PARTIAL_FILL = "partial_fill"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LegResult:
    """Result of submitting one leg of a multi-leg structure."""

    leg: OptionLeg
    status: LegStatus = LegStatus.PENDING
    broker_order_id: str = ""
    fill_price: float = 0.0
    fill_quantity: int = 0
    error: str = ""
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    retries: int = 0


@dataclass
class StructureResult:
    """Result of executing a complete multi-leg structure."""

    structure_id: str
    structure_type: str
    status: StructureExecStatus = StructureExecStatus.PENDING
    legs: list[LegResult] = field(default_factory=list)
    error: str = ""
    submitted_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def all_filled(self) -> bool:
        return all(leg.status == LegStatus.FILLED for leg in self.legs)

    @property
    def any_rejected(self) -> bool:
        return any(
            leg.status in (LegStatus.REJECTED, LegStatus.FAILED)
            for leg in self.legs
        )

    @property
    def fill_count(self) -> int:
        return sum(1 for leg in self.legs if leg.status == LegStatus.FILLED)


# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    """Retry policy for order submission."""

    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    backoff_factor: float = 2.0

    def delay_for_attempt(self, attempt: int) -> float:
        """Calculate delay for the given attempt (0-indexed)."""
        delay = self.base_delay_seconds * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay_seconds)


# ---------------------------------------------------------------------------
# Live option trader
# ---------------------------------------------------------------------------

class LiveOptionTrader:
    """Orchestrates multi-leg option orders via mStock.

    Parameters
    ----------
    broker:
        An authenticated ``MStockBroker`` instance.
    dry_run:
        If ``True``, log what would be sent but don't actually place orders.
    retry_config:
        Retry policy for failed order submissions.
    poll_interval_seconds:
        How often to poll for order status updates.
    poll_timeout_seconds:
        Max time to wait for all legs to fill before giving up.
    """

    def __init__(
        self,
        broker: MStockBroker,
        dry_run: bool = False,
        retry_config: RetryConfig | None = None,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 60.0,
    ) -> None:
        self.broker = broker
        self.dry_run = dry_run
        self.retry = retry_config or RetryConfig()
        self.poll_interval = poll_interval_seconds
        self.poll_timeout = poll_timeout_seconds

        # Tracking
        self._results: dict[str, StructureResult] = {}

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def execute_structure(
        self,
        intent: TradeIntent,
        timestamp: datetime | None = None,
    ) -> StructureResult:
        """Execute a multi-leg option structure.

        Submits legs sequentially.  If any leg fails after retries,
        cancels all previously filled legs (rollback).

        Parameters
        ----------
        intent:
            The trade intent from the expression layer.
        timestamp:
            Trade timestamp (defaults to now).

        Returns
        -------
        ``StructureResult`` with per-leg outcomes.
        """
        ts = timestamp or datetime.utcnow()
        structure_id = str(uuid.uuid4())

        result = StructureResult(
            structure_id=structure_id,
            structure_type=intent.structure_type,
            status=StructureExecStatus.IN_PROGRESS,
            submitted_at=ts,
        )

        logger.info(
            "[live-options] Executing %s structure=%s legs=%d dry_run=%s",
            intent.structure_type,
            structure_id[:8],
            len(intent.legs),
            self.dry_run,
        )

        # Submit each leg sequentially
        filled_legs: list[LegResult] = []

        for i, leg in enumerate(intent.legs):
            leg_result = self._submit_leg_with_retry(leg, i, structure_id)
            result.legs.append(leg_result)

            if leg_result.status == LegStatus.FILLED:
                filled_legs.append(leg_result)
                logger.info(
                    "[live-options] Leg %d/%d filled: %s %s x%d @ %.2f",
                    i + 1, len(intent.legs),
                    leg.side, leg.trading_symbol,
                    leg.quantity, leg_result.fill_price,
                )
            else:
                # Leg failed — rollback previously filled legs
                logger.warning(
                    "[live-options] Leg %d/%d failed: %s — rolling back %d filled legs",
                    i + 1, len(intent.legs),
                    leg_result.error,
                    len(filled_legs),
                )
                self._rollback(filled_legs, intent)
                result.status = StructureExecStatus.ROLLED_BACK
                result.error = f"Leg {i+1} failed: {leg_result.error}"
                result.completed_at = datetime.utcnow()
                self._results[structure_id] = result
                return result

        # All legs filled
        result.status = StructureExecStatus.FILLED
        result.completed_at = datetime.utcnow()
        self._results[structure_id] = result

        logger.info(
            "[live-options] Structure %s fully filled (%d legs)",
            structure_id[:8], len(filled_legs),
        )

        return result

    # ------------------------------------------------------------------
    # Leg submission with retry
    # ------------------------------------------------------------------

    def _submit_leg_with_retry(
        self,
        leg: OptionLeg,
        leg_index: int,
        structure_id: str,
    ) -> LegResult:
        """Submit a single leg with retry and backoff."""
        result = LegResult(leg=leg, submitted_at=datetime.utcnow())

        for attempt in range(self.retry.max_retries + 1):
            try:
                if self.dry_run:
                    result.status = LegStatus.FILLED
                    result.fill_price = 0.0
                    result.fill_quantity = leg.total_quantity
                    result.broker_order_id = f"DRY-{uuid.uuid4().hex[:8]}"
                    logger.info(
                        "[live-options] DRY RUN: would submit %s %s x%d",
                        leg.side, leg.trading_symbol, leg.quantity,
                    )
                    return result

                # Build broker order
                broker_order = self._build_broker_order(leg, structure_id, leg_index)

                # Submit
                order_id = self.broker.place_order(broker_order)
                result.broker_order_id = str(order_id)
                result.status = LegStatus.SUBMITTED
                result.retries = attempt

                # Poll for fill
                fill = self._poll_for_fill(order_id, leg)
                if fill is not None:
                    result.status = LegStatus.FILLED
                    result.fill_price = fill.get("fill_price", 0.0)
                    result.fill_quantity = fill.get("fill_quantity", 0)
                    result.filled_at = datetime.utcnow()
                    return result
                else:
                    result.status = LegStatus.REJECTED
                    result.error = "Order not filled within timeout"
                    return result

            except MStockOrderError as e:
                result.error = str(e)
                result.retries = attempt
                logger.warning(
                    "[live-options] Leg %d attempt %d failed: %s",
                    leg_index + 1, attempt + 1, e,
                )
                if attempt < self.retry.max_retries:
                    delay = self.retry.delay_for_attempt(attempt)
                    logger.info(
                        "[live-options] Retrying in %.1fs...", delay
                    )
                    time.sleep(delay)

            except Exception as e:
                result.error = f"Unexpected error: {e}"
                result.status = LegStatus.FAILED
                logger.error(
                    "[live-options] Leg %d unexpected error: %s",
                    leg_index + 1, e,
                )
                return result

        # Exhausted retries
        result.status = LegStatus.FAILED
        return result

    # ------------------------------------------------------------------
    # Order building
    # ------------------------------------------------------------------

    def _build_broker_order(
        self,
        leg: OptionLeg,
        structure_id: str,
        leg_index: int,
    ) -> BrokerOrder:
        """Build a BrokerOrder from an OptionLeg."""
        return BrokerOrder(
            client_order_id=f"OPT-{structure_id[:8]}-{leg_index}",
            symbol=leg.trading_symbol,
            side=leg.side,
            quantity=leg.total_quantity,
            order_type="MARKET",
            exchange="NFO",
            product="INTRADAY",
            tag={
                "structure_id": structure_id,
                "leg_index": leg_index,
                "instrument_token": leg.instrument_token,
            },
        )

    # ------------------------------------------------------------------
    # Order status polling
    # ------------------------------------------------------------------

    def _poll_for_fill(
        self,
        order_id: BrokerOrderId,
        leg: OptionLeg,
    ) -> dict[str, Any] | None:
        """Poll order status until filled, rejected, or timeout."""
        start = time.time()
        order_id_str = str(order_id)

        while time.time() - start < self.poll_timeout:
            try:
                order_book = self.broker.get_order_book()
                for order in order_book:
                    if order.broker_order_id == order_id_str:
                        status = (order.status or "").upper()
                        if status in ("EXECUTED", "FILLED", "COMPLETE"):
                            return {
                                "fill_price": order.average_fill_price or 0.0,
                                "fill_quantity": int(order.filled_qty or 0),
                                "status": status,
                            }
                        elif status in ("REJECTED", "CANCELLED", "EXPIRED"):
                            return None
                        # else: PENDING, OPEN, PARTIAL — keep polling

            except MStockOrderError as e:
                logger.warning(
                    "[live-options] Order book poll failed: %s", e
                )

            time.sleep(self.poll_interval)

        return None

    # ------------------------------------------------------------------
    # Rollback — cancel filled legs when a subsequent leg fails
    # ------------------------------------------------------------------

    def _rollback(
        self,
        filled_legs: list[LegResult],
        intent: TradeIntent,
    ) -> None:
        """Cancel all previously filled legs (best-effort).

        For BUY legs, we place a SELL to flatten.
        For SELL legs, we place a BUY to flatten.
        """
        for leg_result in filled_legs:
            try:
                if self.dry_run:
                    logger.info(
                        "[live-options] DRY RUN: would cancel/flatten %s %s",
                        leg_result.leg.side, leg_result.leg.trading_symbol,
                    )
                    continue

                # Flatten: opposite side of the filled leg
                flatten_side = "SELL" if leg_result.leg.side == "BUY" else "BUY"
                flatten_order = BrokerOrder(
                    client_order_id=f"FLAT-{leg_result.broker_order_id}",
                    symbol=leg_result.leg.trading_symbol,
                    side=flatten_side,
                    quantity=leg_result.fill_quantity or leg_result.leg.total_quantity,
                    order_type="MARKET",
                    exchange="NFO",
                    product="INTRADAY",
                    tag={"rollback": True},
                )
                order_id = self.broker.place_order(flatten_order)
                logger.info(
                    "[live-options] Rollback order placed: %s %s → %s",
                    flatten_side, leg_result.leg.trading_symbol,
                    order_id.order_id,
                )
                leg_result.status = LegStatus.CANCELLED

            except Exception as e:
                logger.error(
                    "[live-options] Rollback failed for %s: %s",
                    leg_result.leg.trading_symbol, e,
                )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_result(self, structure_id: str) -> StructureResult | None:
        return self._results.get(structure_id)

    def get_all_results(self) -> list[StructureResult]:
        return list(self._results.values())
