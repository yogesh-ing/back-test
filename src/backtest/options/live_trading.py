"""Live option trading via mStock — multi-leg order orchestration & adapter.

Extends the mStock broker with option-specific live trading logic:

- Multi-leg order submission (concurrent 2-leg same-side straddle/strangle
  dispatch; ``BUY``-wing-first SEBI hedged-margin sequencing on spreads)
- Sub-50 ms fill confirmation via ``MTicker.on_order_update`` /
  ``on_trade_update`` push events with HTTP order-book polling fallback
- Marketable ``LIMIT`` pricing rounded to NFO ``₹0.05`` ticks when a quote
  provider is attached (protecting against illiquid gap fills)
- Auto-cancel/flatten on definite sibling rejection; never blind-flatten on
  uncertain outcomes
- Append-only intent journal with idempotency keys (see
  :mod:`backtest.options.intent_journal`)
- :class:`LiveOptionBrokerAdapter` — wraps :class:`OptionPaperBroker` (bookkeeper,
  MTM, state snapshots, IST ``session_date`` roll, ``on_leg_fill`` ->
  ``OrderLedger.record_filled()``) around :class:`LiveOptionTrader` for
  ``mode="live"`` option runners

Never retry an uncertain submit
-------------------------------

A leg is placed **at most once** per decision. The journal key is hashed from
the decision itself, so a replay of the same decision (restart, re-fed bar)
finds the ticket that is already at the broker and reconciles it instead of
placing a second one. A call that ends without a usable answer — connection
loss, gateway 5xx, a 2xx with no order id — is an :class:`OrderOutcomeUnknown`:
it is recorded as ``uncertain``, reconciled once against the WS push cache and
broker order book, and the structure **halts**. It is never re-sent and never
rolled back, because a flatten on top of an order whose existence is unproven
can double a position.

A *definite* broker rejection (a 4xx, an error payload on a 200, or an exchange
``REJECTED``/``CANCELLED`` terminal status with zero fills) is the only outcome
that rolls back previously filled entry legs — there the venue has told us
nothing exists at the exchange.

Arming live orders (fail-closed, three gates)
---------------------------------------------

:class:`LiveOptionTrader` defaults to **dry-run**. Real orders are impossible
unless a caller clears all three gates at construction:

1. ``dry_run=False`` — the explicit opt-out of the safe default;
2. ``confirm_live=True`` — a per-instance, deliberate confirmation
   (refuses to inherit live mode from a copy-pasted config);
3. environment kill-switch ``ALLOW_LIVE_ORDERS=1`` — the ops-level switch;
   unset, live arming fails even with 1 + 2 (lets a desk disable all live
   placement centrally without a deploy).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.mstock import (
    MStockBroker,
    MStockOrderError,
    OrderOutcomeUnknown,
)
from backtest.options.execution_gate import V1_ALLOWED_STRUCTURES, check_intent
from backtest.timekeeping import ensure_utc, now_utc
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
from backtest.options.paper_trading import (
    ZERO,
    InsufficientMarginError,
    OptionPaperBroker,
    OptionPosition,
    PositionStatus,
    StructurePosition,
    _execution_quote,
    _ist_session_date,
)
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent

logger = logging.getLogger("backtest.options.live_trading")

NFO_TICK_SIZE: float = 0.05


def _round_nfo_tick(price: float, side: str) -> float:
    """Round an option limit price to the NFO ₹0.05 tick grid.

    ``BUY`` rounds UP (aggressive enough to cross the ask); ``SELL`` rounds
    DOWN (aggressive enough to hit the bid), clamped to ``>= ₹0.05``.
    """
    if price <= NFO_TICK_SIZE:
        return NFO_TICK_SIZE
    ticks = round(price / NFO_TICK_SIZE, 6)
    if str(side).upper() == "BUY":
        snapped = math.ceil(ticks) * NFO_TICK_SIZE
    else:
        snapped = max(NFO_TICK_SIZE, math.floor(ticks) * NFO_TICK_SIZE)
    return round(snapped, 2)


# ---------------------------------------------------------------------------
# Enums & Errors
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


class LiveExecutionHaltedError(RuntimeError):
    """Raised when a live multi-leg option execution halts in an uncertain state.

    Callers (``OptionsBridge`` / ``StrategyRunner``) must pause the runner for
    operator reconciliation and NEVER auto-retry or blind-flatten.
    """

    def __init__(self, message: str, *, result: "StructureResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


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
    requested_price: float | None = None
    limit_price: float | None = None
    order_type: str = "MARKET"


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
        return bool(self.legs) and all(
            leg.status == LegStatus.FILLED for leg in self.legs
        )

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
    """Orchestrates multi-leg option orders via mStock."""

    def __init__(
        self,
        broker: MStockBroker,
        dry_run: bool = True,
        journal: Any | None = None,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 60.0,
        confirm_live: bool = False,
        allowed_structures: Any | None = None,
        *,
        quote_provider: Any | None = None,
        use_marketable_limit: bool = True,
        limit_slippage_bps: float = 100.0,
        concurrent_same_side_legs: bool = True,
        precheck_margin: bool = False,
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
        self.quote_provider = quote_provider
        self.use_marketable_limit = bool(use_marketable_limit)
        self.limit_slippage_bps = max(0.0, float(limit_slippage_bps))
        self.concurrent_same_side_legs = bool(concurrent_same_side_legs)
        self.precheck_margin = bool(precheck_margin)

        # WS order/trade push event wakeup (<50 ms fill confirmation)
        self._order_lock = threading.RLock()
        self._order_events: dict[str, threading.Event] = {}
        self._ws_push_cache: dict[str, dict[str, Any]] = {}
        self._attach_broker_order_listener()

        # Tracking
        self._results: dict[str, StructureResult] = {}

    @staticmethod
    def _is_real_broker_obj(broker: Any) -> bool:
        return broker is not None and type(broker).__module__ != "unittest.mock"

    def _attach_broker_order_listener(self) -> None:
        if not self._is_real_broker_obj(self.broker):
            return
        add_fn = getattr(self.broker, "add_order_listener", None)
        if callable(add_fn):
            try:
                add_fn(self._on_ws_order_update)
            except Exception:  # noqa: BLE001
                logger.debug("Could not attach WS order listener", exc_info=True)

    def close(self) -> None:
        """Detach WS order listener when tearing down the trader."""
        if not self._is_real_broker_obj(self.broker):
            return
        rem_fn = getattr(self.broker, "remove_order_listener", None)
        if callable(rem_fn):
            try:
                rem_fn(self._on_ws_order_update)
            except Exception:  # noqa: BLE001
                pass

    def _on_ws_order_update(self, row: dict[str, Any]) -> None:
        oid = str(row.get("order_id") or "").strip()
        if not oid:
            return
        with self._order_lock:
            self._ws_push_cache[oid] = dict(row)
            while len(self._ws_push_cache) > 512:
                oldest = next(iter(self._ws_push_cache))
                self._ws_push_cache.pop(oldest, None)
            ev = self._order_events.get(oid)
            if ev is not None:
                ev.set()

    def _event_for_order(self, oid: str) -> threading.Event:
        with self._order_lock:
            ev = self._order_events.get(oid)
            if ev is None:
                ev = threading.Event()
                self._order_events[oid] = ev
            return ev

    def _release_order_event(self, oid: str) -> None:
        with self._order_lock:
            self._order_events.pop(oid, None)

    # ------------------------------------------------------------------
    # Pre-trade margin validation (OPT-4)
    # ------------------------------------------------------------------

    def _validate_pretrade_margin(
        self,
        legs: tuple[OptionLeg, ...],
        structure_id: str,
        quote_provider: Any | None,
    ) -> str | None:
        """Return an error string when pre-trade margin is insufficient, else None."""
        if not self.precheck_margin or self.dry_run:
            return None
        if not self._is_real_broker_obj(self.broker):
            return None
        margin_fn = getattr(self.broker, "calculate_order_margin", None)
        if not callable(margin_fn):
            return None

        total_required = 0.0
        available: float | None = None
        for idx, leg in enumerate(legs):
            order, _, _ = self._prepare_broker_order(
                leg, structure_id, idx, quote_provider=quote_provider, prefix="OPT"
            )
            try:
                info = margin_fn(order)
            except Exception as exc:  # noqa: BLE001 — margin endpoint hiccup logs only
                logger.warning("[live-options] margin precheck skipped (%s)", exc)
                return None
            req = getattr(info, "required_margin", None)
            avail = getattr(info, "available_margin", None)
            if req is not None:
                total_required += max(0.0, float(req))
            if avail is not None:
                available = float(avail)

        if available is not None and available > 0 and total_required > available:
            return (
                f"Pre-trade margin check failed: required ₹{total_required:,.2f} "
                f"> available ₹{available:,.2f}"
            )
        return None

    # ------------------------------------------------------------------
    # Core execution (entry & exit)
    # ------------------------------------------------------------------

    def execute_structure(
        self,
        intent: TradeIntent,
        timestamp: datetime | None = None,
        quote_provider: Any | None = None,
    ) -> StructureResult:
        """Execute a multi-leg option structure.

        * 2-leg same-side structures (``long_straddle``, ``short_straddle``,
          ``strangle``) dispatch both legs concurrently across 2 worker threads
          when ``concurrent_same_side_legs=True``.
        * Mixed ``BUY`` + ``SELL`` structures (``bull_call_spread``,
          ``bear_put_spread``) execute ``BUY`` protective wings first and confirm
          fill before sending ``SELL`` legs for SEBI hedged-margin benefit.
        """
        check_intent(intent, self.allowed_structures)

        qp = quote_provider if quote_provider is not None else self.quote_provider
        ts = ensure_utc(timestamp) or now_utc()
        structure_id = str(uuid.uuid4())

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

        margin_err = self._validate_pretrade_margin(intent.legs, structure_id, qp)
        if margin_err is not None:
            self.journal.append(
                intent_id=intent_id,
                kind=KIND_REJECTED,
                structure_id=structure_id,
                detail={"error": margin_err},
            )
            result.status = StructureExecStatus.ROLLED_BACK
            result.error = margin_err
            result.completed_at = now_utc()
            self._results[structure_id] = result
            return result

        legs = tuple(intent.legs)
        is_two_leg_same_side = (
            len(legs) == 2
            and str(legs[0].side).upper() == str(legs[1].side).upper()
            and self.concurrent_same_side_legs
            and not self.dry_run
        )

        if is_two_leg_same_side:
            return self._execute_concurrent_pair(
                legs=legs,
                intent=intent,
                result=result,
                structure_id=structure_id,
                intent_id=intent_id,
                quote_provider=qp,
                allow_rollback=True,
                order_prefix="OPT",
            )

        # Sequential path: for mixed BUY+SELL structures (spreads), ensure BUY
        # protective wings execute before SELL legs (OPT-4 SEBI hedged margin).
        sides = {str(leg_item.side).upper() for leg_item in legs}
        if len(sides) > 1:
            exec_indices = sorted(
                range(len(legs)),
                key=lambda idx: 0 if str(legs[idx].side).upper() == "BUY" else 1,
            )
        else:
            exec_indices = list(range(len(legs)))

        indexed_results: dict[int, LegResult] = {}
        filled_legs: list[LegResult] = []

        for i in exec_indices:
            leg = legs[i]
            leg_result = self._submit_leg(
                leg, i, structure_id, intent_id, quote_provider=qp, order_prefix="OPT"
            )
            indexed_results[i] = leg_result
            result.legs = [indexed_results[k] for k in sorted(indexed_results)]

            if leg_result.status == LegStatus.FILLED:
                filled_legs.append(leg_result)
                logger.info(
                    "[live-options] Leg %d/%d filled: %s %s x%d @ %.2f",
                    i + 1,
                    len(legs),
                    leg.side,
                    leg.trading_symbol,
                    leg.quantity,
                    leg_result.fill_price,
                )
                continue

            if leg_result.uncertain:
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
                    i + 1,
                    len(legs),
                    leg_result.error,
                    intent_id[:8],
                )
                result.status = StructureExecStatus.HALTED
                result.error = f"Leg {i + 1} outcome unknown: {leg_result.error}"
                result.completed_at = now_utc()
                self._results[structure_id] = result
                return result

            logger.warning(
                "[live-options] Leg %d/%d failed: %s — rolling back %d filled legs",
                i + 1,
                len(legs),
                leg_result.error,
                len(filled_legs),
            )
            self._rollback(filled_legs, intent)
            result.status = StructureExecStatus.ROLLED_BACK
            result.error = f"Leg {i + 1} failed: {leg_result.error}"
            result.completed_at = now_utc()
            self._results[structure_id] = result
            return result

        result.legs = [indexed_results[k] for k in range(len(legs))]
        result.status = StructureExecStatus.FILLED
        result.completed_at = now_utc()
        self._results[structure_id] = result

        logger.info(
            "[live-options] Structure %s fully filled (%d legs)",
            structure_id[:8],
            len(filled_legs),
        )
        return result

    def close_structure(
        self,
        structure: StructurePosition,
        quote_provider: Any | None = None,
        timestamp: datetime | None = None,
        reason: str = "manual",
    ) -> StructureResult:
        """Close all open legs of ``structure`` at the live venue.

        * Mixed spreads close ``SHORT`` legs (``BUY`` to cover) BEFORE selling
          protective ``LONG`` wings so the account is never naked short.
        * 2-leg same-side structures (straddles/strangles) dispatch both closing
          legs concurrently.
        * Closing legs NEVER roll back (re-opening an exited leg on failure would
          re-create market exposure); any partial/uncertain close halts cleanly
          for operator reconciliation.
        """
        qp = quote_provider if quote_provider is not None else self.quote_provider
        ts = ensure_utc(timestamp) or now_utc()
        open_positions = [
            p for p in structure.legs if p.status == PositionStatus.OPEN
        ]
        if not open_positions:
            return StructureResult(
                structure_id=structure.structure_id,
                structure_type=structure.structure_type,
                status=StructureExecStatus.FILLED,
                submitted_at=ts,
                completed_at=ts,
            )

        closing_legs = tuple(
            OptionLeg(
                instrument_token=pos.instrument_token,
                trading_symbol=pos.trading_symbol,
                side="SELL" if pos.is_long else "BUY",
                quantity=int(pos.quantity),
                lot_size=int(pos.lot_size),
            )
            for pos in open_positions
        )
        synthetic_view = MarketView(
            direction=Direction.NEUTRAL,
            confidence=1.0,
            underlying=structure.underlying,
            bar_timestamp=ts,
        )
        close_intent = TradeIntent(
            view=synthetic_view,
            structure_type=structure.structure_type,
            legs=closing_legs,
            expiry=structure.expiry,
            strategy_name=structure.strategy_name,
        )
        # Keyed off structure_id so crash-replay of the same close finds the
        # already-submitted closing ticket rather than double-closing.
        intent_id = self.journal.append_for_intent(
            close_intent,
            KIND_DECIDED,
            action="close",
            clock=structure.structure_id,
            structure_id=structure.structure_id,
            detail={"dry_run": self.dry_run, "reason": reason},
        )

        result = StructureResult(
            structure_id=structure.structure_id,
            structure_type=structure.structure_type,
            intent_id=intent_id,
            status=StructureExecStatus.IN_PROGRESS,
            submitted_at=ts,
        )

        is_two_leg_same_side = (
            len(closing_legs) == 2
            and closing_legs[0].side == closing_legs[1].side
            and self.concurrent_same_side_legs
            and not self.dry_run
        )
        if is_two_leg_same_side:
            return self._execute_concurrent_pair(
                legs=closing_legs,
                intent=close_intent,
                result=result,
                structure_id=structure.structure_id,
                intent_id=intent_id,
                quote_provider=qp,
                allow_rollback=False,
                order_prefix="CLS",
            )

        sides = {leg_item.side for leg_item in closing_legs}
        if len(sides) > 1:
            # Close short legs (BUY-to-cover) before selling protective long legs!
            exec_indices = sorted(
                range(len(closing_legs)),
                key=lambda idx: 0 if closing_legs[idx].side == "BUY" else 1,
            )
        else:
            exec_indices = list(range(len(closing_legs)))

        indexed_results: dict[int, LegResult] = {}
        for i in exec_indices:
            leg = closing_legs[i]
            leg_res = self._submit_leg(
                leg,
                i,
                structure.structure_id,
                intent_id,
                quote_provider=qp,
                order_prefix="CLS",
            )
            indexed_results[i] = leg_res
            result.legs = [indexed_results[k] for k in sorted(indexed_results)]
            if leg_res.status == LegStatus.FILLED:
                continue

            self.journal.append(
                intent_id=intent_id,
                kind=KIND_HALTED,
                structure_id=structure.structure_id,
                detail={
                    "action": "close",
                    "leg_index": i,
                    "reason": leg_res.error,
                    "uncertain": leg_res.uncertain,
                },
            )
            result.status = StructureExecStatus.HALTED
            result.error = f"Close leg {i + 1} incomplete: {leg_res.error}"
            result.completed_at = now_utc()
            self._results[f"close:{structure.structure_id}"] = result
            return result

        result.legs = [indexed_results[k] for k in range(len(closing_legs))]
        result.status = StructureExecStatus.FILLED
        result.completed_at = now_utc()
        self._results[f"close:{structure.structure_id}"] = result
        return result

    def _execute_concurrent_pair(
        self,
        *,
        legs: tuple[OptionLeg, ...],
        intent: TradeIntent,
        result: StructureResult,
        structure_id: str,
        intent_id: str,
        quote_provider: Any | None,
        allow_rollback: bool,
        order_prefix: str,
    ) -> StructureResult:
        """Dispatch a 2-leg same-side pair concurrently across 2 worker threads."""
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="opt-leg") as pool:
            f0 = pool.submit(
                self._submit_leg,
                legs[0],
                0,
                structure_id,
                intent_id,
                quote_provider=quote_provider,
                order_prefix=order_prefix,
            )
            f1 = pool.submit(
                self._submit_leg,
                legs[1],
                1,
                structure_id,
                intent_id,
                quote_provider=quote_provider,
                order_prefix=order_prefix,
            )
            r0 = f0.result()
            r1 = f1.result()

        result.legs = [r0, r1]
        filled = [r for r in result.legs if r.status == LegStatus.FILLED]
        uncertain = [
            (idx, r) for idx, r in enumerate(result.legs) if r.uncertain
        ]

        if len(filled) == len(legs):
            result.status = StructureExecStatus.FILLED
            result.completed_at = now_utc()
            self._results[structure_id] = result
            return result

        if uncertain or not allow_rollback:
            bad_idx, bad_leg = (
                uncertain[0]
                if uncertain
                else next(
                    (idx, r)
                    for idx, r in enumerate(result.legs)
                    if r.status != LegStatus.FILLED
                )
            )
            self.journal.append(
                intent_id=intent_id,
                kind=KIND_HALTED,
                structure_id=structure_id,
                detail={
                    "leg_index": bad_idx,
                    "reason": bad_leg.error,
                    "filled_legs": len(filled),
                },
            )
            result.status = StructureExecStatus.HALTED
            result.error = f"Leg {bad_idx + 1} halted: {bad_leg.error}"
            result.completed_at = now_utc()
            self._results[structure_id] = result
            return result

        failed_idx, failed_leg = next(
            (idx, r)
            for idx, r in enumerate(result.legs)
            if r.status != LegStatus.FILLED
        )
        if filled:
            self._rollback(filled, intent)
        result.status = StructureExecStatus.ROLLED_BACK
        result.error = f"Leg {failed_idx + 1} failed: {failed_leg.error}"
        result.completed_at = now_utc()
        self._results[structure_id] = result
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
        *,
        quote_provider: Any | None = None,
        order_prefix: str = "OPT",
    ) -> LegResult:
        """Place one leg, reconcile it, and journal every outcome.

        There is deliberately no retry loop here. A leg is placed once per
        decision; a call that ended without an answer is reconciled against the
        broker's own book (see :meth:`_reconcile`), never re-sent.
        """
        result = LegResult(leg=leg, submitted_at=now_utc())
        broker_order, req_px, lim_px = self._prepare_broker_order(
            leg,
            structure_id,
            leg_index,
            quote_provider=quote_provider,
            prefix=order_prefix,
        )
        result.requested_price = req_px
        result.limit_price = lim_px
        result.order_type = broker_order.order_type

        if self.dry_run:
            result.status = LegStatus.FILLED
            result.fill_price = float(req_px or 0.0)
            result.fill_quantity = leg.total_quantity
            result.broker_order_id = f"DRY-{uuid.uuid4().hex[:8]}"
            logger.info(
                "[live-options] DRY RUN: would submit %s %s x%d (%s)",
                leg.side,
                leg.trading_symbol,
                leg.quantity,
                broker_order.order_type,
            )
            return result

        # Idempotency guard: the journal already holds a ticket for this leg,
        # so it exists at the broker. Settle that order instead of placing one.
        known = self.journal.order_id_for(intent_id, leg_index)
        if known:
            logger.warning(
                "[live-options] Leg %d of intent %s is already submitted (%s) — "
                "reconciling, not re-placing",
                leg_index + 1,
                intent_id[:8],
                known,
            )
            result.broker_order_id = str(known)
            result.status = LegStatus.SUBMITTED
            return self._settle(result, BrokerOrderId(str(known)), intent_id, leg_index)

        try:
            order_id = self.broker.place_order(broker_order)
        except OrderOutcomeUnknown as exc:
            return self._mark_uncertain(result, exc, broker_order, intent_id, leg_index)
        except MStockOrderError as exc:
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
                "order_type": broker_order.order_type,
                "limit_price": broker_order.limit_price,
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
        """Wait for WS push / order-book terminal state and journal it."""
        outcome, payload = self._poll_terminal_state(order_id, result.leg)
        if outcome == "filled" and payload is not None:
            result.status = LegStatus.FILLED
            result.fill_price = float(payload.get("fill_price", 0.0) or 0.0)
            result.fill_quantity = int(payload.get("fill_quantity", 0) or 0)
            result.filled_at = now_utc()
            self.journal.append(
                intent_id=intent_id,
                kind=KIND_FILLED,
                leg_index=leg_index,
                broker_order_id=str(order_id),
                detail=dict(payload),
            )
            return result

        if outcome == "rejected" and payload is not None:
            status_label = str(payload.get("status") or "REJECTED")
            result.status = LegStatus.REJECTED
            result.uncertain = False
            result.error = f"Order {status_label} at exchange"
            self.journal.append(
                intent_id=intent_id,
                kind=KIND_REJECTED,
                leg_index=leg_index,
                broker_order_id=str(order_id),
                detail=dict(payload),
            )
            return result

        # Timeout: the order was accepted and never confirmed within the window.
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
        """Record an unanswered placement, then reconcile it once (OPT-3)."""
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
            leg_index + 1,
            broker_order.symbol,
            exc,
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
            result.filled_at = now_utc()
        elif found["status"] in ("REJECTED", "CANCELLED", "EXPIRED"):
            result.status = LegStatus.REJECTED
            result.uncertain = False
            result.error = f"Reconciled order status {found['status']}"
        return result

    def _reconcile(self, broker_order: BrokerOrder) -> dict[str, Any] | None:
        """One pass over WS push cache + broker's HTTP book for an uncertain order."""
        # 1. Check WS order push cache first if available
        if self._is_real_broker_obj(self.broker):
            find_fn = getattr(self.broker, "find_ws_order_update", None)
            if callable(find_fn):
                ws_row = find_fn(
                    client_order_id=broker_order.client_order_id,
                    symbol=broker_order.symbol,
                    side=broker_order.side,
                    quantity=int(broker_order.quantity),
                )
                if isinstance(ws_row, dict) and ws_row.get("order_id"):
                    status = str(ws_row.get("status") or "").upper()
                    filled_qty = int(ws_row.get("filled_quantity") or 0)
                    avg_px = float(ws_row.get("average_price") or 0.0)
                    if (
                        status in ("EXECUTED", "FILLED", "COMPLETE")
                        and filled_qty > 0
                        and avg_px > 0.0
                    ) or status in ("REJECTED", "CANCELLED", "EXPIRED"):
                        return {
                            "broker_order_id": str(ws_row["order_id"]),
                            "client_order_id": (
                                ws_row.get("client_order_id")
                                or broker_order.client_order_id
                            ),
                            "status": status,
                            "fill_price": avg_px,
                            "fill_quantity": filled_qty,
                        }

        # 2. Reconcile once against HTTP order book
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
    # Order building & marketable LIMIT pricing
    # ------------------------------------------------------------------

    def _prepare_broker_order(
        self,
        leg: OptionLeg,
        structure_id: str,
        leg_index: int,
        *,
        quote_provider: Any | None = None,
        prefix: str = "OPT",
    ) -> tuple[BrokerOrder, float | None, float | None]:
        """Build a BrokerOrder along with ``(requested_price, limit_price)``."""
        req_price: float | None = None
        limit_price: float | None = None
        order_type = "MARKET"

        if quote_provider is not None:
            try:
                quote = _execution_quote(
                    quote_provider, leg.instrument_token, leg.side
                )
            except Exception:  # noqa: BLE001 — fall back to MARKET if quote reader raises
                quote = None
            if isinstance(quote, dict) and not quote.get("error"):
                side_up = str(leg.side).upper()
                raw_px = (
                    quote.get("ask")
                    if side_up == "BUY" and float(quote.get("ask") or 0.0) > 0
                    else quote.get("bid")
                    if side_up == "SELL" and float(quote.get("bid") or 0.0) > 0
                    else quote.get("ltp")
                )
                try:
                    px_f = float(raw_px or 0.0)
                except (TypeError, ValueError):
                    px_f = 0.0
                if px_f > 0.0:
                    req_price = px_f
                    if self.use_marketable_limit:
                        buffer = max(0.10, px_f * (self.limit_slippage_bps / 10_000.0))
                        target = px_f + buffer if side_up == "BUY" else px_f - buffer
                        limit_price = _round_nfo_tick(target, side_up)
                        order_type = "LIMIT"

        order = BrokerOrder(
            client_order_id=f"{prefix}-{structure_id[:8]}-{leg_index}",
            symbol=leg.trading_symbol,
            side=leg.side,
            quantity=leg.total_quantity,
            order_type=order_type,
            limit_price=limit_price,
            exchange="NFO",
            product="INTRADAY",
            tag={
                "structure_id": structure_id,
                "leg_index": leg_index,
                "instrument_token": leg.instrument_token,
            },
        )
        return order, req_price, limit_price

    def _build_broker_order(
        self,
        leg: OptionLeg,
        structure_id: str,
        leg_index: int,
        *,
        quote_provider: Any | None = None,
    ) -> BrokerOrder:
        """Build a BrokerOrder from an OptionLeg (MARKET when no quote_provider)."""
        order, _, _ = self._prepare_broker_order(
            leg,
            structure_id,
            leg_index,
            quote_provider=quote_provider,
            prefix="OPT",
        )
        return order

    # ------------------------------------------------------------------
    # Order status polling + WS push fast-path (<50 ms)
    # ------------------------------------------------------------------

    def _lookup_ws_push_row(self, order_id_str: str) -> dict[str, Any] | None:
        with self._order_lock:
            cached = self._ws_push_cache.get(order_id_str)
            if cached is not None:
                return dict(cached)
        if self._is_real_broker_obj(self.broker):
            get_ws = getattr(self.broker, "get_ws_order_update", None)
            if callable(get_ws):
                row = get_ws(order_id_str)
                if isinstance(row, dict):
                    return row
        return None

    def _poll_terminal_state(
        self,
        order_id: BrokerOrderId,
        leg: OptionLeg,
    ) -> tuple[str, dict[str, Any] | None]:
        """Wait for terminal fill/rejection via WS push Event + HTTP book fallback."""
        order_id_str = str(order_id)
        ev = self._event_for_order(order_id_str)
        start = time.monotonic()
        try:
            while True:
                # 1. Zero-latency WS push check (<50 ms)
                ws_row = self._lookup_ws_push_row(order_id_str)
                if ws_row is not None:
                    ws_status = str(ws_row.get("status") or "").upper()
                    ws_px = float(ws_row.get("average_price") or 0.0)
                    ws_qty = int(ws_row.get("filled_quantity") or 0)
                    if (
                        ws_status in ("EXECUTED", "FILLED", "COMPLETE")
                        and ws_px > 0.0
                        and ws_qty > 0
                    ):
                        return (
                            "filled",
                            {
                                "fill_price": ws_px,
                                "fill_quantity": ws_qty,
                                "status": ws_status,
                                "via": "ws",
                            },
                        )
                    if ws_status in ("REJECTED", "CANCELLED", "EXPIRED") and ws_qty <= 0:
                        return ("rejected", {"status": ws_status, "via": "ws"})

                # 2. HTTP order-book check
                try:
                    order_book = self.broker.get_order_book()
                    for order in order_book:
                        oid = str(order.broker_order_id) if order.broker_order_id else ""
                        if oid == order_id_str:
                            status = (order.status or "").upper()
                            if status in ("EXECUTED", "FILLED", "COMPLETE"):
                                return (
                                    "filled",
                                    {
                                        "fill_price": order.average_fill_price or 0.0,
                                        "fill_quantity": int(
                                            order.filled_quantity or leg.total_quantity
                                        ),
                                        "status": status,
                                        "via": "http",
                                    },
                                )
                            if status in ("REJECTED", "CANCELLED", "EXPIRED"):
                                return ("rejected", {"status": status, "via": "http"})
                except MStockOrderError as exc:
                    logger.warning("[live-options] Order book poll failed: %s", exc)

                elapsed = time.monotonic() - start
                remaining = self.poll_timeout - elapsed
                if remaining <= 0:
                    break
                ev.wait(timeout=min(self.poll_interval, remaining))
                ev.clear()
        finally:
            self._release_order_event(order_id_str)

        return ("timeout", None)

    def _poll_for_fill(
        self,
        order_id: BrokerOrderId,
        leg: OptionLeg,
    ) -> dict[str, Any] | None:
        """Poll order status until filled, rejected, or timeout."""
        outcome, payload = self._poll_terminal_state(order_id, leg)
        if outcome == "filled":
            return payload
        return None

    # ------------------------------------------------------------------
    # Rollback — cancel filled legs when a subsequent leg fails
    # ------------------------------------------------------------------

    def _rollback(
        self,
        filled_legs: list[LegResult],
        intent: TradeIntent,
    ) -> None:
        """Flatten previously filled entry legs on definite sibling rejection."""
        for leg_result in filled_legs:
            try:
                if self.dry_run:
                    logger.info(
                        "[live-options] DRY RUN: would cancel/flatten %s %s",
                        leg_result.leg.side,
                        leg_result.leg.trading_symbol,
                    )
                    continue

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
                    flatten_side,
                    leg_result.leg.trading_symbol,
                    order_id,
                )
                leg_result.status = LegStatus.CANCELLED

            except OrderOutcomeUnknown as exc:
                logger.error(
                    "[live-options] Rollback outcome UNKNOWN for %s (%s): %s — "
                    "this position needs manual reconciliation",
                    leg_result.leg.trading_symbol,
                    leg_result.broker_order_id,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[live-options] Rollback failed for %s: %s",
                    leg_result.leg.trading_symbol,
                    exc,
                )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_result(self, structure_id: str) -> StructureResult | None:
        return self._results.get(structure_id)

    def get_all_results(self) -> list[StructureResult]:
        return list(self._results.values())


