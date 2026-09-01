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
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

import pandas as pd

from backtest.data.base import CANONICAL_TIMEFRAMES, DataSource
from backtest.data.frame_source import FrameSource
from backtest.data.source_tags import SOURCE_TAG_VALUES, SOURCE_TAGS, source_tag_for
from backtest.data.universe import get_universe_symbols
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

MAX_LEDGER_ORDERS = 100_000  # ring-fence memory in long runs

_ORDER_SEQ = itertools.count(1)


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
        sim_order.validate()
        sim_order.submit()

        result = runner.executor.execute(sim_order, {"bid": price, "ask": price, "last": price})
        fill = result.fill
        if fill is None:
            raise RuntimeError(f"paper fill did not execute: {result.status} — {result.reason}")
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
        self.signal_log: Deque[Dict[str, Any]] = deque(maxlen=MAX_SIGNAL_LOG)

        # -- state -----------------------------------------------------------
        self.status: str = STATUS_STOPPED
        self.error: Optional[str] = None
        self.bars_processed: int = 0
        self.created_ts: str = datetime.now(timezone.utc).isoformat()

        self._lock = threading.RLock()
        self.ledger.register_handler(self.instance_id, self.on_fill)
        self.ledger.register_runner(self.instance_id, self)

    # -- accounting views (over the simulator portfolio) --------------------

    @property
    def cash(self) -> float:
        return float(self.portfolio.current_cash)

    @property
    def realized_pnl(self) -> float:
        return float(self.portfolio.realized_pnl)

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
    def closed_trades(self) -> List[Dict[str, Any]]:
        trades: List[Dict[str, Any]] = []
        for pos in self.portfolio.closed_positions:
            pnl = float(pos.realized_pnl)
            trades.append(
                {
                    "symbol": pos.symbol,
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
                self._mark_to_market()

                if self.config.target_type == TARGET_SINGLE:
                    # Single-symbol runners act immediately on their own bar.
                    if len(self._bars[symbol]) >= MIN_WARMUP_BARS:
                        self._process_single(symbol, bar)
                        self._check_instance_risk()
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
            self._mark_to_market()

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
            except Exception as exc:  # noqa: BLE001
                logger.exception("Runner %s pool scan failed: %s", self.instance_id[:8], exc)
                self.error = str(exc)

    # -- single symbol ----------------------------------------------------

    def _process_single(self, symbol: str, bar: Dict[str, Any]) -> None:
        signal = self._signal_for(symbol)
        if signal is None:
            return
        self._act_on_signal(symbol, signal, bar["close"], bar["ts"])

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

        if signal == 1 and not held:
            if self.status != STATUS_RUNNING:
                self._log_signal(symbol, "BLOCKED", 1, price, f"entry blocked while {self.status}")
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

    def _emit_entry(
        self,
        symbol: str,
        price: float,
        ts: str,
        side: str = SIDE_BUY,
        score: Optional[float] = None,
    ) -> None:
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
            return
        score_part = f" | pool rank score {score:+.4f}" if score is not None else ""
        reason = f"BUY signal{score_part}"
        self._log_signal(
            symbol, "ENTRY", 1, fill.price, f"{reason} → {side} {qty:g} @ {fill.price:.2f}"
        )

    def _emit_close(self, symbol: str, price: float, reason: str, ts: Optional[str] = None) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
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
            return
        self._log_signal(
            symbol, "EXIT", 0, fill.price, f"{reason} → SELL {pos['qty']:g} @ {fill.price:.2f}"
        )

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

    def unrealized_pnl(self) -> float:
        return float(self.portfolio.unrealized_pnl)

    def equity(self) -> float:
        return float(self.portfolio.calculate_total_equity())

    def deployed_capital(self) -> float:
        return sum(p["qty"] * p["entry_price"] for p in self.positions.values())

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
        if record and len(self.equity_curve) < MAX_EQUITY_POINTS:
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
        if self.max_drawdown_pct >= self.config.max_drawdown_pct:
            breach = (
                f"instance max drawdown {self.max_drawdown_pct:.1%} >= "
                f"{self.config.max_drawdown_pct:.1%}"
            )
        elif (alloc - equity) >= alloc * self.config.daily_loss_limit_pct and self.daily_pnl() < 0:
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
        """Compact row for the portfolio matrix table."""
        with self._lock:
            equity = self.equity()
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
                "open_positions": len(self.positions),
                "status": self.status,
                "mode": self.config.mode,
                "source": self.config.source,
                "error": self.error,
                "bars_processed": self.bars_processed,
                "last_bar_ts": max(self._last_bar_ts.values()) if self._last_bar_ts else None,
                "created_ts": self.created_ts,
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
