"""Live option trading via mStock — multi-leg order orchestration.

Extends the mStock broker with option-specific live trading logic:

- Multi-leg order submission (sequential with rollback)
- Auto-cancel if a leg fails
- Order status polling with timeout
- Fill reconciliation
- Append-only intent journal with idempotency keys (see
  :mod:`backtest.options.intent_journal`)
- Dry-run mode (log but don't execute)

Never retry an uncertain submit
-------------------------------

A leg is placed **at most once** per decision. The journal key is hashed from
the decision itself, so a replay of the same decision (restart, re-fed bar)
finds the ticket that is already at the broker and reconciles it instead of
placing a second one. A call that ends without a usable answer — connection
loss, gateway 5xx, a 2xx with no order id — is an :class:`OrderOutcomeUnknown`:
it is recorded as ``uncertain``, reconciled once against the broker order book,
and the structure **halts**. It is never re-sent and never rolled back, because
a flatten on top of an order whose existence is unproven can double a position.

A *definite* broker rejection (a 4xx or an error payload on a 200) is the only
outcome that rolls back previously filled legs — there mStock has told us
nothing exists at the exchange.

Usage::

    from backtest.options.live_trading import LiveOptionTrader

    trader = LiveOptionTrader(broker=mstock_broker, dry_run=True)
    fills = trader.execute_structure(trade_intent)

Arming live orders (fail-closed, three gates)
---------------------------------------------

This class defaults to **dry-run**. Real orders are impossible unless a
caller clears all three gates at construction:

1. ``dry_run=False`` — the explicit opt-out of the safe default;
2. ``confirm_live=True`` — a per-instance, deliberate confirmation
   (refuses to inherit live mode from a copy-pasted config);
3. environment kill-switch ``ALLOW_LIVE_ORDERS=1`` — the ops-level switch;
   unset, live arming fails even with 1 + 2 (lets a desk disable all live
   placement centrally without a deploy).

Any gate missing → ``ValueError`` at construction, before any broker call.
Going live is logged loudly. The previous default (``dry_run=False``) placed
real orders whenever a caller omitted the flag — the exact opposite of the
repo's fail-closed principle (architect review 2026-09-17 §3.2).

V1 scope: sequential leg submission (not atomic at exchange level).
The exchange doesn't support multi-leg orders, so we submit each leg
and monitor for failures.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.mstock import (
    MStockBroker,
    MStockOrderError,
    OrderOutcomeUnknown,
)
from backtest.options.execution_gate import V1_ALLOWED_STRUCTURES, check_intent
from backtest.options.intent_journal import (
    KIND_DECIDED,
    KIND_FILLED,
    KIND_HALTED,
    KIND_RECONCILED,
    KIND_REJECTED,
    KIND_SUBMITTED,
    KIND_UNCERTAIN,
    MemoryJournal,
)
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
    #: A leg's outcome could not be established — execution stopped and the
    #: journal carries the ``uncertain`` record for reconciliation.
    HALTED = "halted"
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
    #: The broker never answered for this leg, so it may or may not exist at the
    #: exchange. Nothing may be placed or flattened on top of it — reconcile.
    uncertain: bool = False


@dataclass
class StructureResult:
    """Result of executing a complete multi-leg structure."""

    structure_id: str
    structure_type: str
    status: StructureExecStatus = StructureExecStatus.PENDING
    #: Journal key for this decision (see :func:`intent_journal.intent_key`).
    intent_id: str = ""
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
    journal:
        Append-only intent journal (see
        :mod:`backtest.options.intent_journal`). It holds the idempotency keys:
        one leg is placed at most once per decision, and an uncertain submit is
        reconciled rather than re-sent. Defaults to the process-local
        :class:`MemoryJournal`, which cannot survive a restart — arming live
        orders on it logs a warning.
    poll_interval_seconds:
        How often to poll for order status updates.
    poll_timeout_seconds:
        Max time to wait for all legs to fill before giving up.
    allowed_structures:
        Execution-gate whitelist (see
        :mod:`backtest.options.execution_gate`). Defaults to the V1 set, checked
        before any broker call — a short-option or multi-leg intent never
        reaches ``place_order`` here, whatever built it.
    """

    def __init__(
        self,
        broker: MStockBroker,
        dry_run: bool = True,
        journal: Any | None = None,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 60.0,
        confirm_live: bool = False,
        allowed_structures: Any | None = None,
    ) -> None:
        # Fail-closed arming (see module docstring): live orders require ALL
        # THREE gates. Any gate missing raises BEFORE any broker call — the
        # default posture is log-only, never placement.
        self.dry_run = bool(dry_run)
        if not self.dry_run:
            if not confirm_live:
                raise ValueError(
                    "[live-options] refusing to arm live orders: dry_run=False "
                    "requires confirm_live=True (explicit per-instance confirmation)"
                )
            if os.environ.get("ALLOW_LIVE_ORDERS", "").strip().lower() not in (
                "1",
                "true",
                "yes",
            ):
                raise ValueError(
                    "[live-options] refusing to arm live orders: environment "
                    "kill-switch ALLOW_LIVE_ORDERS is not set — export "
                    "ALLOW_LIVE_ORDERS=1 to allow real order placement"
                )
            # Panel gate (certified v2 §0 #4/#6) — only bites when the
            # settings DB is configured; legacy env behavior otherwise.
            try:
                from backtest.api.segments_store import assert_live_arming_allowed

                assert_live_arming_allowed()
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 — fail closed
                raise ValueError(
                    "[live-options] refusing to arm live orders: Cost & Risk "
                    f"Settings is configured but unreachable ({exc})"
                ) from exc
            logger.warning(
                "[live-options] LIVE MODE ARMED — real orders will be placed "
                "via %r; every submission is logged",
                type(broker).__name__,
            )
        # Telemetry: True only when the trader is actually armed for live.
        self.confirm_live = bool(confirm_live) and not self.dry_run

        self.broker = broker
        self.journal = journal if journal is not None else MemoryJournal()
        if self.confirm_live and isinstance(self.journal, MemoryJournal):
            self.journal.warn_if_live()
        self.poll_interval = poll_interval_seconds
        self.poll_timeout = poll_timeout_seconds
        self.allowed_structures = frozenset(
            allowed_structures
            if allowed_structures is not None
            else V1_ALLOWED_STRUCTURES
        )

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

        Submits legs sequentially. A definite broker rejection rolls the
        already-filled legs back; an uncertain one halts the structure so the
        journal can be reconciled against the broker before anything else moves.

        Parameters
        ----------
        intent:
            The trade intent from the expression layer.
        timestamp:
            Trade timestamp (defaults to now). It is also the journal's clock
            when the intent carries no bar timestamp of its own.

        Returns
        -------
        ``StructureResult`` with per-leg outcomes.

        Raises
        ------
        StructureNotAllowedError
            If the intent fails the execution gate — before any broker call.
        """
        check_intent(intent, self.allowed_structures)

        ts = timestamp or datetime.utcnow()
        structure_id = str(uuid.uuid4())

        # The journal key comes from the decision, not from ``structure_id``:
        # replaying this decision hashes to the same intent, which is what lets
        # a restart recognise its own submitted leg instead of placing twice.
        intent_id = self.journal.append_for_intent(
            intent,
            KIND_DECIDED,
            action="open",
            clock=ts,
            structure_id=structure_id,
            detail={"dry_run": self.dry_run},
        )

        result = StructureResult(
            structure_id=structure_id,
            structure_type=intent.structure_type,
            intent_id=intent_id,
            status=StructureExecStatus.IN_PROGRESS,
            submitted_at=ts,
        )

        logger.info(
            "[live-options] Executing %s structure=%s intent=%s legs=%d dry_run=%s",
            intent.structure_type,
            structure_id[:8],
            intent_id[:8],
            len(intent.legs),
            self.dry_run,
        )

        # Submit each leg sequentially
        filled_legs: list[LegResult] = []

        for i, leg in enumerate(intent.legs):
            leg_result = self._submit_leg(leg, i, structure_id, intent_id)
            result.legs.append(leg_result)

            if leg_result.status == LegStatus.FILLED:
                filled_legs.append(leg_result)
                logger.info(
                    "[live-options] Leg %d/%d filled: %s %s x%d @ %.2f",
                    i + 1, len(intent.legs),
                    leg.side, leg.trading_symbol,
                    leg.quantity, leg_result.fill_price,
                )
                continue

            if leg_result.uncertain:
                # Outcome unknown — no flatten, no further legs. A rollback is
                # itself an order, and it would sit on top of a ticket we have
                # not proven absent.
                self.journal.append(
                    intent_id=intent_id,
                    kind=KIND_HALTED,
                    structure_id=structure_id,
                    detail={
                        "leg_index": i,
                        "reason": leg_result.error,
                        "filled_legs": len(filled_legs),
                    },
                )
                logger.error(
                    "[live-options] Structure %s HALTED on leg %d/%d with an unknown "
                    "outcome (%s) — reconcile intent %s against the broker book",
                    structure_id[:8],
                    i + 1, len(intent.legs), leg_result.error, intent_id[:8],
                )
                result.status = StructureExecStatus.HALTED
                result.error = f"Leg {i + 1} outcome unknown: {leg_result.error}"
                result.completed_at = datetime.utcnow()
                self._results[structure_id] = result
                return result

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
    # Leg submission — at most once per decision
    # ------------------------------------------------------------------

    def _submit_leg(
        self,
        leg: OptionLeg,
        leg_index: int,
        structure_id: str,
        intent_id: str,
    ) -> LegResult:
        """Place one leg, reconcile it, and journal every outcome.

        There is deliberately no retry loop here. A leg is placed once per
        decision; a call that ended without an answer is reconciled against the
        broker's own book (see :meth:`_reconcile`), never re-sent.
        """
        result = LegResult(leg=leg, submitted_at=datetime.utcnow())

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

        # Idempotency guard: the journal already holds a ticket for this leg,
        # so it exists at the broker. Settle that order instead of placing one.
        known = self.journal.order_id_for(intent_id, leg_index)
        if known:
            logger.warning(
                "[live-options] Leg %d of intent %s is already submitted (%s) — "
                "reconciling, not re-placing",
                leg_index + 1, intent_id[:8], known,
            )
            result.broker_order_id = str(known)
            result.status = LegStatus.SUBMITTED
            return self._settle(result, BrokerOrderId(str(known)), intent_id, leg_index)

        broker_order = self._build_broker_order(leg, structure_id, leg_index)
        try:
            order_id = self.broker.place_order(broker_order)
        except OrderOutcomeUnknown as exc:
            return self._mark_uncertain(result, exc, broker_order, intent_id, leg_index)
        except MStockOrderError as exc:
            # mStock answered and refused: nothing exists at the exchange.
            self.journal.append(
                intent_id=intent_id,
                kind=KIND_REJECTED,
                leg_index=leg_index,
                structure_id=structure_id,
                client_order_id=broker_order.client_order_id,
                detail={"error": str(exc), "status_code": exc.status_code},
            )
            result.status = LegStatus.REJECTED
            result.error = str(exc)
            logger.warning(
                "[live-options] Leg %d rejected by the broker: %s", leg_index + 1, exc
            )
            return result

        self.journal.append(
            intent_id=intent_id,
            kind=KIND_SUBMITTED,
            leg_index=leg_index,
            structure_id=structure_id,
            broker_order_id=str(order_id),
            client_order_id=broker_order.client_order_id,
            detail={
                "symbol": broker_order.symbol,
                "side": broker_order.side,
                "quantity": broker_order.quantity,
            },
        )
        result.broker_order_id = str(order_id)
        result.status = LegStatus.SUBMITTED
        return self._settle(result, order_id, intent_id, leg_index)

    def _settle(
        self,
        result: LegResult,
        order_id: BrokerOrderId,
        intent_id: str,
        leg_index: int,
    ) -> LegResult:
        """Poll a submitted order to a terminal state and journal it."""
        fill = self._poll_for_fill(order_id, result.leg)
        if fill is not None:
            result.status = LegStatus.FILLED
            result.fill_price = fill.get("fill_price", 0.0)
            result.fill_quantity = fill.get("fill_quantity", 0)
            result.filled_at = datetime.utcnow()
            self.journal.append(
                intent_id=intent_id,
                kind=KIND_FILLED,
                leg_index=leg_index,
                broker_order_id=str(order_id),
                detail=dict(fill),
            )
            return result

        # The order was accepted and never confirmed within the poll window.
        # That is not a rejection — it may still be live at the exchange.
        result.status = LegStatus.REJECTED
        result.uncertain = True
        result.error = "Order not filled within timeout"
        self.journal.append(
            intent_id=intent_id,
            kind=KIND_UNCERTAIN,
            leg_index=leg_index,
            broker_order_id=str(order_id),
            detail={"reason": "no fill confirmation within poll timeout"},
        )
        return result

    def _mark_uncertain(
        self,
        result: LegResult,
        exc: OrderOutcomeUnknown,
        broker_order: BrokerOrder,
        intent_id: str,
        leg_index: int,
    ) -> LegResult:
        """Record an unanswered placement, then reconcile it once.

        The ``uncertain`` record is written before the read so a crash between
        the two still leaves the decision on disk. If reconciliation proves the
        order exists, its ``submitted`` key is filled in too — that is what
        makes the *next* replay skip ``place_order`` outright.
        """
        result.error = str(exc)
        result.status = LegStatus.FAILED
        result.uncertain = True
        self.journal.append(
            intent_id=intent_id,
            kind=KIND_UNCERTAIN,
            leg_index=leg_index,
            client_order_id=broker_order.client_order_id,
            detail={"error": str(exc), "status_code": exc.status_code},
            dedupe=False,
        )
        logger.error(
            "[live-options] Leg %d (%s) placement unanswered: %s — reconciling the "
            "broker book once, will not re-send",
            leg_index + 1, broker_order.symbol, exc,
        )

        found = self._reconcile(broker_order)
        if found is None:
            return result

        self.journal.append(
            intent_id=intent_id,
            kind=KIND_RECONCILED,
            leg_index=leg_index,
            broker_order_id=found["broker_order_id"],
            client_order_id=found.get("client_order_id"),
            detail=dict(found),
            dedupe=False,
        )
        if found["broker_order_id"]:
            self.journal.append(
                intent_id=intent_id,
                kind=KIND_SUBMITTED,
                leg_index=leg_index,
                broker_order_id=found["broker_order_id"],
                client_order_id=found.get("client_order_id"),
                detail={"source": "reconciliation", "status": found["status"]},
            )
            result.broker_order_id = found["broker_order_id"]

        if found["fill_quantity"] > 0:
            result.status = LegStatus.FILLED
            result.uncertain = False
            result.fill_price = found["fill_price"]
            result.fill_quantity = found["fill_quantity"]
            result.filled_at = datetime.utcnow()
        return result

    def _reconcile(self, broker_order: BrokerOrder) -> dict[str, Any] | None:
        """One pass over the broker's book for the order we may have placed.

        mStock's order rows do not reliably echo ``client_order_id``, so the
        match is that id when present, else the exact symbol/side/quantity of
        the packet we sent. Ambiguity is resolved against us: the first
        matching row only.
        """
        try:
            rows = self.broker.get_order_book()
        except MStockOrderError as exc:
            logger.warning("[live-options] Reconciliation read failed: %s", exc)
            return None

        wanted = (
            (broker_order.symbol or "").strip().upper(),
            (broker_order.side or "").strip().upper(),
            int(broker_order.quantity),
        )
        for order in rows:
            matches_client = (
                broker_order.client_order_id is not None
                and order.client_order_id == broker_order.client_order_id
            )
            matches_packet = (
                (order.symbol or "").strip().upper() == wanted[0]
                and (order.side or "").strip().upper() == wanted[1]
                and int(order.quantity or 0) == wanted[2]
            )
            if not (matches_client or matches_packet):
                continue
            return {
                "broker_order_id": str(order.broker_order_id or ""),
                "client_order_id": order.client_order_id or broker_order.client_order_id,
                "status": (order.status or "").upper(),
                "fill_price": order.average_fill_price or 0.0,
                "fill_quantity": int(order.filled_quantity or 0),
            }
        return None

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
                    oid = str(order.broker_order_id) if order.broker_order_id else ""
                    if oid == order_id_str:
                        status = (order.status or "").upper()
                        if status in ("EXECUTED", "FILLED", "COMPLETE"):
                            return {
                                "fill_price": order.average_fill_price or 0.0,
                                "fill_quantity": int(order.filled_quantity or 0),
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
                    order_id,
                )
                leg_result.status = LegStatus.CANCELLED

            except OrderOutcomeUnknown as exc:
                # The flatten may or may not exist. Rollback is best-effort, but
                # an unanswered flatten leaves real exposure — say so loudly.
                logger.error(
                    "[live-options] Rollback outcome UNKNOWN for %s (%s): %s — "
                    "this position needs manual reconciliation",
                    leg_result.leg.trading_symbol, leg_result.broker_order_id, exc,
                )

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