# ---------------------------------------------------------------------------
# LiveOptionBrokerAdapter — bookkeeper + venue execution for mode="live" (OPT-1)
# ---------------------------------------------------------------------------

class LiveOptionBrokerAdapter:
    """Adapts :class:`LiveOptionTrader` onto the :class:`OptionPaperBroker` seam.

    * **Venue execution**: ``execute_structure`` and ``close_structure`` place
      real orders through :attr:`trader` (fail-closed behind ``confirm_live=True``,
      ``ALLOW_LIVE_ORDERS=1``, ``assert_live_arming_allowed()``, and an
      authenticated broker session).
    * **Authoritative bookkeeping**: :attr:`book` (:class:`OptionPaperBroker`)
      maintains ``StructurePosition`` / ``OptionPosition`` MTM, cash, statutory
      fees, IST ``session_date`` roll, ``StateStore`` snapshots, and fires
      ``on_leg_fill`` -> ``OrderLedger.record_filled()`` with real venue
      ``avg_fill_price`` vs WS ``requested_price`` so live slippage and
      ``broker_order_id`` appear in the Orders tab identically to equities.
    """

    is_live_adapter: bool = True

    def __init__(
        self,
        *,
        trader: LiveOptionTrader,
        capital: float = 100_000.0,
        commission_per_lot: float = 20.0,
        fee_calculator: Any | None = None,
        allowed_structures: Any | None = None,
        on_structure_opened: Any | None = None,
        on_structure_closed: Any | None = None,
        on_leg_fill: Any | None = None,
    ) -> None:
        if trader.dry_run or not trader.confirm_live:
            raise ValueError(
                "[live-options] LiveOptionBrokerAdapter requires a live-armed "
                "LiveOptionTrader (dry_run=False, confirm_live=True, ALLOW_LIVE_ORDERS=1)"
            )
        is_authed = getattr(trader.broker, "is_authenticated", None)
        if not callable(is_authed) or not is_authed():
            raise ValueError(
                "[live-options] LiveOptionBrokerAdapter refused: broker session "
                "is not authenticated"
            )

        effective_allowed = frozenset(
            allowed_structures
            if allowed_structures is not None
            else trader.allowed_structures
        )
        self.trader = trader
        self.trader.allowed_structures = effective_allowed
        self.book = OptionPaperBroker(
            capital=capital,
            slippage_pct=0.0,  # real venue fill prices already include market impact
            commission_per_lot=commission_per_lot,
            fee_calculator=fee_calculator,
            on_structure_opened=on_structure_opened,
            on_structure_closed=on_structure_closed,
            on_leg_fill=on_leg_fill,
            allowed_structures=effective_allowed,
        )

    # -- delegated properties & state-store surface -------------------------

    @property
    def allowed_structures(self) -> frozenset[str]:
        return self.book.allowed_structures

    @allowed_structures.setter
    def allowed_structures(self, value: Any) -> None:
        frozen = frozenset(value)
        self.book.allowed_structures = frozen
        self.trader.allowed_structures = frozen

    @property
    def capital(self) -> Decimal:
        return self.book.capital

    @property
    def available_cash(self) -> Decimal:
        return self.book.available_cash

    @available_cash.setter
    def available_cash(self, value: Decimal) -> None:
        self.book.available_cash = Decimal(str(value))

    @property
    def fee_calculator(self) -> Any:
        return self.book.fee_calculator

    @property
    def _statutory_fees_paid(self) -> Decimal:
        return self.book._statutory_fees_paid

    @_statutory_fees_paid.setter
    def _statutory_fees_paid(self, value: Decimal) -> None:
        self.book._statutory_fees_paid = Decimal(str(value))

    @property
    def _positions(self) -> dict[str, OptionPosition]:
        return self.book._positions

    @property
    def _structures(self) -> dict[str, StructurePosition]:
        return self.book._structures

    @property
    def _order_history(self) -> list[dict[str, Any]]:
        return self.book._order_history

    @property
    def on_structure_opened(self) -> Any:
        return self.book.on_structure_opened

    @on_structure_opened.setter
    def on_structure_opened(self, fn: Any) -> None:
        self.book.on_structure_opened = fn

    @property
    def on_structure_closed(self) -> Any:
        return self.book.on_structure_closed

    @on_structure_closed.setter
    def on_structure_closed(self, fn: Any) -> None:
        self.book.on_structure_closed = fn

    @property
    def on_leg_fill(self) -> Any:
        return self.book.on_leg_fill

    @on_leg_fill.setter
    def on_leg_fill(self, fn: Any) -> None:
        self.book.on_leg_fill = fn

    def update_mtm(self, quote_provider: Any, timestamp: datetime | None = None) -> Decimal:
        return self.book.update_mtm(quote_provider, timestamp=timestamp)

    def expire_positions(
        self,
        expiry_date: Any,
        underlying_spot: Decimal,
        timestamp: datetime | None = None,
    ) -> Decimal:
        return self.book.expire_positions(expiry_date, underlying_spot, timestamp=timestamp)

    def settle_expired(self, *args: Any, **kwargs: Any) -> Any:
        return self.book.settle_expired(*args, **kwargs)

    def get_open_positions(self) -> list[OptionPosition]:
        return self.book.get_open_positions()

    def get_open_structures(self) -> list[StructurePosition]:
        return self.book.get_open_structures()

    def get_closed_structures(self) -> list[StructurePosition]:
        return self.book.get_closed_structures()

    def get_structure(self, structure_id: str) -> StructurePosition | None:
        return self.book.get_structure(structure_id)

    @property
    def total_realized_pnl(self) -> Decimal:
        return self.book.total_realized_pnl

    @property
    def total_unrealized_pnl(self) -> Decimal:
        return self.book.total_unrealized_pnl

    @property
    def total_commission_paid(self) -> Decimal:
        return self.book.total_commission_paid

    @property
    def total_statutory_fees_paid(self) -> Decimal:
        return self.book.total_statutory_fees_paid

    @property
    def total_costs_paid(self) -> Decimal:
        return self.book.total_costs_paid

    @property
    def total_equity(self) -> Decimal:
        return self.book.total_equity

    def summary(self) -> dict[str, Any]:
        out = self.book.summary()
        out["live_adapter"] = True
        return out

    # -- live execution + book recording -----------------------------------

    @staticmethod
    def _infer_leg_option_type(intent: TradeIntent, leg: OptionLeg) -> str:
        raw_opt_type = str(intent.metadata.get("option_type", "") or "").upper()
        if raw_opt_type in ("CE", "PE"):
            return raw_opt_type
        sym_up = str(leg.trading_symbol or "").upper()
        if sym_up.endswith("CE"):
            return "CE"
        if sym_up.endswith("PE"):
            return "PE"
        return str(intent.metadata.get("option_type", ""))

    def execute_structure(
        self,
        intent: TradeIntent,
        quote_provider: Any,
        timestamp: datetime | None = None,
    ) -> list[OptionPosition]:
        """Submit ``intent`` to the live venue and book exact fills."""
        check_intent(intent, self.allowed_structures)
        for leg in intent.legs:
            self.book._validate_lot_size(leg)

        ts = ensure_utc(timestamp) or now_utc()
        exec_res = self.trader.execute_structure(
            intent, timestamp=ts, quote_provider=quote_provider
        )

        if exec_res.status == StructureExecStatus.HALTED:
            raise LiveExecutionHaltedError(
                exec_res.error or "Live option entry halted with uncertain outcome",
                result=exec_res,
            )
        if exec_res.status != StructureExecStatus.FILLED or not exec_res.all_filled:
            raise InsufficientMarginError(
                exec_res.error or f"Live option order {exec_res.status.value}"
            )

        structure_id = exec_res.structure_id
        strikes_map = intent.metadata.get("strikes", {})
        default_strike = intent.metadata.get("strike", "0")

        fills_for_fees: list[dict[str, Any]] = []
        positions: list[OptionPosition] = []
        net_cost = ZERO

        for leg_res in exec_res.legs:
            leg = leg_res.leg
            fill_price = Decimal(str(leg_res.fill_price))
            if fill_price <= ZERO:
                raise LiveExecutionHaltedError(
                    f"Venue reported non-positive fill price ({leg_res.fill_price}) "
                    f"for {leg.trading_symbol} — halting for reconciliation",
                    result=exec_res,
                )
            req_px = (
                Decimal(str(leg_res.requested_price))
                if leg_res.requested_price is not None and leg_res.requested_price > 0
                else fill_price
            )
            commission = self.book.commission_per_lot * Decimal(str(leg.quantity))
            leg_cost = fill_price * Decimal(str(leg.total_quantity))
            if leg.side == "BUY":
                net_cost += leg_cost + commission
            else:
                net_cost -= leg_cost - commission

            fills_for_fees.append(
                {
                    "side": leg.side,
                    "quantity": leg.total_quantity,
                    "price": fill_price,
                }
            )

            self.book._position_seq += 1
            position = OptionPosition(
                position_id=f"pos_{ts:%Y%m%d%H%M%S}_{self.book._position_seq:04d}",
                structure_id=structure_id,
                strategy_name=intent.strategy_name,
                instrument_token=leg.instrument_token,
                trading_symbol=leg.trading_symbol,
                underlying=intent.view.underlying,
                option_type=self._infer_leg_option_type(intent, leg),
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
                    "broker_order_id": leg_res.broker_order_id,
                    "order_type": leg_res.order_type,
                    "limit_price": (
                        str(leg_res.limit_price)
                        if leg_res.limit_price is not None
                        else None
                    ),
                },
            )
            self.book._positions[position.position_id] = position
            positions.append(position)
            self.book._fire_leg_fill(
                {
                    "phase": "entry",
                    "structure_id": structure_id,
                    "position_id": position.position_id,
                    "structure_type": intent.structure_type,
                    "underlying": intent.view.underlying,
                    "symbol": leg.trading_symbol or leg.instrument_token,
                    "instrument_token": leg.instrument_token,
                    "side": leg.side,
                    "lots": int(leg.quantity),
                    "lot_size": int(leg.lot_size),
                    "quantity": int(leg.total_quantity),
                    "price": fill_price,
                    "requested_price": req_px,
                    "commission": commission,
                    "broker_order_id": leg_res.broker_order_id,
                    "order_type": leg_res.order_type,
                    "limit_price": leg_res.limit_price,
                    "ts": ts,
                }
            )

        statutory_fees = ZERO
        if self.book.fee_calculator is not None and fills_for_fees:
            try:
                fee_breakdown = self.book.fee_calculator.calculate_structure(
                    fills_for_fees,
                    when=ts,
                )
                statutory_fees = Decimal(str(fee_breakdown.total))
                self.book._statutory_fees_paid += statutory_fees
            except Exception:
                logger.exception(
                    "[live-options] entry statutory fee calculation failed for %s",
                    structure_id[:8],
                )

        structure = StructurePosition(
            structure_id=structure_id,
            structure_type=intent.structure_type,
            strategy_name=intent.strategy_name,
            underlying=intent.view.underlying,
            expiry=intent.expiry,
            legs=positions,
            opened_at=ts,
            statutory_fees=statutory_fees,
        )
        self.book._structures[structure_id] = structure
        self.book.available_cash -= net_cost + statutory_fees
        self.book._fire_structure_opened(structure, statutory_fees)
        return positions

    def close_structure(
        self,
        structure_id: str,
        quote_provider: Any,
        timestamp: datetime | None = None,
        reason: str = "manual",
    ) -> Decimal:
        """Close all open legs at the live venue and settle the local book."""
        structure = self.book._structures.get(structure_id)
        if structure is None:
            raise ValueError(f"Structure {structure_id} not found")

        ts = ensure_utc(timestamp) or now_utc()
        open_legs = [
            pos for pos in structure.legs if pos.status == PositionStatus.OPEN
        ]
        if not open_legs:
            return ZERO

        close_res = self.trader.close_structure(
            structure, quote_provider=quote_provider, timestamp=ts, reason=reason
        )

        total_pnl = ZERO
        closing_fills: list[dict[str, Any]] = []
        remaining_open = list(open_legs)

        # Settle whichever open legs actually filled at the venue first so
        # partial closes keep the local book 1:1 with real exchange exposure,
        # regardless of execution order (e.g. BUY-to-cover short leg first).
        for leg_res in close_res.legs:
            if leg_res.status != LegStatus.FILLED:
                continue
            pos = next(
                (
                    p
                    for p in remaining_open
                    if str(p.instrument_token) == str(leg_res.leg.instrument_token)
                    and ("SELL" if p.is_long else "BUY") == str(leg_res.leg.side).upper()
                ),
                None,
            )
            if pos is None:
                pos = next(
                    (
                        p
                        for p in remaining_open
                        if str(p.trading_symbol) == str(leg_res.leg.trading_symbol)
                    ),
                    None,
                )
            if pos is None:
                continue
            remaining_open.remove(pos)
            exit_price = Decimal(str(leg_res.fill_price))
            if exit_price <= ZERO:
                continue
            exit_side = "SELL" if pos.is_long else "BUY"
            req_px = (
                Decimal(str(leg_res.requested_price))
                if leg_res.requested_price is not None and leg_res.requested_price > 0
                else exit_price
            )
            pnl = pos.close(exit_price, ts)
            total_pnl += pnl

            if pos.is_long:
                self.book.available_cash += exit_price * Decimal(str(pos.total_quantity))
            else:
                self.book.available_cash -= exit_price * Decimal(str(pos.total_quantity))

            closing_fills.append(
                {
                    "side": exit_side,
                    "quantity": pos.total_quantity,
                    "price": exit_price,
                }
            )
            self.book._fire_leg_fill(
                {
                    "phase": "exit",
                    "structure_id": structure.structure_id,
                    "position_id": pos.position_id,
                    "structure_type": structure.structure_type,
                    "underlying": structure.underlying,
                    "symbol": pos.trading_symbol or pos.instrument_token,
                    "instrument_token": pos.instrument_token,
                    "side": exit_side,
                    "lots": int(pos.quantity),
                    "lot_size": int(pos.lot_size),
                    "quantity": int(pos.total_quantity),
                    "price": exit_price,
                    "requested_price": req_px,
                    "commission": pos.commission,
                    "broker_order_id": leg_res.broker_order_id,
                    "order_type": leg_res.order_type,
                    "limit_price": leg_res.limit_price,
                    "ts": ts,
                }
            )

        exit_fees = ZERO
        if self.book.fee_calculator is not None and closing_fills:
            try:
                fee_breakdown = self.book.fee_calculator.calculate_structure(
                    closing_fills,
                    when=ts,
                )
                exit_fees = Decimal(str(fee_breakdown.total))
                self.book._statutory_fees_paid += exit_fees
                self.book.available_cash -= exit_fees
                structure.statutory_fees += exit_fees
            except Exception:
                logger.exception(
                    "[live-options] exit statutory fee calculation failed for %s",
                    structure_id[:8],
                )

        if close_res.status != StructureExecStatus.FILLED or not close_res.all_filled:
            raise LiveExecutionHaltedError(
                close_res.error
                or f"Live option close halted ({close_res.status.value}) — "
                "reconcile remaining open legs against broker",
                result=close_res,
            )

        structure.closed_at = ts
        structure.exit_reason = reason
        structure.session_date = _ist_session_date(ts)
        self.book._fire_structure_closed(structure, total_pnl)
        return total_pnl
