"""Paper runs and the portfolio command center — all on simulator primitives
(ticket P1.4).

This module is the single home for three things that used to live in
``forward/paper.py``, ``forward/broker.py``, ``forward/portfolio.py``,
``forward/runner.py`` and ``forward/order_ledger.py``:

* :class:`PaperRunner` — one bar-replay paper run = one
  :class:`~backtest.simulator.portfolio.Portfolio` + one source + one
  strategy. The engine never branches on the data source: only
  ``source.get_candles()`` differs between synthetic / replay / mstock.
  Its bar-clock loop is the shared
  :func:`~backtest.simulator.engine_loop.run_engine_loop` — the same code
  :class:`~backtest.engine.backtest_driver.BacktestDriver` drives
  (ticket P2.1: backtest and forward are one engine).

* :class:`StrategyRunner` + :class:`OrderLedger` + :class:`PaperBroker` —
  the tick-driven command-center unit (hosted by
  :class:`~backtest.forward.portfolio_manager.PortfolioManager`). Its
  accounting now lives in a :class:`~backtest.simulator.portfolio.Portfolio`
  and every fill goes through a
  :class:`~backtest.simulator.execution.OrderExecutor` — the same single
  fill path the paper run uses (ticket P1.3). The ledger keeps its original
  job only: deterministic ``PRT-{instance}-…`` client-order tagging and
  zero-cross-contamination fill routing.

* :func:`run_walkforward` / :func:`run_live_papertrade` — multi-strategy
  buckets over one symbol, re-expressed on :class:`PaperRunner`.

Fill discipline: a signal computed on bar ``t`` becomes an order while bar
``t`` is the latest known data and trades at bar ``t+1``'s **open** — never
bar ``t``'s close.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

import pandas as pd

from backtest.data.base import CANONICAL_TIMEFRAMES, DataSource
from backtest.data.frame_source import FrameSource
from backtest.data.source_tags import SOURCE_TAG_VALUES, SOURCE_TAGS, source_tag_for
from backtest.data.universe import get_universe_symbols
from backtest.forward.options_bridge import OptionsBridge
from backtest.simulator.engine_loop import OrderQueue, run_engine_loop
from backtest.simulator.enums import OrderSide, OrderType, TimeInForce
from backtest.simulator.execution import OrderExecutor, free_executor
from backtest.simulator.order import Order as SimOrder
from backtest.simulator.portfolio import Portfolio, PortfolioLimits
from backtest.simulator.position_sizing import all_in_size
from backtest.strategy.base import Strategy
from backtest.strategy.registry import get_strategy

__all__ = [
    # paper run
    "OrderQueue",
    "PaperRunner",
    "SOURCE_TAGS",
    # ledger & gateway (ex-order_ledger)
    "OrderRequest",
    "Order",
    "FillEvent",
    "OrderLedger",
    "PaperBroker",
    "ORDER_PENDING",
    "ORDER_FILLED",
    "ORDER_CANCELLED",
    "ORDER_REJECTED",
    "SIDE_BUY",
    "SIDE_SELL",
    # command-center runner (ex-runner)
    "RunnerConfig",
    "StrategyRunner",
    "TARGET_SINGLE",
    "TARGET_POOL",
    "STATUS_RUNNING",
    "STATUS_PAUSED",
    "STATUS_STOPPED",
    "STATUS_ERROR",
    "MAX_BARS_PER_SYMBOL",
    # walk-forward (ex-paper / ex-portfolio)
    "StrategyAccount",
    "StrategyPortfolio",
    "run_walkforward",
    "run_live_papertrade",
    "poll_live_papertrade",
    "save_state",
    "load_state",
]

logger = logging.getLogger("backtest.forward.paper_runner")

# =====================================================================
# Deterministic zero-cost execution (canonical, ticket #6)
# =====================================================================
#
# The zero-cost profile (:data:`backtest.simulator.fees.PAPER_FREE_PROFILE`)
# and the deterministic executor factory
# (:func:`backtest.simulator.execution.free_executor`) live in the simulator
# package — the same primitives the canonical backtest entry uses. V1
# command-center buckets traded without costs (the old PaperBroker filled at
# the supplied price, no slippage, no fees); the executor reproduces that
# exactly so cash and P&L assertions stay meaningful, while realistic costing
# belongs to the live paper run / broker paths.
#
# ``free_executor`` is re-exported here for import compatibility.


# =====================================================================
# Order ledger — client-order tagging & fill routing (ex-order_ledger.py)
# =====================================================================

ORDER_PENDING = "PENDING"
ORDER_FILLED = "FILLED"
ORDER_CANCELLED = "CANCELLED"
ORDER_REJECTED = "REJECTED"

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

#: Manual position-management fields (Live Order Management). Both are PRICE
#: levels: the equity mark for a share position, the signed net premium per
#: unit for an option structure.
RULE_STOP_LOSS = "stop_loss"
RULE_TARGET = "target"
POSITION_RULE_FIELDS = (RULE_STOP_LOSS, RULE_TARGET)

MAX_LEDGER_ORDERS = 100_000  # ring-fence memory in long runs

#: Order-aging thresholds (Phase 3, "order aging alerts"). A working order is
#: normal for a few seconds and a problem after a minute: on a 1-minute
#: validator fleet an order that is still PENDING after a bar has been seen
#: means the venue is not answering, not that it is about to fill. WARN is
#: "look at it", ALERT is "this is stuck — cancel or reconcile".
ORDER_AGING_WARN_S = 60.0
ORDER_AGING_ALERT_S = 300.0

_ORDER_SEQ = itertools.count(1)


class OrderRefused(RuntimeError):
    """A send that did not become a working order (Phase 3).

    Carries the pieces the retry path and the Orders tab need to act on a
    failure instead of just logging it: which ledger row died, why, and
    whether re-sending is SAFE.

    ``retryable`` is a statement about the venue's state, not about the
    error's severity: paper failures are retryable (nothing left the process),
    while a live placement failure is not — a venue error can mean the order
    was accepted and the acknowledgment was lost, and re-sending into that
    ambiguity is how one intent becomes two live orders.
    """

    def __init__(
        self,
        message: str,
        client_order_id: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id
        self.retryable = bool(retryable)
        self.reason = str(message)


@dataclass
class OrderRequest:
    """An order intent emitted by a runner, before tagging/registration."""

    symbol: str
    side: str
    quantity: float
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    tag: Dict = field(default_factory=dict)


@dataclass
class Order:
    client_order_id: str
    instance_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: Optional[float]
    status: str
    created_ts: str
    filled_qty: float = 0.0
    avg_fill_price: Optional[float] = None
    filled_ts: Optional[str] = None
    tag: Dict = field(default_factory=dict)
    #: F-12: venue order id once placed at the live broker (None for paper).
    #: Survives state-file round-trips so a restart re-arms POLLING for the
    #: same venue order instead of double-placing.
    broker_order_id: Optional[str] = None
    # -- Live Order Management (Orders tab) --------------------------------
    #: The price the order was *sent* at (the bar/last price the runner saw).
    #: Fills are compared against it to report real slippage instead of a
    #: guess; None for orders replayed from an earlier process.
    requested_price: Optional[float] = None
    #: Per-unit slippage actually paid, **adverse-positive**: a BUY filled
    #: above the request is positive, a SELL filled below it is positive.
    #: Negative means the fill beat the request (price improvement).
    slippage: Optional[float] = None
    #: ``slippage / requested_price`` — comparable across symbols.
    slippage_pct: Optional[float] = None
    #: Why the order was rejected (ledger-level or broker-level). Set only
    #: alongside ``status == ORDER_REJECTED``.
    reject_reason: Optional[str] = None
    #: Last status transition timestamp (fill/cancel/reject) — lets the UI
    #: show "pending for 42s" instead of a lone creation time.
    updated_ts: Optional[str] = None
    # -- Phase 3: amending a working order --------------------------------
    #: How many times the order was amended while working. An amended order
    #: is still the SAME order — cancel and replace would lose the venue id
    #: and the audit trail of what was originally asked for.
    amend_count: int = 0
    #: The amended quantity/limit the venue now holds (None = as originally
    #: sent). ``requested_price`` deliberately stays the price the runner
    #: DECIDED on, so slippage keeps measuring the fill against the original
    #: instruction and never shifts under the operator's feet.
    amended_quantity: Optional[float] = None
    amended_limit_price: Optional[float] = None
    amended_ts: Optional[str] = None
    #: Retry lineage (Phase 3 auto-retry): the coid of the attempt this order
    #: re-placed, and which attempt it is. Kept in ``tag`` as well so it
    #: survives the ledger's row → dict conversion without a schema change.
    retry_of: Optional[str] = None
    retry_attempt: int = 0


@dataclass
class FillEvent:
    client_order_id: str
    instance_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    ts: str


class OrderLedger:
    """Thread-safe order tagging & fill-routing ledger.

    Every order is tagged with a deterministic ``PRT-{instance}-…`` client
    order id; fills are routed strictly back to the owning runner's fill
    handler — zero cross-contamination between 50+ concurrent runners.
    Runners also register themselves (:meth:`register_runner`) so the
    :class:`PaperBroker` can route execution to their
    :class:`~backtest.simulator.execution.OrderExecutor`.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._routing: Dict[str, str] = {}  # client_order_id -> instance_id
        self._orders: Dict[str, Order] = {}
        self._order_history: deque = deque()
        self._handlers: Dict[str, Callable[[FillEvent], None]] = {}
        self._runners: Dict[str, Any] = {}  # instance_id -> StrategyRunner
        self._pending_fills: Dict[str, Deque[FillEvent]] = defaultdict(deque)
        self._fill_count = 0

    # -- registration ------------------------------------------------------

    def register_handler(self, instance_id: str, handler: Callable[[FillEvent], None]) -> None:
        with self._lock:
            self._handlers[instance_id] = handler

    def register_runner(self, instance_id: str, runner: Any) -> None:
        """Bind the runner (and its executor) to an instance id."""
        with self._lock:
            self._runners[instance_id] = runner

    def unregister_handler(self, instance_id: str) -> None:
        with self._lock:
            self._handlers.pop(instance_id, None)
            self._runners.pop(instance_id, None)

    def runner_for(self, instance_id: str) -> Optional[Any]:
        with self._lock:
            return self._runners.get(instance_id)

    # -- orders -------------------------------------------------------------

    def submit(self, instance_id: str, request: OrderRequest) -> Order:
        """Tag and register an outgoing order. Returns the tagged :class:`Order`."""
        if request.quantity <= 0:
            raise ValueError(f"order quantity must be positive, got {request.quantity}")
        if request.side not in (SIDE_BUY, SIDE_SELL):
            raise ValueError(f"order side must be BUY or SELL, got {request.side}")

        coid = self._make_client_order_id(instance_id)
        order = Order(
            client_order_id=coid,
            instance_id=instance_id,
            symbol=str(request.symbol).upper(),
            side=request.side,
            quantity=float(request.quantity),
            order_type=request.order_type,
            limit_price=request.limit_price,
            status=ORDER_PENDING,
            created_ts=datetime.now(timezone.utc).isoformat(),
            tag=dict(request.tag),
        )
        with self._lock:
            self._routing[coid] = instance_id
            self._orders[coid] = order
            self._order_history.append(coid)
            self._trim_locked()
        return order

    def cancel(self, client_order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(client_order_id)
            if order is None or order.status != ORDER_PENDING:
                return False
            order.status = ORDER_CANCELLED
            order.updated_ts = datetime.now(timezone.utc).isoformat()
            return True

    def reject(self, client_order_id: str, reason: str) -> bool:
        """Mark a pending order rejected with the broker's reason.

        A rejected order is a *terminal* state the Orders tab must be able to
        show — an order that never filled used to stay PENDING forever, which
        reads as "still working" and hides the failure.
        """
        with self._lock:
            order = self._orders.get(client_order_id)
            if order is None or order.status != ORDER_PENDING:
                return False
            order.status = ORDER_REJECTED
            order.reject_reason = str(reason or "rejected")
            order.updated_ts = datetime.now(timezone.utc).isoformat()
            return True

    def amend(
        self,
        client_order_id: str,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> Order:
        """Amend a PENDING order in place; returns the updated order.

        An amend is not a cancel-and-replace: the venue order id, the client
        order id and the record of what was originally asked for all survive,
        so "what did I send, and what did I change it to?" stays answerable.

        ``requested_price`` is deliberately NOT rewritten — slippage keeps
        measuring the fill against the price the runner decided on. If the
        operator's amend is what should be measured, that is a different
        question ("did my amendment help?"), not this one.

        Raises ``KeyError`` for an unknown id and ``ValueError`` when the order
        is already terminal (a filled order is history; amending it would be a
        lie about what happened).
        """
        with self._lock:
            order = self._orders.get(client_order_id)
            if order is None:
                raise KeyError(f"unknown order: {client_order_id}")
            if order.status != ORDER_PENDING:
                raise ValueError(
                    f"order {client_order_id} is {order.status} — only a working "
                    "order can be amended"
                )
            if quantity is None and limit_price is None:
                raise ValueError("amend needs a new quantity and/or limit price")
            if quantity is not None:
                new_qty = float(quantity)
                if new_qty <= 0:
                    raise ValueError(f"amended quantity must be positive, got {new_qty!r}")
                order.quantity = new_qty
                order.amended_quantity = new_qty
            if limit_price is not None:
                new_price = float(limit_price)
                if new_price <= 0:
                    raise ValueError(f"amended limit price must be positive, got {new_price!r}")
                order.limit_price = new_price
                order.amended_limit_price = new_price
            order.amend_count += 1
            order.amended_ts = datetime.now(timezone.utc).isoformat()
            order.updated_ts = order.amended_ts
            return order

    def apply_fill(
        self,
        client_order_id: str,
        price: float,
        quantity: Optional[float] = None,
        ts: Optional[str] = None,
    ) -> FillEvent:
        """Record a broker fill and route it to the owning runner.

        Raises ``KeyError`` if the client order id is unknown — an unknown
        order id must never silently fill.
        """
        with self._lock:
            instance_id = self._routing.get(client_order_id)
            if instance_id is None:
                raise KeyError(f"unknown client_order_id: {client_order_id}")
            order = self._orders[client_order_id]

            qty = float(quantity) if quantity is not None else order.quantity
            fill = FillEvent(
                client_order_id=client_order_id,
                instance_id=instance_id,
                symbol=order.symbol,
                side=order.side,
                quantity=qty,
                price=float(price),
                ts=ts or datetime.now(timezone.utc).isoformat(),
            )

            order.status = ORDER_FILLED
            order.filled_qty = qty
            order.avg_fill_price = fill.price
            order.filled_ts = fill.ts
            order.updated_ts = fill.ts
            # Slippage is measured against the price the order was SENT at.
            # Recorded adverse-positive so "+₹2.50/unit" always means "cost me
            # money" regardless of side (a SELL filled low is as bad as a BUY
            # filled high).
            if order.requested_price:
                diff = fill.price - float(order.requested_price)
                adverse = diff if order.side == SIDE_BUY else -diff
                # Collapse -0.0 (and float dust) to a clean 0.0 — "-₹0.00
                # slippage" is noise in a table, not information.
                adverse = 0.0 if abs(adverse) < 1e-12 else adverse
                order.slippage = round(adverse, 6)
                order.slippage_pct = round(adverse / float(order.requested_price), 8)

            self._fill_count += 1
            self._pending_fills[instance_id].append(fill)
            handler = self._handlers.get(instance_id)

        # Dispatch outside the lock so runner accounting can call back in.
        if handler is not None:
            handler(fill)
        return fill

    def drain_pending_fills(self, instance_id: str) -> List[FillEvent]:
        with self._lock:
            pending = self._pending_fills.get(instance_id)
            if not pending:
                return []
            drained = list(pending)
            pending.clear()
            return drained

    # -- lookups -----------------------------------------------------------

    def get_order(self, client_order_id: str) -> Optional[Order]:
        with self._lock:
            return self._orders.get(client_order_id)

    def owner_of(self, client_order_id: str) -> Optional[str]:
        with self._lock:
            return self._routing.get(client_order_id)

    def reattach_order(
        self,
        instance_id: str,
        client_order_id: str,
        spec: Dict[str, Any],
        broker_order_id: Optional[str] = None,
    ) -> Order:
        """Re-tag an order from a previous process (live gateway restore).

        The original PENDING order died with the old process; the venue
        order it became did NOT. This recreates the ledger row so the
        incoming fill has somewhere to route — the venue order is NEVER
        re-placed.
        """
        order = Order(
            client_order_id=client_order_id,
            instance_id=instance_id,
            symbol=str(spec.get("symbol", "")).upper(),
            side=str(spec.get("side", "BUY")),
            quantity=float(spec.get("quantity", 0.0)),
            order_type="MARKET",
            limit_price=None,
            status=ORDER_PENDING,
            created_ts=str(spec.get("created_ts") or datetime.now(timezone.utc).isoformat()),
            tag=dict(spec.get("tag") or {}),
            broker_order_id=broker_order_id,
        )
        with self._lock:
            self._routing[client_order_id] = instance_id
            self._orders[client_order_id] = order
            self._order_history.append(client_order_id)
            self._trim_locked()
        return order

    def orders_for(self, instance_id: str) -> List[Order]:
        with self._lock:
            return [o for o in self._orders.values() if o.instance_id == instance_id]

    # -- read surface (Orders tab) -----------------------------------------

    @staticmethod
    def order_to_dict(order: Order) -> Dict[str, Any]:
        """One order as a JSON-safe row (the Orders tab's only shape)."""
        return {
            "client_order_id": order.client_order_id,
            "instance_id": order.instance_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "order_type": order.order_type,
            "limit_price": order.limit_price,
            "status": order.status,
            "created_ts": order.created_ts,
            "updated_ts": order.updated_ts or order.filled_ts or order.created_ts,
            "filled_qty": order.filled_qty,
            "avg_fill_price": order.avg_fill_price,
            "filled_ts": order.filled_ts,
            "requested_price": order.requested_price,
            "slippage": order.slippage,
            "slippage_pct": order.slippage_pct,
            "reject_reason": order.reject_reason,
            "tag": dict(order.tag),
            "broker_order_id": order.broker_order_id,
            "cancellable": order.status == ORDER_PENDING,
            # Phase 3: only a LIVE working order can be amended — a paper order
            # fills or rejects synchronously, so there is nothing resting at a
            # venue to amend. The server decides this, not the UI.
            "modifiable": order.status == ORDER_PENDING and order.broker_order_id is not None,
            "amend_count": order.amend_count,
            "amended_quantity": order.amended_quantity,
            "amended_limit_price": order.amended_limit_price,
            "amended_ts": order.amended_ts,
            "retry_of": order.retry_of,
            "retry_attempt": order.retry_attempt,
        }

    @staticmethod
    def aging_band(age_s: float, pending: bool = True) -> str:
        """``"warn"`` / ``"alert"`` / ``""`` for a working order's age."""
        if not pending:
            return ""
        if age_s >= ORDER_AGING_ALERT_S:
            return "alert"
        if age_s >= ORDER_AGING_WARN_S:
            return "warn"
        return ""

    def row_with_age(self, order: Order) -> Dict[str, Any]:
        """One ledger row plus its derived age band (the Orders tab's row).

        Computed here rather than in the browser so the row the page renders
        and the row the manager alerts on can never disagree about whether an
        order is stuck.
        """
        row = self.order_to_dict(order)
        age = self.age_seconds(order.created_ts) if order.status == ORDER_PENDING else 0.0
        row["age_s"] = round(age, 1) if order.status == ORDER_PENDING else None
        row["aging"] = self.aging_band(age, order.status == ORDER_PENDING)
        return row

    def snapshot(
        self,
        instance_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Newest-first order rows, optionally filtered.

        ``statuses`` matches on exact status names (case-insensitive); an
        empty list means "no filter" rather than "nothing", so a UI that
        sends no status selection still gets the whole ledger.
        """
        wanted = {str(s).upper() for s in (statuses or []) if s}
        with self._lock:
            orders = [
                o for o in self._orders.values()
                if instance_id is None or o.instance_id == instance_id
            ]
            orders = [o for o in orders if not wanted or o.status.upper() in wanted]
            orders.sort(key=lambda o: (o.created_ts, o.client_order_id), reverse=True)
            rows = [self.row_with_age(o) for o in orders[: max(0, int(limit))]]
        return rows

    def summary(self, instance_id: Optional[str] = None) -> Dict[str, Any]:
        """Counts + slippage statistics for the Orders header strip."""
        with self._lock:
            orders = [
                o for o in self._orders.values()
                if instance_id is None or o.instance_id == instance_id
            ]
            counts: Dict[str, int] = {
                ORDER_PENDING: 0,
                ORDER_FILLED: 0,
                ORDER_CANCELLED: 0,
                ORDER_REJECTED: 0,
            }
            for order in orders:
                counts[order.status] = counts.get(order.status, 0) + 1
            slips = [o.slippage for o in orders if o.slippage is not None]
            slip_pcts = [o.slippage_pct for o in orders if o.slippage_pct is not None]
            pending = [o for o in orders if o.status == ORDER_PENDING]
            pending_age = [self.age_seconds(o.created_ts) for o in pending]
            oldest = max(pending, key=lambda o: self.age_seconds(o.created_ts), default=None)
            bands = {coid: 0 for coid in ("warn", "alert")}
            for order in pending:
                band = self.aging_band(self.age_seconds(order.created_ts))
                if band:
                    bands[band] += 1
        return {
            "total": len(orders),
            "pending": counts.get(ORDER_PENDING, 0),
            "filled": counts.get(ORDER_FILLED, 0),
            "cancelled": counts.get(ORDER_CANCELLED, 0),
            "rejected": counts.get(ORDER_REJECTED, 0),
            "status_counts": counts,
            "fills": counts.get(ORDER_FILLED, 0),
            "avg_slippage": round(sum(slips) / len(slips), 6) if slips else 0.0,
            "avg_slippage_pct": round(sum(slip_pcts) / len(slip_pcts), 8) if slip_pcts else 0.0,
            "worst_slippage": round(max(slips), 6) if slips else 0.0,
            "slippage_samples": len(slips),
            "oldest_pending_age_s": round(max(pending_age), 1) if pending_age else 0.0,
            # Order aging (Phase 3): the thresholds travel with the data so the
            # strip, the rows and the manager's alerts share one definition of
            # "stuck" instead of three approximations of it.
            "aging_warn_s": ORDER_AGING_WARN_S,
            "aging_alert_s": ORDER_AGING_ALERT_S,
            "aging_warn_count": bands["warn"],
            "aging_alert_count": bands["alert"],
            "oldest_pending_coid": oldest.client_order_id if oldest is not None else None,
            "oldest_pending_symbol": oldest.symbol if oldest is not None else None,
        }

    @staticmethod
    def age_seconds(created_ts: Optional[str]) -> float:
        """Seconds since ``created_ts``; 0.0 when it cannot be parsed."""
        if not created_ts:
            return 0.0
        try:
            created = datetime.fromisoformat(str(created_ts).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())

    @property
    def fill_count(self) -> int:
        with self._lock:
            return self._fill_count

    @property
    def order_count(self) -> int:
        with self._lock:
            return len(self._orders)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _make_client_order_id(instance_id: str) -> str:
        """``PRT-{instance_id}-{timestamp_ms}-{counter}`` (Task 4.1 schema)."""
        ts_ms = int(time.time() * 1000)
        return f"PRT-{instance_id[:8]}-{ts_ms}-{next(_ORDER_SEQ)}"

    def _trim_locked(self) -> None:
        while len(self._order_history) > MAX_LEDGER_ORDERS:
            old = self._order_history.popleft()
            self._orders.pop(old, None)
            self._routing.pop(old, None)


class PaperBroker:
    """Simulated execution gateway for command-center buckets.

    Market orders fill immediately at the supplied price, routed through
    the owning runner's :class:`~backtest.simulator.execution.OrderExecutor`
    (zero-cost profile — see :func:`free_executor`). Every order is tagged
    by the ledger and every fill is dispatched back through it, exactly as
    a live gateway would.
    """

    def __init__(self, ledger: OrderLedger, slippage_pct: float = 0.0) -> None:
        self.ledger = ledger
        self.slippage_pct = float(slippage_pct)

    def submit_market(
        self,
        instance_id: str,
        symbol: str,
        side: str,
        quantity: float,
        fill_price: float,
        ts: Optional[str] = None,
        tag: Optional[Dict] = None,
    ) -> FillEvent:
        order = self.ledger.submit(
            instance_id,
            OrderRequest(symbol=symbol, side=side, quantity=quantity, tag=tag or {}),
        )
        # Live Order Management: remember the price the order was SENT at so
        # the fill's slippage is a measurement, not a guess.
        order.requested_price = float(fill_price)
        runner = self.ledger.runner_for(instance_id)
        if runner is None:
            # Ledger-level caller (no runner bound): record the fill at the
            # supplied price and route it through the ledger.
            return self.ledger.apply_fill(
                order.client_order_id, float(fill_price), float(quantity), ts=ts
            )

        slip = 1.0 + (self.slippage_pct if side == SIDE_BUY else -self.slippage_pct)
        price = float(fill_price) * slip

        sim_order = SimOrder(
            symbol=str(symbol).strip().upper(),
            side=OrderSide.BUY if side == SIDE_BUY else OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            portfolio_id=runner.portfolio.portfolio_id,
            strategy_name=runner.config.strategy_name,
            client_order_id=order.client_order_id,
        )
        # Everything from here on can refuse: a bad ticket, a frozen book, an
        # executor that will not fill. The ledger row was already written, so
        # each failure mode MUST terminate it — an order that never reached the
        # venue and never got refused shows up as "still working" forever.
        try:
            sim_order.validate()
            sim_order.submit()
            result = runner.executor.execute(
                sim_order, {"bid": price, "ask": price, "last": price}
            )
        except Exception as exc:  # noqa: BLE001 — every refusal is a refusal
            reason = (
                f"paper order refused before the venue: "
                f"{exc.__class__.__name__}: {exc}"
            )
            self.ledger.reject(order.client_order_id, reason)
            raise OrderRefused(
                reason,
                client_order_id=order.client_order_id,
                # Nothing left this process, and the ticket is re-derivable
                # from the runner's own state — a bounded retry is safe.
                retryable=True,
            ) from exc

        fill = result.fill
        if fill is None:
            # Terminal, not working: the venue (here, the executor) refused.
            # Leaving it PENDING would show as a live order in the Orders tab
            # forever, hiding the failure that the operator needs to retry.
            reason = f"paper fill did not execute: {result.status} — {result.reason}"
            self.ledger.reject(order.client_order_id, reason)
            raise OrderRefused(reason, client_order_id=order.client_order_id, retryable=True)
        runner.portfolio.add_order(sim_order)

        return self.ledger.apply_fill(
            order.client_order_id, float(fill.fill_price), float(fill.quantity), ts=ts
        )


# =====================================================================
# RunnerConfig + StrategyRunner (ex-runner.py, re-architected)
# =====================================================================

TARGET_SINGLE = "SINGLE_SYMBOL"
TARGET_POOL = "SYMBOL_UNIVERSE"

#: Portfolio bucket modes (ticket P4.1): paper = simulated fills,
#: live = broker execution (wired in F-12).
VALID_INSTANCE_MODES = ("paper", "live")

STATUS_RUNNING = "RUNNING"
STATUS_PAUSED = "PAUSED"
STATUS_STOPPED = "STOPPED"
STATUS_ERROR = "ERROR"

MAX_BARS_PER_SYMBOL = 500  # Task 5: light rolling buffers
MAX_SIGNAL_LOG = 200
MAX_TRADE_LOG = 200
MAX_EQUITY_POINTS = 500
MIN_WARMUP_BARS = 12  # strategies need history to compute
#: Every Nth bar while an option structure is open, the runner records an
#: ``OPTION_MTM`` heartbeat (task A1) — a chartable mark of the book's
#: unrealized P&L without flooding the signal log.
OPTION_MTM_LOG_EVERY = 5


@dataclass
class OrderRetryPolicy:
    """Auto-retry for an order the venue refused (Phase 3) — OFF by default.

    Why this exists: a strategy's signal is *transition-based* (it fires on
    ``0→1``, not on every bar). If that one order is refused, the position
    never opens even though the reason for opening has not changed — the
    runner just sits flat, and nothing in the P&L explains why. A bounded
    retry is what turns "the strategy decided" into "the book followed".

    Why it is bounded and off by default: a retry loop that never gives up is
    a way to discover a risk limit N times. Every attempt is a fresh order
    with a fresh client order id, all audited, and the lineage (``retry_of``,
    ``retry_attempt``) rides on the order's tag so the Orders tab shows what
    happened instead of N mysterious duplicates.

    ``max_attempts`` counts retries AFTER the first failure: 2 means up to
    three sends in total. ``cooldown_s`` is a floor between attempts (bars
    arrive at whatever cadence the feed runs at, so a retry is only pumped
    when the cooldown has genuinely elapsed).
    """

    max_attempts: int = 0
    cooldown_s: float = 5.0
    #: How long a SPENT budget stays spent before a new episode may start.
    #: Without this, a venue outage that outlasts one episode strands the
    #: runner flat forever whenever the strategy's signal never flips (a
    #: buy-and-hold entry never "changes its mind"). With it, the runner keeps
    #: trying at a slow, bounded, audited cadence instead of either giving up
    #: permanently or hammering the venue.
    rearm_after_s: float = 300.0
    #: Which order kinds may be retried. An exit is a de-risking action, and a
    #: refused exit is the one case where retrying matters most; entries are
    #: included for the transition-signal reason above.
    kinds: tuple = ("entry", "exit")

    def allows(self, kind: str) -> bool:
        return self.max_attempts > 0 and str(kind) in self.kinds


@dataclass
class RunnerConfig:
    """Spawn configuration for a runner (validated on construction)."""

    name: str
    strategy_name: str
    allocated_capital: float
    target_type: str = TARGET_SINGLE
    symbols: Optional[List[str]] = None
    universe_id: Optional[str] = None
    timeframe: str = "1hour"
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    max_pool_positions: int = 5
    position_pct: Optional[float] = None  # fraction of bucket per entry
    instance_id: Optional[str] = None
    # Instance-level circuit breakers (fraction of allocation)
    max_drawdown_pct: float = 0.25
    daily_loss_limit_pct: float = 0.15
    allow_short: bool = False
    # Bucket classification for the portfolio UI (ticket P4.1):
    # 'paper' (simulated fills) or 'live' (broker execution — F-12 wiring).
    mode: str = "paper"
    # Canonical P1.1 source tag: synthetic / replay / mstock.
    source: str = "synthetic"
    # Instrument config (Gap G3.2): {"type": "equity"} keeps the classic
    # flow; {"type": "option", "expression": {...}} routes bars through the
    # options expression layer via an OptionsBridge.
    instrument: Dict[str, Any] = field(default_factory=lambda: {"type": "equity"})
    # U3.4: playbook snapshot — running runners never mutate on playbook edit
    playbook_id: Optional[str] = None
    playbook_version: Optional[int] = None
    playbook_snapshot: Optional[Dict[str, Any]] = None
    # Phase 3: bounded auto-retry for a refused order (None/0 attempts = off).
    retry_policy: Optional[OrderRetryPolicy] = None

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("runner name required")
        self.target_type = str(self.target_type).upper()
        if self.target_type not in (TARGET_SINGLE, TARGET_POOL):
            raise ValueError(f"target_type must be {TARGET_SINGLE} or {TARGET_POOL}")
        if self.allocated_capital <= 0:
            raise ValueError("allocated_capital must be positive")
        self.timeframe = str(self.timeframe).strip().lower() or "1hour"
        if self.timeframe not in CANONICAL_TIMEFRAMES:
            raise ValueError(
                f"timeframe must be one of {CANONICAL_TIMEFRAMES}, got {self.timeframe!r}"
            )

        # Bucket classification (ticket P4.1)
        self.mode = str(self.mode).strip().lower()
        if self.mode not in VALID_INSTANCE_MODES:
            raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {self.mode!r}")
        self.source = str(self.source).strip().lower()
        if self.source not in SOURCE_TAG_VALUES:
            raise ValueError(
                f"source must be one of {sorted(SOURCE_TAG_VALUES)}, got {self.source!r}"
            )

        # Resolve symbols
        if self.universe_id:
            resolved = get_universe_symbols(self.universe_id)
            self.symbols = resolved
            self.target_type = TARGET_POOL
        elif self.symbols:
            self.symbols = [str(s).upper() for s in self.symbols]
        else:
            raise ValueError("runner needs symbols or a universe_id")

        if self.target_type == TARGET_SINGLE and len(self.symbols) != 1:
            # Tolerate a 1-element list; reject larger lists for single mode.
            if len(self.symbols) > 1:
                raise ValueError("SINGLE_SYMBOL runners target exactly one symbol")

        if self.max_pool_positions < 1:
            raise ValueError("max_pool_positions must be >= 1")

        # Instrument config (Gap G3.2)
        if self.instrument is None:
            self.instrument = {"type": "equity"}
        if not isinstance(self.instrument, dict):
            raise ValueError("instrument must be a dict, e.g. {'type': 'equity'|'option'}")
        inst_type = str(self.instrument.get("type", "equity")).strip().lower()
        if inst_type not in ("equity", "option"):
            raise ValueError(
                f"instrument.type must be 'equity' or 'option', got {inst_type!r}"
            )
        self.instrument["type"] = inst_type


class StrategyRunner:
    """Isolated strategy execution worker (Layer 1 of the portfolio engine).

    Accounting lives in a :class:`~backtest.simulator.portfolio.Portfolio`
    and every fill is executed by the runner's own
    :class:`~backtest.simulator.execution.OrderExecutor` (zero-cost V1
    profile). The public surface (state machine, candle processing, pool
    scans, metrics, state snapshots) is unchanged from the original
    runner, so :class:`~backtest.forward.portfolio_manager.PortfolioManager`
    and the dashboard API keep working untouched.
    """

    def __init__(
        self,
        config: RunnerConfig,
        ledger: OrderLedger,
        broker: Optional[PaperBroker] = None,
        strategy: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.instance_id: str = config.instance_id or uuid.uuid4().hex
        self.ledger = ledger
        self.broker = broker or PaperBroker(ledger)

        # 2026-09-24: for option runners, the strategy's free-text
        # ``underlying`` param is FORCED to the runner symbol BEFORE the
        # strategy instance is built — the options bridge prices the chain
        # off the view's underlying, so a spawn-form mismatch (Instrument=
        # BANKNIFTY, Underlying param=NIFTY) would book BANKNIFTY positions
        # priced off the NIFTY chain. The runner symbol is the single source
        # of truth; the param is just a label.
        if (
            str(config.instrument.get("type", "equity")) == "option"
            and config.symbols
            and "underlying" in getattr(get_strategy(config.strategy_name), "params", {})
        ):
            try:
                config.strategy_params["underlying"] = str(config.symbols[0]).upper()
            except (AttributeError, TypeError):
                pass

        # Strategy instance (isolated per runner so indicator state never leaks)
        if strategy is not None:
            self.strategy = strategy
        else:
            self.strategy = get_strategy(config.strategy_name)(**config.strategy_params)

        # -- isolated accounting (simulator portfolio, Decimal-exact) ------
        self.portfolio = Portfolio(
            name=config.name,
            initial_capital=config.allocated_capital,
            limits=PortfolioLimits(allow_short=config.allow_short),
            mode="paper",
            source="synthetic",
        )
        self.executor = free_executor(self.portfolio)
        self.closed_trades_cache: List[Dict[str, Any]] = []

        # -- options bridge (Gap G3.2) --------------------------------------
        # Runners with instrument.type == "option" route bars through the
        # expression layer instead of the equity signal flow.
        # U6.2: the chain generator is the SHARED one (one per underlying via
        # the ChainBus) so two NIFTY runners price identical chains off the
        # same spot. The quote provider stays per-runner (cheap, per-book
        # contract registry + pricing clock).
        self.options_bridge: Optional["OptionsBridge"] = None
        self._chain_released = True  # U6.2 guard; flipped when a bridge takes its subscription
        self._chain_source = "synthetic"  # P1.1: release must match the acquire source
        if str(config.instrument.get("type", "equity")) == "option":
            from backtest.forward.feed_registry import option_quote_provider_for

            self._chain_underlying = config.symbols[0] if config.symbols else "NIFTY"
            # P1.1: source routes the chain — mstock/dhan + authenticated
            # session → the shared LiveChainProvider (real chains + LTP, one
            # API budget per underlying); anything else → the synthetic pair.
            # The fallback is deliberate and labelled ("synthetic:bs") so a
            # synthetic-priced runner can never pass as live.
            provider, quote_label = option_quote_provider_for(
                config.source, self._chain_underlying
            )
            # The bus STORE the acquire landed in (synthetic|mstock|dhan) —
            # derived from the resolved provider, not from config.source: a
            # requested "mstock"/"dhan" that fell back to synthetic must
            # release the synthetic entry, or stop() leaks the refcount.
            live_label = f"live:{config.source.lower()}"
            self._chain_source = (
                str(config.source).lower() if quote_label == live_label else "synthetic"
            )
            self._chain_released = False  # one release per acquire (stop is idempotent)
            self.options_bridge = OptionsBridge(
                capital=config.allocated_capital,
                expression=config.instrument.get("expression"),
                quote_provider=provider,
            )

        # -- rolling candle buffers ----------------------------------------
        self._bars: Dict[str, Deque[Dict[str, Any]]] = {
            sym: deque(maxlen=MAX_BARS_PER_SYMBOL) for sym in config.symbols
        }
        self._last_bar_ts: Dict[str, str] = {}

        # -- metrics ---------------------------------------------------------
        self.equity_curve: List[Dict[str, Any]] = []
        self.peak_equity: float = float(config.allocated_capital)
        self.max_drawdown_pct: float = 0.0
        self._day_start_equity: float = float(config.allocated_capital)
        self._current_day: Optional[str] = None
        self.last_price: Dict[str, float] = {}
        #: Most recent option-book MTM (0.0 for equity runners) — task A1.
        self.last_option_pnl: float = 0.0
        self.signal_log: Deque[Dict[str, Any]] = deque(maxlen=MAX_SIGNAL_LOG)
        #: PnL-vs-spot watch series (2026-09-24): one row per closed bar while
        #: an option structure is open — feeds the end-of-day chart export
        #: (watch_export.py). Live (mstock) runners only; synthetic books are
        #: excluded per the owner's "synthetic stuff must vanish" rule.
        self.watch_series: List[Dict[str, Any]] = []
        self.last_option_label: Optional[str] = None

        # -- state -----------------------------------------------------------
        self.status: str = STATUS_STOPPED
        self.error: Optional[str] = None
        self.bars_processed: int = 0
        self.created_ts: str = datetime.now(timezone.utc).isoformat()

        # -- operator-set levels (Live Order Management) --------------------
        # ``{position_key: {"stop_loss": float|None, "target": float|None}}``
        # for EQUITY positions (key = symbol). Option structures keep their
        # equivalent levels on the bridge (key = structure_id) because the
        # bridge owns the per-bar mark that arms them; ``position_rules_view``
        # merges both so the dashboard sees one shape.
        self.position_rules: Dict[str, Dict[str, Optional[float]]] = {}

        # -- order auto-retry (Phase 3) --------------------------------------
        # ``{symbol: {side, qty, kind, attempts, last_ts, retry_of, reason}}``
        # — the most recent refused intent per symbol. One pending retry per
        # symbol on purpose: the strategy's LATEST intent is the one worth
        # re-sending, and a queue of stale ones would trade a view the
        # strategy has already left.
        self._retries: Dict[str, Dict[str, Any]] = {}
        #: ``{symbol: {"side", "kind"}}`` — intents whose retry budget was spent.
        #: A transition signal keeps firing while its condition holds, so
        #: without this the very next bar would open a fresh budget and the
        #: "bounded" policy would retry forever. Cleared as soon as the
        #: strategy stops asking for that action (a genuinely new decision).
        self._retry_blocked: Dict[str, Dict[str, Any]] = {}
        self.retries_raised: int = 0
        self.retries_recovered: int = 0
        self.retries_exhausted: int = 0

        self._lock = threading.RLock()
        self.ledger.register_handler(self.instance_id, self.on_fill)
        self.ledger.register_runner(self.instance_id, self)

    # -- accounting views (over the simulator portfolio) --------------------

    @property
    def cash(self) -> float:
        return float(self.portfolio.current_cash)

    @property
    def realized_pnl(self) -> float:
        return float(self.portfolio.realized_pnl) + self._option_realized_pnl()

    @property
    def positions(self) -> Dict[str, Dict[str, Any]]:
        """Open positions as the legacy dict view (``qty`` is unsigned)."""
        out: Dict[str, Dict[str, Any]] = {}
        for sym, pos in self.portfolio.positions.items():
            out[sym] = {
                "symbol": sym,
                "side": "LONG" if pos.quantity > 0 else "SHORT",
                "qty": abs(float(pos.quantity)),
                "entry_price": float(pos.average_entry_price),
                "entry_ts": pos.opened_at.isoformat() if pos.opened_at else None,
                "coid": None,
            }
        return out

    @property
    def closed_option_trades(self) -> List[Dict[str, Any]]:
        """Closed option structures as trade records (task B3).

        One record per structure, shaped like the equity trade records so the
        deep-dive table and the win-rate metrics can consume both without
        branching on instrument type:

        * ``symbol`` — underlying + structure (e.g. ``NIFTY bull_call_spread``)
        * ``qty`` — lots per leg, ``units`` — contracts per leg
        * ``entry_price`` / ``exit_price`` — **net premium per unit** (a
          positive entry is a debit spread, negative a credit structure)
        * ``pnl`` (gross of costs) / ``commission`` (the fee stack's
          per-structure part; statutory fees are tracked per book, not per
          structure), ``win``, ``exit_reason``, ``kind="option"``
        """
        bridge = self.options_bridge
        if bridge is None:
            return []
        records: List[Dict[str, Any]] = []
        for structure in bridge.option_broker.get_closed_structures():
            records.append(self._option_trade_record(structure))
        return records

    @staticmethod
    def _option_trade_record(structure: Any) -> Dict[str, Any]:
        """Flatten a closed ``StructurePosition`` into a trade-log row."""
        legs = list(structure.legs)
        units = max((leg.total_quantity for leg in legs), default=0)
        lots = max((leg.quantity for leg in legs), default=0)

        def _net_premium(price_attr: str) -> float:
            """Signed premium per unit: longs pay, shorts receive."""
            total = Decimal("0")
            for leg in legs:
                price = getattr(leg, price_attr, Decimal("0")) or Decimal("0")
                total += price * Decimal(str(leg.total_quantity)) * (
                    1 if leg.is_long else -1
                )
            return float(total / Decimal(str(units))) if units else 0.0

        strikes = sorted({str(leg.strike) for leg in legs})
        pnl = float(structure.total_realized_pnl)
        return {
            "symbol": f"{structure.underlying} {structure.structure_type}",
            "label": (
                f"{structure.underlying} {structure.structure_type} "
                f"{'/'.join(strikes)} {legs[0].option_type if legs else ''}".strip()
            ),
            "kind": "option",
            "structure_type": structure.structure_type,
            "underlying": structure.underlying,
            "strikes": strikes,
            "expiry": structure.expiry.isoformat() if structure.expiry else None,
            "legs": len(legs),
            "side": "LONG" if legs and legs[0].is_long else "SHORT",
            "qty": lots,
            "units": units,
            "entry_price": round(_net_premium("entry_price"), 2),
            "exit_price": round(_net_premium("current_price"), 2),
            "entry_ts": structure.opened_at.isoformat() if structure.opened_at else None,
            "exit_ts": structure.closed_at.isoformat() if structure.closed_at else None,
            "pnl": round(pnl, 2),
            "commission": round(float(structure.total_commission), 2),
            "win": pnl >= 0,
            "exit_reason": structure.exit_reason,
            "coid": None,
            "exit_coid": None,
        }

    @property
    def closed_trades(self) -> List[Dict[str, Any]]:
        """Closed round-trips: equity positions **and** option structures (B3).

        Sorted by exit time so a mixed book reads chronologically. Records
        from the classic equity flow are unchanged except for the added
        ``kind: "equity"`` tag.
        """
        trades: List[Dict[str, Any]] = []
        for pos in self.portfolio.closed_positions:
            pnl = float(pos.realized_pnl)
            trades.append(
                {
                    "symbol": pos.symbol,
                    "kind": "equity",
                    "side": "LONG" if pos.quantity >= 0 else "SHORT",
                    "qty": abs(float(pos.quantity)),
                    "entry_price": float(pos.average_entry_price),
                    "exit_price": float(
                        pos.current_price
                        if pos.current_price is not None
                        else pos.average_entry_price
                    ),
                    "entry_ts": pos.opened_at.isoformat() if pos.opened_at else None,
                    "exit_ts": pos.closed_at.isoformat() if pos.closed_at else None,
                    "pnl": round(pnl, 2),
                    "win": pnl >= 0,
                    "coid": None,
                    "exit_coid": None,
                }
            )
        trades.extend(self.closed_option_trades)
        trades.sort(key=lambda t: (t.get("exit_ts") or "", t.get("symbol") or ""))
        return trades[-MAX_TRADE_LOG:]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.closed_trades if t["win"])

    @property
    def losses(self) -> int:
        return sum(1 for t in self.closed_trades if not t["win"])

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        with self._lock:
            if self.status == STATUS_RUNNING:
                return
            if self.status == STATUS_STOPPED and self.bars_processed > 0:
                # A stopped runner stays stopped; spawn a fresh one to re-run.
                logger.warning("Runner %s already stopped; start() ignored", self.instance_id)
                return
            self.status = STATUS_RUNNING
            self.error = None
            # U6.2: re-acquire the chain subscription if this runner was
            # previously stopped (stop released it); start is idempotent.
            if self.options_bridge is not None and self._chain_released:
                try:
                    from backtest.forward.feed_registry import get_chain_bus
                    get_chain_bus().acquire(
                        self._chain_underlying, source=self._chain_source
                    )
                    self._chain_released = False
                except Exception:  # noqa: BLE001 — acquire must never block start
                    logger.exception("chain bus acquire failed for %s", self.instance_id[:8])
            logger.info(
                "Runner %s (%s) started: %s on %s",
                self.instance_id[:8],
                self.config.name,
                self.config.strategy_name,
                self.target_label,
            )

    def pause(self) -> None:
        with self._lock:
            if self.status == STATUS_RUNNING:
                self.status = STATUS_PAUSED
                logger.info("Runner %s paused", self.instance_id[:8])

    def resume(self) -> None:
        with self._lock:
            if self.status == STATUS_PAUSED:
                self.status = STATUS_RUNNING
                self.error = None
                logger.info("Runner %s resumed", self.instance_id[:8])

    def stop(self) -> None:
        with self._lock:
            if not self._chain_released:
                # U6.2: give back the shared chain-generator subscription.
                # (Status is no good as a guard — runners are BORN stopped,
                # so stop() must release exactly once per acquire.)
                if self.options_bridge is not None:
                    try:
                        from backtest.forward.feed_registry import get_chain_bus
                        get_chain_bus().release(
                            self._chain_underlying, source=self._chain_source
                        )
                    except Exception:  # noqa: BLE001 — release must never block stop
                        logger.exception("chain bus release failed for %s", self.instance_id[:8])
                self._chain_released = True
            self.status = STATUS_STOPPED
            logger.info("Runner %s stopped", self.instance_id[:8])

    def flatten_all(self, reason: str = "emergency_flatten") -> int:
        """Market-exit every open position at last known price. Returns count."""
        count = 0
        with self._lock:
            for symbol, pos in list(self.positions.items()):
                price = self.last_price.get(symbol)
                if price is None:
                    continue
                self._emit_close(symbol, price, reason=reason)
                count += 1
        return count

    # ------------------------------------------------------------------ #
    # Manual position management (Live Order Management)
    # ------------------------------------------------------------------ #
    #
    # What an operator can do to a LIVE position from the positions table:
    # set/replace/clear its stop, set/replace/clear its target, close part of
    # it, or close all of it. Nothing here is strategy logic — the levels live
    # beside the book (runner for equity, bridge for option structures) so a
    # strategy reload, a config edit or a restart can never silently drop them
    # (state_store persists them), and enforcement happens on every mark, not
    # only on bars that produce a signal.

    def position_rules_view(self) -> Dict[str, Dict[str, Optional[float]]]:
        """Operator levels for every open position (equity **and** options)."""
        with self._lock:
            rules = {k: dict(v) for k, v in self.position_rules.items()}
            bridge = self.options_bridge
            if bridge is not None and bridge.open_structure_id:
                levels = bridge.manual_rules()
                if any(v is not None for v in levels.values()):
                    rules[bridge.open_structure_id] = dict(levels)
            return rules

    def _resolve_position_key(self, key: str) -> tuple[str, str]:
        """Map a dashboard key → ``("equity", SYMBOL)`` or ``("option", ID)``.

        Accepts what the tables actually render: the symbol (equity), the
        structure id (options, the preferred key) or the structure's display
        label (``"NIFTY bull_call_spread"``), because a human copying a row out
        of a log should not have to translate.
        """
        candidate = str(key or "").strip()
        if not candidate:
            raise KeyError("a position key (symbol or structure id) is required")
        upper = candidate.upper()
        with self._lock:
            if upper in self.positions:
                return "equity", upper
            bridge = self.options_bridge
            if bridge is not None:
                for structure in bridge.option_broker.get_open_structures():
                    label = f"{structure.underlying} {structure.structure_type}"
                    if candidate in (structure.structure_id, label):
                        return "option", structure.structure_id
        raise KeyError(f"no open position matching {candidate!r} on this runner")

    def _position_mark(self, kind: str, resolved: str) -> Optional[float]:
        """The price a level must sit on the correct side of (None if unknown)."""
        if kind == "equity":
            pos = self.positions.get(resolved)
            if pos is None:
                return None
            return self.last_price.get(resolved, pos["entry_price"])
        bridge = self.options_bridge
        if bridge is None or bridge.open_structure_id != resolved:
            return None
        return bridge._open_mark()

    @staticmethod
    def _validated_rule_value(
        value: Any, field: str, mark: Optional[float]
    ) -> Optional[float]:
        """Coerce an operator level, or raise a message the UI can show.

        ``None``/blank clears the level. A level on the wrong side of the
        current mark is REFUSED rather than armed: it would liquidate the
        position on the very next tick, which is what Close is for, and a
        transposed digit must not be able to flatten a book.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            level = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a number, got {value!r}") from None
        if not math.isfinite(level) or level <= 0:
            raise ValueError(f"{field} must be a finite price above 0, got {value!r}")
        if mark:
            if field == RULE_STOP_LOSS and level >= mark:
                raise ValueError(
                    f"stop-loss {level:.2f} is at or above the current price "
                    f"{mark:.2f} — it would exit immediately; use Close instead"
                )
            if field == RULE_TARGET and level <= mark:
                raise ValueError(
                    f"target {level:.2f} is at or below the current price "
                    f"{mark:.2f} — it would exit immediately; use Close instead"
                )
        return round(level, 4)

    def set_position_rule(
        self, key: str, field: str, value: Any
    ) -> Dict[str, Any]:
        """Arm / re-price / clear a manual stop-loss or target on one position.

        Returns ``{"key", "kind", "field", "value", "rules", "mark"}`` — the
        post-change level set, so the caller can echo exactly what is armed.
        """
        field = str(field or "").strip().lower()
        if field not in POSITION_RULE_FIELDS:
            raise ValueError(
                f"unknown position rule {field!r}; expected one of {POSITION_RULE_FIELDS}"
            )
        kind, resolved = self._resolve_position_key(key)
        mark = self._position_mark(kind, resolved)
        level = self._validated_rule_value(value, field, mark)

        with self._lock:
            if kind == "option":
                bridge = self.options_bridge
                if bridge is None:  # pragma: no cover — guarded by resolution
                    raise KeyError(f"no open position matching {key!r}")
                if field == RULE_STOP_LOSS:
                    levels = bridge.set_manual_rule(stop_loss=level)
                else:
                    levels = bridge.set_manual_rule(target=level)
            else:
                rules = self.position_rules.setdefault(resolved, {})
                rules[field] = level
                if not any(v is not None for v in rules.values()):
                    self.position_rules.pop(resolved, None)
                levels = {
                    f: self.position_rules.get(resolved, {}).get(f)
                    for f in POSITION_RULE_FIELDS
                }

        action = "SET" if level is not None else "CLEARED"
        self._log_signal(
            resolved if kind == "equity" else resolved[:8],
            f"MANUAL_{field.upper()}",
            None,
            mark,
            f"{field} {action} at {level if level is not None else '—'}"
            + (f" (mark {mark:.2f})" if mark else ""),
        )
        logger.info(
            "Runner %s manual %s on %s (%s): %s",
            self.instance_id[:8], field, resolved, kind, level,
        )
        return {
            "instance_id": self.instance_id,
            "key": resolved,
            "kind": kind,
            "field": field,
            "value": level,
            "rules": {f: levels.get(f) for f in POSITION_RULE_FIELDS},
            "mark": round(mark, 4) if mark else None,
        }

    def clear_position_rule(self, key: str, field: str) -> Dict[str, Any]:
        """Drop one operator level (``set_position_rule(key, field, None)``)."""
        return self.set_position_rule(key, field, None)

    def _check_position_rules(self, symbol: str, price: float) -> bool:
        """Fire an operator stop/target for one symbol at ``price``.

        Returns True when a position was closed. Runs on EVERY mark (each bar
        for single runners, each tick for pool books, and on a stress
        markdown) — a manual stop that only worked on bars the strategy traded
        would be worse than no stop at all.
        """
        rules = self.position_rules.get(symbol)
        if not rules:
            return False
        pos = self.positions.get(symbol)
        if pos is None:
            self.position_rules.pop(symbol, None)
            return False
        side = pos["side"]
        long_side = side == "LONG"
        for rule_field, reason in (
            (RULE_STOP_LOSS, "manual_stop_loss"),
            (RULE_TARGET, "manual_target"),
        ):
            level = rules.get(rule_field)
            if level is None:
                continue
            # A stop sits on the losing side of the mark, a target on the
            # winning side — and "losing" flips with the position's direction.
            # (Using the stop test for both would make every target dead code.)
            if rule_field == RULE_STOP_LOSS:
                hit = price <= float(level) if long_side else price >= float(level)
            else:
                hit = price >= float(level) if long_side else price <= float(level)
            if not hit:
                continue
            self._log_signal(
                symbol,
                "MANUAL_EXIT",
                0,
                price,
                f"{reason} {float(level):.2f} hit at {price:.2f}",
            )
            self._emit_close(symbol, price, reason=reason)
            return True
        return False

    def positions_detail(self) -> List[Dict[str, Any]]:
        """Equity positions as flat rows — the positions table's row shape.

        Same keys as an option structure row (``open_structures_snapshot``),
        so one renderer can draw both: ``position_key``, ``kind``, ``symbol``,
        ``side``, ``qty``, ``entry_price``, ``current_price``,
        ``unrealized_pnl``, ``stop_loss``, ``target``, ``can_partial_close``.
        """
        rows: List[Dict[str, Any]] = []
        with self._lock:
            for sym, pos in self.positions.items():
                qty = float(pos["qty"])
                entry = float(pos["entry_price"])
                current = float(self.last_price.get(sym, entry))
                sign = 1.0 if pos["side"] == "LONG" else -1.0
                pnl = (current - entry) * qty * sign
                rules = self.position_rules.get(sym, {})
                rows.append(
                    {
                        "position_key": sym,
                        "kind": "equity",
                        "symbol": sym,
                        "label": sym,
                        "side": pos["side"],
                        "qty": qty,
                        "units": qty,
                        "entry_price": round(entry, 4),
                        "current_price": round(current, 4),
                        "unrealized_pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / (entry * qty), 4) if entry and qty else 0.0,
                        "entry_ts": pos.get("entry_ts"),
                        "stop_loss": rules.get(RULE_STOP_LOSS),
                        "target": rules.get(RULE_TARGET),
                        # Scaling out needs more than one unit to scale.
                        "can_partial_close": qty > 1,
                    }
                )
        return rows

    def close_position(
        self,
        key: str,
        fraction: float = 1.0,
        reason: str = "manual_close",
    ) -> Dict[str, Any]:
        """Close one open position — all of it, or ``fraction`` of it.

        Equity positions scale out: ``fraction=0.5`` books half the ticket's
        realised P&L and leaves the rest working (with its stop/target intact).
        Option structures close **atomically** — every leg together — so a
        partial request is refused with a message rather than silently rounded
        up to a full exit.

        Works while the runner is PAUSED: closing a position de-risks the book,
        which is exactly when a human reaches for this button.
        """
        kind, resolved = self._resolve_position_key(key)
        try:
            frac = float(fraction)
        except (TypeError, ValueError):
            raise ValueError(f"fraction must be a number, got {fraction!r}") from None
        if not 0 < frac <= 1:
            raise ValueError(f"fraction must be in (0, 1], got {frac}")

        with self._lock:
            if kind == "option":
                if frac < 1.0:
                    raise ValueError(
                        "option structures close atomically (all legs together) — "
                        "use Close All, or 100%"
                    )
                bridge = self.options_bridge
                event = (
                    bridge.manual_exit(reason=reason, strategy_name=self.config.strategy_name)
                    if bridge is not None
                    else None
                )
                if event is None:
                    raise KeyError(f"no open position matching {key!r} on this runner")
                self.position_rules.pop(resolved, None)
                self._record_equity_point(self.equity())
                self._log_signal(
                    resolved[:8],
                    "MANUAL_CLOSE",
                    0,
                    self.last_price.get(self._chain_underlying),
                    f"{reason} — closed {event.get('structure_type')} "
                    f"for {float(event.get('pnl', 0.0)):+,.2f}",
                )
                return {
                    "instance_id": self.instance_id,
                    "key": resolved,
                    "kind": "option",
                    "symbol": event.get("structure_type"),
                    "fraction": 1.0,
                    "qty_closed": None,
                    "price": None,
                    "coid": None,
                    "status": "filled",
                    "reason": reason,
                    "realized_pnl": round(float(event.get("pnl", 0.0)), 2),
                    "exit_reason": event.get("reason"),
                    "remaining_qty": 0.0,
                }

            pos = self.positions.get(resolved)
            if pos is None:
                raise KeyError(f"no open position matching {key!r} on this runner")
            qty = float(pos["qty"])
            if frac >= 1.0:
                close_qty = qty
            elif "/" in resolved:  # crypto pairs trade fractionally
                close_qty = round(qty * frac, 8)
            else:
                close_qty = float(int(qty * frac))
            if close_qty <= 0:
                raise ValueError(
                    f"closing {frac:.0%} of {qty:g} {resolved} would close 0 units — "
                    "close it all instead"
                )
            price = self.last_price.get(resolved)
            if price is None:
                raise ValueError(f"no price for {resolved} yet — cannot close safely")
            side = SIDE_SELL if pos["side"] == "LONG" else SIDE_BUY
            try:
                fill = self.broker.submit_market(
                    self.instance_id,
                    resolved,
                    side,
                    close_qty,
                    float(price),
                    tag={
                        "runner": self.config.name,
                        "kind": "exit",
                        "reason": reason,
                        "fraction": round(frac, 4),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — surface it, don't crash the API
                self.error = str(exc)
                logger.exception("Manual close failed for %s: %s", resolved, exc)
                raise

            remaining = float(self.positions.get(resolved, {}).get("qty", 0.0))
            if remaining <= 0:
                # Flat: the levels belonged to THAT ticket. Keep them and the
                # next entry inherits a stop nobody set on it.
                self.position_rules.pop(resolved, None)
            if not isinstance(fill, FillEvent):
                # Live gateway: placed, not filled — say so instead of
                # reporting a fill that has not happened.
                self._log_signal(
                    resolved, "LIVE_ORDER", 0, price,
                    f"{reason} → {side} {close_qty:g} @ ~{price:.2f} PLACED coid={fill}",
                )
                return {
                    "instance_id": self.instance_id,
                    "key": resolved,
                    "kind": "equity",
                    "symbol": resolved,
                    "fraction": frac,
                    "qty_closed": close_qty,
                    "price": round(float(price), 4),
                    "coid": str(fill),
                    "status": "placed",
                    "reason": reason,
                    "realized_pnl": round(self.realized_pnl, 2),
                    "remaining_qty": remaining,
                }

            self._record_equity_point(self.equity())
            self._log_signal(
                resolved, "MANUAL_CLOSE", 0, fill.price,
                f"{reason} → {side} {close_qty:g} @ {fill.price:.2f} "
                f"({frac:.0%} of position)" + ("" if remaining else " — flat"),
            )
            return {
                "instance_id": self.instance_id,
                "key": resolved,
                "kind": "equity",
                "symbol": resolved,
                "fraction": frac,
                "qty_closed": close_qty,
                "price": round(float(fill.price), 4),
                "coid": fill.client_order_id,
                "status": "filled",
                "reason": reason,
                "realized_pnl": round(self.realized_pnl, 2),
                "remaining_qty": remaining,
            }

    # ------------------------------------------------------------------ #
    # Candle event processing
    # ------------------------------------------------------------------ #

    def process_candle_event(self, symbol: str, candle: Dict[str, Any]) -> None:
        """Feed one closed bar. Main entry point from the portfolio feed."""
        symbol = str(symbol).upper()
        if symbol not in self._bars:
            # Unknown symbol for this runner — ignore defensively.
            return

        with self._lock:
            if self.status in (STATUS_STOPPED, STATUS_ERROR):
                return

            try:
                ts = str(candle.get("ts") or candle.get("timestamp") or "")
                price = float(candle["close"])

                # De-dup replayed bars
                if symbol in self._last_bar_ts and ts and ts <= self._last_bar_ts[symbol]:
                    return

                bar = {
                    "ts": ts,
                    "open": float(candle.get("open", price)),
                    "high": float(candle.get("high", price)),
                    "low": float(candle.get("low", price)),
                    "close": price,
                    "volume": float(candle.get("volume", 0)),
                }
                self._bars[symbol].append(bar)
                if ts:
                    self._last_bar_ts[symbol] = ts
                self.last_price[symbol] = price
                self.bars_processed += 1

                self._roll_trading_day(ts)
                self.portfolio.update_prices({symbol: price})
                if self.options_bridge is not None:
                    # A1: re-price the option book before equity is marked,
                    # so the curve and the breakers see fresh premiums.
                    self._mark_option_book(symbol, price, ts)
                # B3: record the curve every bar — the deep-dive chart had no
                # data because nothing called this with record=True.
                self._mark_to_market(record=True)

                if self.config.target_type == TARGET_SINGLE:
                    # Single-symbol runners act immediately on their own bar.
                    # Option runners are exempt from the warmup gate (2026-09-18):
                    # the bridge owns history needs (ATM strike = closest chain
                    # strike, no lookback math), and an "immediate entry" spec
                    # cannot wait 12 bars for a warmup designed for lookback
                    # indicators. The strategy contract is unaffected.
                    warmup_ok = (
                        len(self._bars[symbol]) >= MIN_WARMUP_BARS
                        or self.options_bridge is not None
                    )
                    if warmup_ok:
                        if self.options_bridge is not None:
                            # Gap G3.2: option instrument → expression layer.
                            self._process_option_bar(symbol, bar)
                        else:
                            self._process_single(symbol, bar)
                        self._check_instance_risk()
                # Manual stop/target (Live Order Management): checked AFTER the
                # strategy has had its say on this bar, so an exit the operator
                # asked for can never be immediately undone by a same-bar
                # re-entry the strategy was about to make anyway.
                self._check_position_rules(symbol, price)
                # Portfolio Intelligence: exits the strategy *requested* from
                # on_alert run here, through the normal close path.
                self._apply_strategy_requests()
                # Phase 3: a refused order gets its next attempt here, on the
                # bar clock — after the strategy and the levels, so a retry can
                # never jump ahead of a decision made on the same bar.
                self._pump_retries(symbol, price, ts)
                # Pool mode defers the basket scan to :meth:`on_tick_end`
                # (once per tick instead of once per symbol event — O(n) vs O(n^2)).

            except Exception as exc:  # noqa: BLE001 — one bad bar must not kill the runner
                logger.exception("Runner %s bar error on %s: %s", self.instance_id[:8], symbol, exc)
                self.error = str(exc)

    def apply_markdown(self, symbol: str, price: float, ts: Optional[str] = None) -> None:
        """Re-price a symbol (and re-mark the book) **without** running the
        strategy — used by the circuit-breaker stress test so the halt/flatten
        reflects a pure risk event, not a strategy-generated exit.
        """
        symbol = str(symbol).upper()
        if symbol not in self._bars:
            return
        with self._lock:
            bar = self._bars[symbol][-1] if self._bars[symbol] else None
            new_bar = {
                "ts": ts or (bar["ts"] if bar else ""),
                "open": bar["open"] if bar else price,
                "high": max(bar["high"], price) if bar else price,
                "low": min(bar["low"], price) if bar else price,
                "close": float(price),
                "volume": bar["volume"] if bar else 0,
            }
            # Overwrite the last bar so the buffer index grows for the dedup
            # guard while the latest close is the crashed price.
            if bar is not None:
                self._bars[symbol][-1] = new_bar
            else:
                self._bars[symbol].append(new_bar)
            self.last_price[symbol] = float(price)
            self.portfolio.update_prices({symbol: price})
            if self.options_bridge is not None:
                # A1: a stress markdown is still a market move — re-price the
                # option book so a breaker halt reflects it.
                self._mark_option_book(symbol, float(price), ts or new_bar["ts"])
            self._mark_to_market(record=True)
            # A manual stop is a risk control, not a strategy decision: it
            # must fire on a pure market move too (the crash simulation is the
            # one place where "did my stop actually work?" gets answered).
            self._check_position_rules(symbol, float(price))

    def on_tick_end(self, tick_ts: str) -> None:
        """Hook fired by the feed after every symbol's bar for this tick.

        Pool runners run the basket scan exactly once here.
        """
        with self._lock:
            if self.status in (STATUS_STOPPED, STATUS_ERROR):
                return
            if self.config.target_type != TARGET_POOL:
                return
            try:
                self._process_pool(tick_ts)
                self._check_instance_risk()
                self._apply_strategy_requests()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Runner %s pool scan failed: %s", self.instance_id[:8], exc)
                self.error = str(exc)

    # -- strategy alert responses (Portfolio Intelligence) -----------------

    def _entries_paused_by_strategy(self) -> bool:
        try:
            return bool(getattr(self.strategy, "pause_new_entries", False))
        except Exception:  # noqa: BLE001
            return False

    def _apply_strategy_requests(self) -> List[Dict[str, Any]]:
        """Execute exits the strategy queued via ``request_exit``.

        The platform never decides to close; it only carries out a request
        the strategy made, through :meth:`close_position` (same audit trail
        as a manual close, reason ``strategy_alert:<reason>``). Option
        structures close atomically, so a partial request on one is refused
        and logged instead of splitting a spread's legs.
        """
        drain = getattr(self.strategy, "drain_exit_requests", None)
        if not callable(drain):
            return []
        try:
            requests = drain()
        except Exception:  # noqa: BLE001
            return []
        results: List[Dict[str, Any]] = []
        for req in requests or []:
            fraction = float(req.get("fraction", 1.0) or 1.0)
            reason = f"strategy_alert:{req.get('reason') or 'alert'}"
            key = req.get("position_key")
            if key:
                keys = [str(key)]
            else:
                keys = list(self.positions)
                if self.options_bridge is not None:
                    keys += [
                        s.structure_id
                        for s in self.options_bridge.option_broker.get_open_structures()
                    ]
            for k in keys:
                try:
                    kind, _ = self._resolve_position_key(k)
                    if kind == "option" and fraction < 1.0:
                        self._log_signal(
                            k[:8],
                            "ALERT_EXIT_REFUSED",
                            None,
                            None,
                            f"{reason}: option structures close atomically — "
                            f"partial {fraction:.0%} refused",
                        )
                        continue
                    results.append(self.close_position(k, fraction=fraction, reason=reason))
                except (KeyError, ValueError) as exc:
                    self._log_signal(k[:8], "ALERT_EXIT_REFUSED", None, None, f"{reason}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Runner %s alert exit failed: %s", self.instance_id[:8], exc)
        return results

    # -- single symbol ----------------------------------------------------

    def _process_single(self, symbol: str, bar: Dict[str, Any]) -> None:
        signal = self._signal_for(symbol)
        if signal is None:
            return
        self._act_on_signal(symbol, signal, bar["close"], bar["ts"])

    # -- options (Gap G3.2) -----------------------------------------------

    def _process_option_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        """Run the strategy's market view through the options bridge.

        ``instrument.type == 'option'`` runners come through here instead of
        :meth:`_process_single`: the strategy's ``generate_market_view()``
        output feeds the expression layer (chain → selector → structure →
        paper book). A ``None`` view means "no trade this bar".
        """
        if self.options_bridge is None:
            return
        df = self._bars_to_frame(self._bars[symbol])
        try:
            view = self.strategy.generate_market_view(df)
        except Exception as exc:  # noqa: BLE001 — one bad bar must not kill the runner
            logger.debug(
                "Strategy %s market view failed for %s: %s",
                self.config.strategy_name, symbol, exc,
            )
            self._log_signal(
                symbol, "ERROR", None, self.last_price.get(symbol), f"view error: {exc}"
            )
            return
        if (
            view is not None
            and self._entries_paused_by_strategy()
            and not self.options_bridge._has_open_structure()
        ):
            # Strategy paused new entries (alert response): with nothing open
            # a view could only open a structure, so drop it. With a structure
            # open the view still flows — it drives the exit rules, and the
            # bridge never re-enters on the bar it exits.
            self._log_signal(
                symbol,
                "OPTION_BLOCKED",
                None,
                bar["close"],
                "entry paused by strategy (alert response)",
            )
            view = None
        # A viewless bar is meaningful (B1): the bridge counts it towards the
        # neutral exit rule and can close a position the strategy walked away
        # from. So the call happens even when the strategy has no opinion.
        result = self.options_bridge.on_market_view(view, self.config.strategy_name)
        if not result:
            return
        if result.get("exited"):
            self._log_option_exit(symbol, result, bar["close"])
            return
        if result.get("rejected"):
            self._log_signal(
                symbol, "OPTION_BLOCKED", None, bar["close"], str(result.get("reason"))
            )
            return
        self._log_signal(
            symbol,
            "OPTION_ENTRY",
            1 if result.get("direction") == "bullish" else -1,
            bar["close"],
            "{} {} expiry={} legs={}".format(
                result.get("structure_type"),
                "/".join(result.get("strikes", [])),
                result.get("expiry"),
                result.get("positions"),
            ),
        )
        # Watch-export chart title (2026-09-24): e.g. "long_call 23250".
        try:
            self.last_option_label = "{} {}".format(
                result.get("structure_type", ""),
                "/".join(result.get("strikes", [])),
            ).strip()
        except Exception:  # noqa: BLE001 — a label must never kill the entry
            pass

    def options_summary(self) -> Optional[Dict[str, Any]]:
        """Option-book state for runners with ``instrument.type == 'option'``.

        ``None`` for classic equity runners.
        """
        if self.options_bridge is None:
            return None
        with self._lock:
            return self.options_bridge.summary()

    def _mark_option_book(self, symbol: str, price: float, ts: Optional[str]) -> None:
        """Mark the option book to market for one bar (task A1).

        Called for every closed bar before equity is marked, so an option
        runner's curve, daily P&L and instance breakers are built from fresh
        premiums instead of the entry price. While a structure is open the
        book also emits a throttled ``OPTION_MTM`` heartbeat the deep-dive can
        chart.
        """
        bridge = self.options_bridge
        if bridge is None:
            return
        pnl = bridge.on_bar(symbol, price, ts)
        if pnl is None:
            return
        self.last_option_pnl = float(pnl)
        self._record_watch_point(ts, price, float(pnl))
        # A stop/target/time exit fires inside the pricing hook — drain and log it.
        while True:
            event = bridge.pop_exit_event()
            if event is None:
                break
            self._log_option_exit(symbol, event, price)
        if self.bars_processed % OPTION_MTM_LOG_EVERY == 0:
            self._log_signal(
                symbol, "OPTION_MTM", None, price, f"option MTM {float(pnl):+,.0f}"
            )

    def _record_watch_point(self, ts: Optional[str], spot: float, option_pnl: float) -> None:
        """Append one watch-series row (2026-09-24, watch_export.py).

        Live option runners only — synthetic books are never recorded, so an
        export can never resurrect vanished synthetic P&L. Capped at one
        trading day of 1-min bars (~400) × 4 to bound memory.
        """
        if str(self.config.source).lower() != "mstock":
            return
        if len(self.watch_series) >= 1600:
            self.watch_series.pop(0)
        try:
            self.watch_series.append(
                {
                    "ts": str(ts or ""),
                    "spot": round(float(spot), 2),
                    "option_pnl": round(float(option_pnl), 2),
                    "equity": round(self.equity(), 2),
                }
            )
        except Exception:  # noqa: BLE001 — a bad row must never kill the bar
            pass

    def _log_option_exit(self, symbol: str, event: Dict[str, Any], price: float) -> None:
        """Record a structure close in the signal log (task B1/B3).

        Settlements get their own kind (``OPTION_SETTLED``) so a log reader or
        the UI can tell "we chose to close" from "it expired on us"; both are
        closes for every other purpose.
        """
        # A close always deserves a curve point (B3), so the equity chart lines
        # up with the trade log instead of only showing bar marks.
        self._record_equity_point(self.equity())
        self._log_signal(
            symbol,
            "OPTION_SETTLED" if event.get("settled") else "OPTION_EXIT",
            0,  # flat again — matches the equity flow's EXIT convention
            price,
            "{} closed after {} bars — {} — pnl {:+,.0f}".format(
                event.get("structure_type"),
                event.get("bars_held", 0),
                event.get("detail") or event.get("reason"),
                float(event.get("pnl", 0.0)),
            ),
        )

    # -- pool / universe --------------------------------------------------

    def _process_pool(self, tick_ts: str) -> None:
        """Evaluate every basket symbol; rank candidates; enter top-K."""
        scores: List[tuple] = []  # (score, symbol, signal)
        for symbol in self.config.symbols:
            if len(self._bars[symbol]) < MIN_WARMUP_BARS:
                continue
            signal = self._signal_for(symbol)
            if signal is None:
                continue

            held = symbol in self.positions
            price = self.last_price.get(symbol)
            if price is None:
                continue
            # Exits first: close anything the strategy says to leave.
            if signal == 0 and held:
                self._act_on_signal(symbol, 0, price, tick_ts, score=None)
                continue
            if signal == -1 and held and self.config.allow_short:
                self._act_on_signal(symbol, -1, price, tick_ts, score=None)
                continue
            if signal == 1 and not held:
                scores.append((self._entry_score(symbol), symbol, 1))

        # Rank candidates: strongest score first, symbol name tie-break for
        # deterministic behaviour.
        scores.sort(key=lambda item: (-item[0], item[1]))

        open_slots = self.config.max_pool_positions - len(self.positions)
        for score, symbol, signal in scores[: max(0, open_slots)]:
            self._act_on_signal(symbol, signal, self.last_price.get(symbol), tick_ts, score=score)

    # -- signal / action --------------------------------------------------

    @staticmethod
    def _bars_to_frame(buf: Deque[Dict[str, Any]]) -> pd.DataFrame:
        """Build the canonical OHLCV frame from a rolling buffer."""
        n = len(buf)
        data = {
            "open": [None] * n,
            "high": [None] * n,
            "low": [None] * n,
            "close": [None] * n,
            "volume": [None] * n,
        }
        for i, bar in enumerate(buf):
            data["open"][i] = bar["open"]
            data["high"][i] = bar["high"]
            data["low"][i] = bar["low"]
            data["close"][i] = bar["close"]
            data["volume"][i] = bar["volume"]
        # 2026-09-25: give the frame a REAL time index when every bar carries
        # a parseable timestamp. Time-aware strategies (daily EMA, session
        # windows, 30-min resamples) need ``candles.index`` to be a clock,
        # not a row counter. Unparseable/partial timestamps fall back to the
        # legacy positional index, so nothing that shipped before changes.
        ts_values = [str(bar.get("ts") or "").strip() for bar in buf]
        index = None
        if buf and all(ts_values):
            try:
                parsed = pd.to_datetime(ts_values, errors="coerce")
                if not parsed.isna().any():
                    index = pd.DatetimeIndex(parsed)
            except (TypeError, ValueError):
                index = None
        if index is not None:
            return pd.DataFrame(
                data,
                index=index,
                columns=["open", "high", "low", "close", "volume"],
                dtype="float64",
            )
        return pd.DataFrame(
            data, columns=["open", "high", "low", "close", "volume"], dtype="float64"
        )

    def _signal_for(self, symbol: str) -> Optional[int]:
        """Run the strategy over the symbol's rolling buffer; return {-1,0,1}."""
        df = self._bars_to_frame(self._bars[symbol])
        try:
            series = self.strategy.generate_signals(df)
            return int(series.iloc[-1])
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Strategy %s signal failed for %s: %s", self.config.strategy_name, symbol, exc
            )
            self._log_signal(
                symbol, "ERROR", None, self.last_price.get(symbol), f"signal error: {exc}"
            )
            return None

    def _entry_score(self, symbol: str) -> float:
        """Generic, strategy-agnostic ranking score for pool entries."""
        closes = [b["close"] for b in self._bars[symbol]]
        window = closes[-20:]
        sma = sum(window) / len(window)
        last = closes[-1]
        return (sma - last) / last if last else 0.0

    def _act_on_signal(
        self,
        symbol: str,
        signal: int,
        price: float,
        ts: str,
        score: Optional[float] = None,
    ) -> None:
        held = symbol in self.positions

        # A spent retry budget lifts as soon as the strategy stops asking for
        # that action: the signal becoming a NEW decision (flat again, or the
        # other way) is what makes another attempt legitimate.
        if signal != 1:
            self.clear_retry_block(symbol, "entry")
        if signal == 1:
            self.clear_retry_block(symbol, "exit")

        if signal == 1 and not held:
            if self.status != STATUS_RUNNING:
                self._log_signal(symbol, "BLOCKED", 1, price, f"entry blocked while {self.status}")
                return
            if self._entries_paused_by_strategy():
                # Portfolio Intelligence: the *strategy* chose to pause entries
                # (typically in on_alert). Exits below are unaffected.
                self._log_signal(
                    symbol, "BLOCKED", 1, price, "entry paused by strategy (alert response)"
                )
                return
            if len(self.positions) >= self.config.max_pool_positions:
                self._log_signal(
                    symbol,
                    "BLOCKED",
                    1,
                    price,
                    f"max positions ({self.config.max_pool_positions}) reached",
                )
                return
            self._emit_entry(symbol, price, ts, side=SIDE_BUY, score=score)
        elif signal == 0 and held:
            # Strategy exit — allowed even when paused (it de-risks the book).
            self._emit_close(symbol, price, reason="strategy_exit", ts=ts)
        elif signal == -1 and held:
            # Long-only V1: treat a short signal as flat/exit.
            self._emit_close(symbol, price, reason="strategy_exit", ts=ts)

    # -- order emission (via ledger + executor) ----------------------------

    def _position_size(self, price: float) -> float:
        pct = self.config.position_pct
        if pct is None:
            if self.config.target_type == TARGET_POOL:
                pct = 1.0 / max(1, self.config.max_pool_positions)
            else:
                pct = 0.95
        budget = min(self.cash, self.config.allocated_capital * pct)
        if budget <= 0 or price <= 0:
            return 0.0
        qty = budget / price
        # Whole units for equity symbols; fractional allowed for crypto pairs.
        if "/" not in next(iter(self.config.symbols), ""):
            qty = float(int(qty))
        return round(qty, 6)

    # -- auto-retry (Phase 3) ----------------------------------------------

    def _retry_policy(self) -> Optional[OrderRetryPolicy]:
        policy = self.config.retry_policy
        return policy if policy is not None and policy.max_attempts > 0 else None

    def clear_retry_block(self, symbol: str, kind: Optional[str] = None) -> bool:
        """Lift a spent-budget block (the strategy changed its mind).

        Called automatically when the signal stops asking for the blocked
        action; exposed so an operator can also clear it deliberately.
        """
        blocked = self._retry_blocked.get(symbol)
        if blocked is None or (kind is not None and blocked["kind"] != kind):
            return False
        self._retry_blocked.pop(symbol, None)
        self._log_signal(
            symbol, "RETRY_REARMED", 0, self.last_price.get(symbol),
            f"{blocked['kind']} retry budget re-armed (the signal changed)",
        )
        return True

    def _retry_owns(self, symbol: str, kind: str) -> bool:
        """True when an intent is already spoken for — queued OR spent.

        Queued: the strategy's own re-signal would race the pump and send the
        order twice. Spent: the budget is gone, so a fresh send on the next bar
        is the unbounded loop ``max_attempts`` exists to prevent.
        """
        pending = self._retries.get(symbol)
        if pending is not None and pending["kind"] == kind:
            return True
        blocked = self._retry_blocked.get(symbol)
        if blocked is not None and blocked["kind"] == kind:
            policy = self._retry_policy()
            rearm = float(policy.rearm_after_s) if policy is not None else float("inf")
            if time.time() - float(blocked.get("blocked_ts", 0.0)) < rearm:
                return True
            # The episode has expired: a new one may start (fresh budget),
            # which is a retry at a bounded RATE rather than no retry at all.
            self._retry_blocked.pop(symbol, None)
            self._log_signal(
                symbol, "RETRY_REARMED", 0, self.last_price.get(symbol),
                f"{blocked['kind']} retry budget re-armed after "
                f"{rearm:g}s — starting a new {policy.max_attempts}-attempt episode",
            )
        return False

    def _queue_retry(
        self,
        symbol: str,
        side: str,
        qty: float,
        kind: str,
        exc: Exception,
        now_ts: Optional[float] = None,
        bar_ts: Optional[str] = None,
    ) -> bool:
        """Remember a refused order so a later bar can re-send it.

        Only refusals that are SAFE to re-send are queued (see
        :class:`OrderRefused`): a paper failure leaves nothing at a venue,
        while a live placement error may have been accepted with the
        acknowledgment lost — re-sending there is how one intent becomes two
        live orders, so it is refused with an explicit reason instead.
        """
        policy = self._retry_policy()
        if policy is None or not policy.allows(kind):
            return False
        retryable = getattr(exc, "retryable", False)
        coid = getattr(exc, "client_order_id", None)
        blocked = self._retry_blocked.get(symbol)
        if blocked is not None and blocked["side"] == side and blocked["kind"] == kind:
            # This intent already spent its budget. Re-arming on the next bar
            # would make max_attempts meaningless — the signal stays on until
            # the position exists, which is exactly what is not happening.
            return False
        existing = self._retries.get(symbol)
        if existing is not None and existing["side"] == side and existing["kind"] == kind:
            # The strategy re-signalled the same intent while its retry is
            # still in flight (a transition signal keeps firing while flat).
            # That is NOT a new failure: refreshing the entry must keep the
            # attempt count and the cooldown anchor, or the budget resets every
            # bar and an infinite retry loop hides inside a bounded policy.
            existing["qty"] = float(qty)
            existing["reason"] = str(exc)
            return False
        if not retryable:
            # Not just "don't retry" — don't let the next bar's identical
            # signal quietly do the re-send this refused. The block lifts when
            # the strategy's ask changes, so a human can also reconcile and
            # let the signal fire again.
            self._retry_blocked[symbol] = {
                "side": side, "kind": kind, "reason": str(exc),
                "needs_reconcile": True, "blocked_ts": time.time(),
            }
            self._log_signal(
                symbol, "RETRY_REFUSED", 0, self.last_price.get(symbol),
                f"{kind} refused and NOT retried ({exc}) — the venue may still "
                "hold the order; reconcile before re-sending",
            )
            return False
        self._retries[symbol] = {
            "side": side,
            "qty": float(qty),
            "kind": kind,
            "attempts": 0,
            "cooldown_s": float(policy.cooldown_s),
            "last_ts": float(now_ts if now_ts is not None else time.time()),
            "retry_of": coid,
            "reason": str(exc),
            # The bar this intent was refused ON. One attempt per bar even at
            # cooldown 0, so a retry can never fire inside the same bar that
            # produced the signal it is retrying.
            "bar_ts": bar_ts,
        }
        self.retries_raised += 1
        self._log_signal(
            symbol, "RETRY_QUEUED", 1 if side == SIDE_BUY else 0,
            self.last_price.get(symbol),
            f"{kind} refused ({exc}) — will retry up to "
            f"{policy.max_attempts}x, first after {policy.cooldown_s:g}s",
        )
        return True

    def _pump_retries(self, symbol: str, price: float, ts: str) -> None:
        """Re-send a queued retry once its cooldown has elapsed.

        Called per bar (from the same place the strategy gets its say), so a
        retry rides the market clock rather than a wall-clock timer — the two
        only agree when the feed runs in real time.
        """
        policy = self._retry_policy()
        pending = self._retries.get(symbol)
        if policy is None or pending is None:
            return
        if pending.get("bar_ts") == ts:
            return  # this bar already had its attempt
        now = time.time()
        if now - float(pending["last_ts"]) < float(pending["cooldown_s"]):
            return
        pending["bar_ts"] = ts

        attempt = int(pending["attempts"]) + 1
        if attempt > policy.max_attempts:
            self._retries.pop(symbol, None)
            self._retry_blocked[symbol] = {
                "side": pending["side"], "kind": pending["kind"],
                "reason": pending["reason"], "blocked_ts": time.time(),
            }
            self.retries_exhausted += 1
            self._log_signal(
                symbol, "RETRY_EXHAUSTED", 1 if pending["side"] == SIDE_BUY else 0, price,
                f"{pending['kind']} still refused after {policy.max_attempts} "
                f"retries (last: {pending['reason']}) — giving up; the book "
                "stays as it is until something changes",
            )
            return

        pending["attempts"] = attempt
        pending["last_ts"] = now
        tag = {
            "runner": self.config.name,
            "kind": pending["kind"],
            "retry_attempt": attempt,
            "retry_of": pending["retry_of"],
            "retry_reason": pending["reason"],
        }
        try:
            fill = self.broker.submit_market(
                self.instance_id, symbol, pending["side"], pending["qty"], price, ts=ts, tag=tag
            )
        except Exception as exc:  # noqa: BLE001 — the next bar tries again
            pending["reason"] = str(exc)
            if not getattr(exc, "retryable", False):
                # The situation stopped being retryable (e.g. cash ran out,
                # or the venue started refusing live). Stop, and say why —
                # a retry that cannot succeed must not burn the budget.
                self._retries.pop(symbol, None)
                self._retry_blocked[symbol] = {
                    "side": pending["side"], "kind": pending["kind"],
                    "reason": str(exc), "blocked_ts": time.time(),
                }
                self.retries_exhausted += 1
                self._log_signal(
                    symbol, "RETRY_EXHAUSTED", 1 if pending["side"] == SIDE_BUY else 0, price,
                    f"retry refused as non-retryable ({exc}) — stopping",
                )
            return

        self._retries.pop(symbol, None)
        self._retry_blocked.pop(symbol, None)
        self.retries_recovered += 1
        placed = isinstance(fill, FillEvent)
        self._log_signal(
            symbol,
            "ENTRY" if pending["kind"] == "entry" else "EXIT",
            1 if pending["side"] == SIDE_BUY else 0,
            price if not placed else fill.price,
            f"RETRY {attempt}/{policy.max_attempts} of {pending['kind']} accepted"
            + (f" @ {fill.price:.2f}" if placed else " (placed at the venue)"),
        )

    def _emit_entry(
        self,
        symbol: str,
        price: float,
        ts: str,
        side: str = SIDE_BUY,
        score: Optional[float] = None,
    ) -> None:
        if self._retry_owns(symbol, "entry"):
            # A retry for this exact intent is already queued; sending again
            # here would duplicate the order the pump is about to re-send.
            return
        qty = self._position_size(price)
        if qty <= 0:
            self._log_signal(
                symbol, "NO_FILL", 1, price, f"insufficient capital (cash={self.cash:.2f})"
            )
            return
        try:
            fill = self.broker.submit_market(
                self.instance_id,
                symbol,
                side,
                qty,
                price,
                ts=ts,
                tag={"runner": self.config.name, "kind": "entry", "score": score},
            )
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            logger.exception("Entry order failed for %s: %s", symbol, exc)
            self._queue_retry(symbol, side, qty, "entry", exc, bar_ts=ts)
            return
        score_part = f" | pool rank score {score:+.4f}" if score is not None else ""
        reason = f"BUY signal{score_part}"
        if not isinstance(fill, FillEvent):
            # F-12: the live gateway returns the coid — the order is PLACED,
            # not filled. Logging a fake instant fill here is exactly the
            # silent-paper-trading this seam exists to prevent.
            self._log_signal(
                symbol, "LIVE_ORDER", 1, price,
                f"{reason} → {side} {qty:g} @ ~{price:.2f} PLACED coid={fill}",
            )
            return
        self._log_signal(
            symbol, "ENTRY", 1, fill.price, f"{reason} → {side} {qty:g} @ {fill.price:.2f}"
        )

    def _emit_close(self, symbol: str, price: float, reason: str, ts: Optional[str] = None) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        if self._retry_owns(symbol, "exit") and reason not in (
            "manual_close", "manual_stop_loss", "manual_target",
        ):
            # The queued retry owns the exit. A manual close is an explicit
            # operator instruction and always gets its own attempt.
            return
        try:
            fill = self.broker.submit_market(
                self.instance_id,
                symbol,
                SIDE_SELL,
                pos["qty"],
                price,
                ts=ts,
                tag={"runner": self.config.name, "kind": "exit", "reason": reason},
            )
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            logger.exception("Exit order failed for %s: %s", symbol, exc)
            self._queue_retry(symbol, SIDE_SELL, pos["qty"], "exit", exc, bar_ts=ts)
            return
        if not isinstance(fill, FillEvent):
            self._log_signal(
                symbol, "LIVE_ORDER", 0, price,
                f"{reason} → SELL {pos['qty']:g} @ ~{price:.2f} PLACED coid={fill}",
            )
            return
        self._log_signal(
            symbol, "EXIT", 0, fill.price, f"{reason} → SELL {pos['qty']:g} @ {fill.price:.2f}"
        )
        # A level belongs to the position it was set on: once the book is flat
        # there is nothing left to protect, and carrying the level into the
        # NEXT trade would arm a stop nobody asked for.
        if symbol not in self.positions:
            self.position_rules.pop(symbol, None)

    # ------------------------------------------------------------------ #
    # Fill routing — ledger calls back here (zero cross-contamination).
    # Accounting itself is applied by the executor into the portfolio;
    # this hook is retained for monitoring/compatibility.
    # ------------------------------------------------------------------ #

    def on_fill(self, fill: FillEvent) -> None:
        if fill.instance_id != self.instance_id:
            # Ledger must never route here; guard anyway.
            logger.error(
                "Fill routed to wrong runner: %s != %s", fill.instance_id, self.instance_id
            )
            return
        logger.debug(
            "fill routed to runner %s: %s %s %g @ %s",
            self.instance_id[:8],
            fill.side,
            fill.symbol,
            fill.quantity,
            fill.price,
        )

    # ------------------------------------------------------------------ #
    # Accounting / metrics
    # ------------------------------------------------------------------ #

    def _positions_value(self) -> float:
        return float(self.portfolio.calculate_position_value())

    def _option_realized_pnl(self) -> float:
        """Booked option P&L from closed/expired legs (gross of costs).

        Matches the options dashboard's convention: ``realized_pnl`` is what
        the legs booked, while the commission + statutory fee stack lives in
        :meth:`option_pnl` / :meth:`equity` — netting fees here would make a
        runner show a *negative* realized P&L the instant it opened anything.
        """
        bridge = self.options_bridge
        if bridge is None:
            return 0.0
        return float(bridge.option_broker.total_realized_pnl)

    def option_pnl(self) -> float:
        """Total option-book contribution to this runner's equity (task A2).

        ``realized + unrealized − commission − statutory fees``; ``0.0`` for
        equity runners. This is the bridge's ``net_pnl`` — equity must move by
        exactly this much for the book to be honestly reported.
        """
        bridge = self.options_bridge
        if bridge is None:
            return 0.0
        return float(bridge.net_pnl)

    def unrealized_pnl(self) -> float:
        unrealized = float(self.portfolio.unrealized_pnl)
        if self.options_bridge is not None:
            unrealized += float(self.options_bridge.unrealized_pnl)
        return unrealized

    def equity(self) -> float:
        """Equity portfolio + this runner's option book (task A2).

        Gap P2: the option book used to be invisible here, so an option
        runner's card, bucket aggregate and instance circuit breakers all
        reported the untouched equity portfolio. Folding ``net_pnl`` in makes
        an option drawdown trip the same breakers an equity drawdown does.
        """
        return float(self.portfolio.calculate_total_equity()) + self.option_pnl()

    def deployed_capital(self) -> float:
        deployed = sum(p["qty"] * p["entry_price"] for p in self.positions.values())
        if self.options_bridge is not None:
            deployed += float(self.options_bridge.premium_at_risk)
        return deployed

    def daily_pnl(self) -> float:
        return self.equity() - self._day_start_equity

    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total) if total else 0.0

    def _mark_to_market(self, record: bool = False) -> None:
        equity = self.equity()
        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity
            if dd > self.max_drawdown_pct:
                self.max_drawdown_pct = dd
        if record:
            self._record_equity_point(equity)

    def _record_equity_point(self, equity: float) -> None:
        """Append one equity-curve point, downsampling when the buffer is full.

        ``MAX_EQUITY_POINTS`` used to be a hard stop: once the buffer filled,
        the curve froze and the deep-dive chart silently showed only the start
        of the run. Decimating (keep every other point, then continue) keeps
        the whole history visible at progressively coarser resolution instead.
        A close/stop-out is also always worth a point, so the curve can be read
        against the trade log.
        """
        if len(self.equity_curve) >= MAX_EQUITY_POINTS:
            self.equity_curve = self.equity_curve[::2]
        self.equity_curve.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "equity": round(equity, 2),
            }
        )

    def _roll_trading_day(self, ts: str) -> None:
        # Daily PnL is session-anchored: the baseline is fixed at the first
        # bar the runner sees (warmup and replay do not roll days). Call
        # :meth:`reset_daily_anchor` to re-baseline at the real session start.
        day = ts[:10] if ts else datetime.now(timezone.utc).date().isoformat()
        if self._current_day is None:
            self._current_day = day
            self._day_start_equity = self.equity()

    def reset_daily_anchor(self) -> None:
        """Re-baseline daily PnL at current equity (session open / tests)."""
        with self._lock:
            self._day_start_equity = self.equity()
            self._current_day = datetime.now(timezone.utc).date().isoformat()

    # -- instance-level circuit breakers ----------------------------------

    def _check_instance_risk(self) -> None:
        equity = self.equity()
        alloc = self.config.allocated_capital
        breach = None
        # 0 disables a breaker (2026-09-24, mirrors the global daily-loss
        # convention): a clean watch session spawns with 0/0 and never
        # auto-pauses. A 0 limit can't mean "zero tolerance" — it would trip
        # on the first ₹1 (or instantly, 0 >= 0).
        if self.config.max_drawdown_pct > 0 and self.max_drawdown_pct >= self.config.max_drawdown_pct:
            breach = (
                f"instance max drawdown {self.max_drawdown_pct:.1%} >= "
                f"{self.config.max_drawdown_pct:.1%}"
            )
        elif (
            self.config.daily_loss_limit_pct > 0
            and (alloc - equity) >= alloc * self.config.daily_loss_limit_pct
            and self.daily_pnl() < 0
        ):
            loss_pct = (self._day_start_equity - equity) / alloc
            if loss_pct >= self.config.daily_loss_limit_pct:
                breach = (
                    f"instance daily loss {loss_pct:.1%} >= "
                    f"{self.config.daily_loss_limit_pct:.1%}"
                )
        if breach:
            self._log_signal("-", "RISK_HALT", None, None, breach)
            if self.status == STATUS_RUNNING:
                self.status = STATUS_PAUSED
                self.error = breach

    # ------------------------------------------------------------------ #
    # Logging / state snapshots
    # ------------------------------------------------------------------ #

    def _log_signal(
        self, symbol: str, kind: str, signal: Optional[int], price: Optional[float], reason: str
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "kind": kind,
            "signal": signal,
            "price": round(price, 4) if price is not None else None,
            "reason": reason,
        }
        self.signal_log.append(entry)
        logger.info(
            "Runner %s | %s %s sig=%s @ %s — %s",
            self.config.name,
            symbol,
            kind,
            signal,
            price,
            reason,
        )

    @property
    def target_label(self) -> str:
        if self.config.target_type == TARGET_POOL:
            label = self.config.universe_id or "POOL"
            return f"{label} [{len(self.config.symbols)} symbols]"
        return self.config.symbols[0]

    def get_state(self) -> Dict[str, Any]:
        """Compact row for the portfolio matrix table.

        Task C2: the row is also the matrix's only data source, so it carries
        what an **option** runner needs there. Options live in the bridge, not
        in ``self.positions``, so an option runner holding a spread used to
        report ``open_positions: 0`` (reading as "flat") while its premium was
        genuinely at work. ``open_positions`` now counts equity positions *and*
        open option structures; ``equity_positions`` keeps the old number for
        anything that needs them apart, and ``options`` carries the
        structure-level detail the matrix and deep-dive render.
        """
        with self._lock:
            equity = self.equity()
            options_summary = (
                self.options_bridge.summary() if self.options_bridge is not None else None
            )
            open_structures = int((options_summary or {}).get("open_structures") or 0)
            return {
                "instance_id": self.instance_id,
                "name": self.config.name,
                "strategy_name": self.config.strategy_name,
                "target_type": self.config.target_type,
                "target_label": self.target_label,
                "symbols": list(self.config.symbols),
                "symbol_count": len(self.config.symbols),
                "timeframe": self.config.timeframe,
                "allocated_capital": round(self.config.allocated_capital, 2),
                "equity": round(equity, 2),
                "deployed_capital": round(self.deployed_capital(), 2),
                "open_pnl": round(self.unrealized_pnl(), 2),
                "daily_pnl": round(self.daily_pnl(), 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "win_rate": round(self.win_rate(), 4),
                "max_drawdown_pct": round(self.max_drawdown_pct, 4),
                "open_positions": len(self.positions) + open_structures,
                "equity_positions": len(self.positions),
                "status": self.status,
                "mode": self.config.mode,
                "source": self.config.source,
                "error": self.error,
                "bars_processed": self.bars_processed,
                "last_bar_ts": max(self._last_bar_ts.values()) if self._last_bar_ts else None,
                "created_ts": self.created_ts,
                "instrument": dict(self.config.instrument or {"type": "equity"}),
                "options": options_summary,
                # A1: the book's most recent MTM, mirrored onto the row so a
                # card can show option P&L before it is folded into equity (A2).
                "option_pnl": round(self.last_option_pnl, 2),
                # U3.4: playbook snapshot version for display
                "playbook_id": self.config.playbook_id,
                "playbook_version": self.config.playbook_version,
                "playbook_snapshot": self.config.playbook_snapshot,
                # -- Live Order Management ---------------------------------
                # Per-position rows (equity) + the operator's levels. Option
                # structures already ship their own rows inside ``options``
                # (``open_structures_detail``), enriched with the same
                # ``stop_loss``/``target``/``position_key`` keys so one
                # positions table can render both kinds.
                "positions_detail": self.positions_detail(),
                "position_rules": self.position_rules_view(),
                # Phase 3: refused orders waiting for their next attempt. The
                # dashboard shows this so "the strategy went quiet" and "the
                # venue keeps saying no" are distinguishable at a glance.
                "order_retries": {
                    "pending": [
                        {
                            "symbol": sym,
                            "side": entry["side"],
                            "quantity": entry["qty"],
                            "kind": entry["kind"],
                            "attempts": entry["attempts"],
                            "retry_of": entry["retry_of"],
                            "reason": entry["reason"],
                        }
                        for sym, entry in sorted(self._retries.items())
                    ],
                    "blocked": [
                        {"symbol": sym, **entry}
                        for sym, entry in sorted(self._retry_blocked.items())
                    ],
                    "raised": self.retries_raised,
                    "recovered": self.retries_recovered,
                    "exhausted": self.retries_exhausted,
                },
            }

    def get_detail(self) -> Dict[str, Any]:
        """Full deep-dive payload (Task 6.3)."""
        with self._lock:
            state = self.get_state()
            positions = [
                {
                    "symbol": sym,
                    "side": p["side"],
                    "qty": p["qty"],
                    "entry_price": round(p["entry_price"], 4),
                    "current_price": round(self.last_price.get(sym, p["entry_price"]), 4),
                    "unrealized_pnl": round(
                        (self.last_price.get(sym, p["entry_price"]) - p["entry_price"]) * p["qty"],
                        2,
                    ),
                    "entry_ts": p["entry_ts"],
                    # Live Order Management: the levels armed on this row (the
                    # key the dashboard sends back for any manual action).
                    "position_key": sym,
                    "kind": "equity",
                    "stop_loss": self.position_rules.get(sym, {}).get(RULE_STOP_LOSS),
                    "target": self.position_rules.get(sym, {}).get(RULE_TARGET),
                }
                for sym, p in self.positions.items()
            ]
            return {
                **state,
                "params": dict(self.config.strategy_params),
                "max_pool_positions": self.config.max_pool_positions,
                "positions": positions,
                "trades": list(reversed(self.closed_trades))[:MAX_TRADE_LOG],
                "signals": list(self.signal_log)[::-1],
                "equity_curve": list(self.equity_curve),
                "universe_symbols": list(self.config.symbols),
                "cash": round(self.cash, 2),
            }

    def __repr__(self) -> str:
        return (
            f"<StrategyRunner {self.config.name!r} {self.config.strategy_name} "
            f"{self.target_label} status={self.status}>"
        )


# =====================================================================
# Walk-forward / live loop — multi-strategy buckets (ex-paper.py)
# =====================================================================


@dataclass
class StrategyAccount:
    cash: float = 0.0
    position: float = 0.0
    entry_price: float | None = None
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    equity_history: list[float] = field(default_factory=list)
    blocked: bool = False


class StrategyPortfolio:
    """Multi-strategy bucket view (ex ``forward.portfolio.Portfolio``).

    Each strategy rings its own capital; the container only aggregates.
    """

    def __init__(self, allocations: dict[str, float] | None = None) -> None:
        self.allocations: dict[str, float] = {}
        self.accounts: dict[str, StrategyAccount] = {}
        if allocations:
            for name, capital in allocations.items():
                self.allocate(name, float(capital))

    def allocate(self, strategy: str, capital: float) -> StrategyAccount:
        self.allocations[strategy] = float(capital)
        account = self.accounts.setdefault(strategy, StrategyAccount(cash=float(capital)))
        account.cash = float(capital)
        account.position = 0.0
        account.entry_price = None
        account.realized_pnl = 0.0
        account.unrealized_pnl = 0.0
        account.blocked = False
        account.equity_history = []
        return account

    def mark_to_market(self, prices: dict[str, float]) -> None:
        for strategy, account in self.accounts.items():
            price = float(prices.get(strategy, prices.get("close", 0.0) or 0.0))
            if account.position != 0 and account.entry_price is not None:
                account.unrealized_pnl = (price - account.entry_price) * account.position
            else:
                account.unrealized_pnl = 0.0
            value = (
                account.cash
                + account.position * price
                + account.realized_pnl
                + account.unrealized_pnl
            )
            account.equity_history.append(value)

    def equity(self) -> float:
        total = 0.0
        for strategy, account in self.accounts.items():
            price = 0.0
            if account.position and account.entry_price is not None:
                price = account.entry_price
            total += (
                account.cash
                + account.position * price
                + account.realized_pnl
                + account.unrealized_pnl
            )
        return total

    def snapshot(self) -> dict[str, Any]:
        return {
            "allocations": dict(self.allocations),
            "accounts": {
                name: {
                    "cash": account.cash,
                    "position": account.position,
                    "entry_price": account.entry_price,
                    "realized_pnl": account.realized_pnl,
                    "unrealized_pnl": account.unrealized_pnl,
                    "blocked": account.blocked,
                    "equity_history": account.equity_history,
                }
                for name, account in self.accounts.items()
            },
        }

    @classmethod
    def load_from_snapshot(cls, snapshot: dict[str, Any]) -> "StrategyPortfolio":
        portfolio = cls(snapshot.get("allocations", {}))
        for name, payload in snapshot.get("accounts", {}).items():
            account = StrategyAccount(
                cash=float(payload.get("cash", 0.0)),
                position=float(payload.get("position", 0.0)),
                entry_price=payload.get("entry_price"),
                realized_pnl=float(payload.get("realized_pnl", 0.0)),
                unrealized_pnl=float(payload.get("unrealized_pnl", 0.0)),
                blocked=bool(payload.get("blocked", False)),
                equity_history=list(payload.get("equity_history", [])),
            )
            portfolio.accounts[name] = account
        return portfolio


def save_state(portfolio: StrategyPortfolio, path: str) -> str:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(portfolio.snapshot(), indent=2))
    return str(file_path)


