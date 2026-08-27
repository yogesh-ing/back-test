"""Client Order ID tagging ledger & fill routing (PRD Phase 4 / Task 4.1).

Every order emitted by any :class:`~backtest.forward.runner.StrategyRunner` is
tagged with a deterministic, collision-free client order id::

    PRT-{instance_id}-{timestamp_ms}-{counter}

The ledger keeps an in-memory execution map ``{client_order_id: instance_id}``
so that when the (paper or live) broker reports a fill, the fill event is
routed **strictly** back to the owning runner via ``on_fill`` — with zero
cross-contamination between the 50+ concurrent runners.

The :class:`PaperBroker` is the V1 execution gateway: it fills market orders
immediately at the supplied price (bar close), exercising the full
register → tag → fill → dispatch path without broker credentials. A live
gateway can replace it by feeding :meth:`OrderLedger.apply_fill` from broker
callbacks.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORDER_PENDING = "PENDING"
ORDER_FILLED = "FILLED"
ORDER_CANCELLED = "CANCELLED"
ORDER_REJECTED = "REJECTED"

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

MAX_LEDGER_ORDERS = 100_000  # ring-fence memory in long runs

# Module-level monotonic sequence — timestamps can collide under fast
# benchmark loops, the counter makes every client order id unique.
_ORDER_SEQ = itertools.count(1)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


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


@dataclass
class FillEvent:
    client_order_id: str
    instance_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    ts: str


# ---------------------------------------------------------------------------
# Order ledger
# ---------------------------------------------------------------------------


class OrderLedger:
    """Thread-safe order tagging & fill-routing ledger.

    Runners register a fill handler via :meth:`register_handler` so fills are
    pushed immediately on :meth:`apply_fill`; handlers that miss a callback
    (or any external reader) can also drain :meth:`drain_pending_fills`.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._routing: Dict[str, str] = {}  # client_order_id -> instance_id
        self._orders: Dict[str, Order] = {}
        self._order_history: Deque[str] = deque()
        self._handlers: Dict[str, Callable[[FillEvent], None]] = {}
        self._pending_fills: Dict[str, Deque[FillEvent]] = defaultdict(deque)
        self._fill_count = 0

    # -- registration ------------------------------------------------------

    def register_handler(self, instance_id: str, handler: Callable[[FillEvent], None]) -> None:
        with self._lock:
            self._handlers[instance_id] = handler

    def unregister_handler(self, instance_id: str) -> None:
        with self._lock:
            self._handlers.pop(instance_id, None)

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
            return True

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

    def orders_for(self, instance_id: str) -> List[Order]:
        with self._lock:
            return [o for o in self._orders.values() if o.instance_id == instance_id]

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
        """``PRT-{instance_id}-{timestamp_ms}-{counter}`` (Task 4.1 schema).

        A module-level monotonic counter guarantees uniqueness even when
        timestamps collide (fast benchmark loops).
        """
        ts_ms = int(time.time() * 1000)
        # instance_id is a uuid4 hex; the first 8 chars keep the tag compact
        # while the full id stays in the routing map.
        return f"PRT-{instance_id[:8]}-{ts_ms}-{next(_ORDER_SEQ)}"

    def _trim_locked(self) -> None:
        while len(self._order_history) > MAX_LEDGER_ORDERS:
            old = self._order_history.popleft()
            self._orders.pop(old, None)
            self._routing.pop(old, None)




# ---------------------------------------------------------------------------
# Paper broker gateway
# ---------------------------------------------------------------------------


class PaperBroker:
    """V1 simulated execution gateway.

    Market orders fill immediately at the supplied price (typically the bar
    close). It sits behind the ledger, so every fill is tagged and routed
    through the same path a live gateway would use.
    """

    def __init__(self, ledger: OrderLedger, slippage_pct: float = 0.0):
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
        slip = 1.0 + (self.slippage_pct if side == SIDE_BUY else -self.slippage_pct)
        return self.ledger.apply_fill(order.client_order_id, fill_price * slip, ts=ts)