def load_state(path: str) -> StrategyPortfolio:
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, dict) and "portfolio" in payload:
        return StrategyPortfolio.load_from_snapshot(payload["portfolio"])
    return StrategyPortfolio.load_from_snapshot(payload)


#: Compatibility aliases (ticket #6) — the canonical homes are
#: :class:`backtest.data.frame_source.FrameSource` and
#: :func:`backtest.simulator.position_sizing.all_in_size`; this module and
#: its tests historically used the private spellings.
_FrameSource = FrameSource
_all_in_size = all_in_size


def run_walkforward(
    source: DataSource,
    strategies: list[str] | str,
    symbol: str,
    start: str,
    end: str,
    allocations: dict[str, float] | None = None,
    interval: str = "day",
) -> dict[str, Any]:
    """Run each strategy as its own :class:`PaperRunner` bucket over one symbol.

    The port (ticket P1.4): every bucket goes through the simulator
    executor, so entries/exit fills happen at the NEXT bar's open (P1.3)
    and all accounting is Decimal-exact in a
    :class:`~backtest.simulator.portfolio.Portfolio`.
    """
    if isinstance(strategies, str):
        strategies = [strategies]
    if not strategies:
        raise ValueError("at least one strategy required")
    candles = source.get_candles(symbol, start, end, interval)
    if candles is None or candles.empty:
        raise ValueError("source returned no bars")
    if allocations is None:
        allocations = {name: 100_000.0 for name in strategies}

    frame = _FrameSource(candles)
    walk = StrategyPortfolio(allocations)
    equity: dict[str, list[float]] = {}

    for name in strategies:
        capital = float(allocations.get(name, 100_000.0))
        portfolio = Portfolio(
            name=f"walk-{name}",
            initial_capital=capital,
            mode="paper",
            source=source_tag_for(source),
        )
        runner = PaperRunner(
            portfolio=portfolio,
            source=frame,
            strategy=get_strategy(name)(),
            executor=free_executor(portfolio, max_participation="1"),
            symbols=[str(symbol).strip().upper()],
            size_fn=_all_in_size,
        )
        runner.run()

        history = [float(p.total_equity) for p in portfolio.equity_history]
        equity[name] = history

        account = walk.allocate(name, capital)
        account.cash = float(portfolio.current_cash)
        account.position = float(sum(abs(p.quantity) for p in portfolio.positions.values()))
        open_pos = list(portfolio.positions.values())
        account.entry_price = float(open_pos[0].average_entry_price) if open_pos else None
        account.realized_pnl = float(portfolio.realized_pnl)
        account.equity_history = history

    return {
        "portfolio": walk,
        "equity": equity,
        "total_equity": sum(v[-1] for v in equity.values()),
    }


def _load_live_state(path: str) -> tuple[StrategyPortfolio, dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return StrategyPortfolio(), {
            "resume_count": 0,
            "processed_bars": 0,
            "poll_interval_s": 0,
        }
    payload = json.loads(file_path.read_text())
    portfolio = StrategyPortfolio.load_from_snapshot(payload.get("portfolio", payload))
    state = payload.get("state", {})
    return portfolio, {
        "resume_count": int(state.get("resume_count", 0)),
        "processed_bars": int(state.get("processed_bars", 0)),
        "poll_interval_s": int(state.get("poll_interval_s", 0)),
    }


def _save_live_state(portfolio: StrategyPortfolio, path: str, state: dict[str, Any]) -> str:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps({"portfolio": portfolio.snapshot(), "state": state}, indent=2))
    return str(file_path)


def run_live_papertrade(
    source: DataSource,
    strategies: list[str] | str,
    symbol: str,
    allocations: dict[str, float] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    interval: str = "day",
    state_file: str | None = None,
    poll_interval_s: int = 60,
    resume_on_start: bool = True,
) -> dict[str, Any]:
    """Run a live-style paper trade loop with resumable state.

    Same engine as :func:`run_walkforward` (simulator executor, next-bar-
    open fills); each call recomputes the strategy over the window and
    processes only bars not yet persisted. A second call over a fully
    processed window returns the saved state (idempotent).
    """
    if isinstance(strategies, str):
        strategies = [strategies]
    if not strategies:
        raise ValueError("at least one strategy required")
    if allocations is None:
        allocations = {name: 100_000.0 for name in strategies}
    if from_date is None or to_date is None:
        raise ValueError("live papertrade requires both --from and --to")

    candles = source.get_candles(symbol, from_date, to_date, interval)
    if candles is None or candles.empty:
        raise ValueError("source returned no bars")

    portfolio_view = StrategyPortfolio(allocations)
    meta = {"resume_count": 0, "processed_bars": 0, "poll_interval_s": poll_interval_s}

    if state_file and resume_on_start:
        portfolio_view, saved = _load_live_state(state_file)
        meta["resume_count"] = saved["resume_count"] + 1
        meta["processed_bars"] = saved["processed_bars"]
        meta["poll_interval_s"] = saved.get("poll_interval_s", poll_interval_s)
        if meta["processed_bars"] >= len(candles):
            # Fully processed: return the saved state untouched.
            for name in strategies:
                if name not in portfolio_view.accounts:
                    portfolio_view.allocate(name, float(allocations.get(name, 100_000.0)))
            _save_live_state(portfolio_view, state_file, meta)
            return {
                "portfolio": portfolio_view,
                "equity": {
                    name: list(portfolio_view.accounts[name].equity_history) for name in strategies
                },
                "total_equity": sum(
                    portfolio_view.accounts[name].equity_history[-1]
                    for name in strategies
                    if portfolio_view.accounts[name].equity_history
                ),
                "state": dict(meta),
            }

    start_idx = min(int(meta["processed_bars"]), len(candles))
    remaining = candles.iloc[start_idx:] if start_idx else candles

    # Re-run each bucket over the remaining bars (restored accounts carry
    # the cash/position state from the previous call).
    frame = _FrameSource(remaining)
    equity = {
        name: list(portfolio_view.accounts[name].equity_history)
        for name in strategies
        if name in portfolio_view.accounts
    }
    for name in strategies:
        if name not in portfolio_view.accounts:
            portfolio_view.allocate(name, float(allocations.get(name, 100_000.0)))

    for name in strategies:
        account = portfolio_view.accounts[name]
        capital = float(allocations.get(name, 100_000.0))
        # Carry the previous bucket forward: cash plus any open exposure,
        # settled at the saved entry price (no PnL), so the resumed bucket
        # keeps the same capital base.
        start_cash = account.cash if account.cash > 0 else capital
        if account.position and account.entry_price:
            start_cash = account.cash + account.position * account.entry_price
        portfolio = Portfolio(
            name=f"live-{name}",
            initial_capital=capital,
            current_cash=start_cash,
            mode="paper",
            source=source_tag_for(source),
        )
        runner = PaperRunner(
            portfolio=portfolio,
            source=frame,
            strategy=get_strategy(name)(),
            executor=free_executor(portfolio, max_participation="1"),
            symbols=[str(symbol).strip().upper()],
            size_fn=_all_in_size,
        )
        runner.run()
        account.cash = float(portfolio.current_cash)
        account.realized_pnl = float(portfolio.realized_pnl)
        open_pos = list(portfolio.positions.values())
        account.position = float(sum(abs(p.quantity) for p in open_pos))
        account.entry_price = float(open_pos[0].average_entry_price) if open_pos else None
        new_history = [float(p.total_equity) for p in portfolio.equity_history]
        account.equity_history = equity.get(name, []) + new_history
        equity[name] = account.equity_history

    meta["processed_bars"] = len(candles)
    if state_file:
        _save_live_state(portfolio_view, state_file, meta)

    return {
        "portfolio": portfolio_view,
        "equity": equity,
        "total_equity": sum(v[-1] for v in equity.values() if v),
        "state": dict(meta),
    }


def poll_live_papertrade(
    source: DataSource,
    strategies: list[str] | str,
    symbol: str,
    allocations: dict[str, float] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    interval: str = "day",
    state_file: str | None = None,
    poll_interval_s: int = 60,
    resume_on_start: bool = True,
    max_cycles: int | None = None,
) -> list[dict[str, Any]]:
    """Poll market data and process a paper trade loop on each tick."""
    if poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be positive")
    if to_date is None:
        to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cycles = 0
    results: list[dict[str, Any]] = []
    while True:
        result = run_live_papertrade(
            source=source,
            strategies=strategies,
            symbol=symbol,
            allocations=allocations,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            state_file=state_file,
            poll_interval_s=poll_interval_s,
            resume_on_start=resume_on_start,
        )
        results.append(result)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(poll_interval_s)

    return results


# =====================================================================
# PaperRunner — one bar-replay paper run (ticket P1.4)
# =====================================================================

# ``SOURCE_TAGS`` (which ``portfolios.source`` value each source class maps
# to, ticket P1.1) is imported from ``backtest.data.source_tags`` — the
# canonical single copy, shared with ``backtest.engine.backtest_driver``
# (ticket F-14). It is re-exported here (and by ``backtest.forward``) for
# import compatibility.


class PaperRunner:
    """Drives ONE paper run. Reuses simulator/ — no custom engines.

    Parameters
    ----------
    portfolio:
        The :class:`~backtest.simulator.portfolio.Portfolio` that owns cash,
        positions and the equity history for this run.
    source:
        A :class:`~backtest.data.base.DataSource` — the bars' origin. The
        run is tagged with the matching ``source`` value (P1.1).
    strategy:
        A :class:`~backtest.strategy.base.Strategy`. Its vectorised
        ``generate_signals`` is computed once per symbol; a ``0 → 1``
        transition opens a position, a ``1 → 0`` transition closes it.
    executor:
        The :class:`~backtest.simulator.execution.OrderExecutor`. It must be
        the only fill path — the runner wires it to the portfolio when the
        executor was built without one.
    order_queue:
        Optional :class:`OrderQueue` for client-order idempotency.
    symbols / start / end / interval:
        The bar query handed to ``source.get_candles`` per symbol.
    quantity / size_fn:
        Entry sizing — a fixed quantity, or a callable
        ``(symbol, price, portfolio) -> int`` (e.g. all-in). Exits always
        close the actual held quantity.
    db:
        Optional :class:`~backtest.db.manager.DatabaseManager`; when given,
        the portfolio graph (portfolios/positions/orders/fills) is saved at
        the end of the run, tagged ``mode='paper'``.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        source: DataSource,
        strategy: Strategy,
        executor: OrderExecutor,
        order_queue: OrderQueue | None = None,
        symbols: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        interval: str = "day",
        quantity: int = 100,
        size_fn: Callable[[str, float, Portfolio], int] | None = None,
        db: Any = None,
        source_tag: str | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.source = source
        self.strategy = strategy
        self.executor = executor
        self.order_queue = order_queue or OrderQueue()
        self.symbols = [str(s).strip().upper() for s in (symbols or [])]
        self.start = start
        self.end = end
        self.interval = interval
        self.quantity = int(quantity)
        self.size_fn = size_fn
        self.db = db
        self.source_tag = source_tag or source_tag_for(source)

        # Run classification (ticket P1.1): a PaperRunner is, by definition,
        # a paper run; the bars' origin comes from the source class.
        self.portfolio.mode = "paper"
        self.portfolio.source = self.source_tag

    # ------------------------------------------------------------------ #

    def run(self) -> dict[str, Any]:
        """Replay the source bar-by-bar and return the portfolio summary.

        The bar-clock loop lives in
        :func:`backtest.simulator.engine_loop.run_engine_loop` — the SAME
        loop :class:`~backtest.engine.backtest_driver.BacktestDriver`
        drives (ticket P2.1), so backtest and forward are one engine.
        Per bar tick: (1) signal transitions → orders; (2) fills at this
        bar's open for orders armed earlier; (3) mark to market + equity
        snapshot at this bar's close.
        """
        return run_engine_loop(
            source=self.source,
            strategy=self.strategy,
            portfolio=self.portfolio,
            executor=self.executor,
            order_queue=self.order_queue,
            symbols=self.symbols,
            start=self.start,
            end=self.end,
            interval=self.interval,
            quantity=self.quantity,
            size_fn=self.size_fn,
            db=self.db,
            coid_prefix="paper",
            log_label="paper run",
        )
