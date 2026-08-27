"""StrategyRunner — isolated execution unit (PRD Phase 1 & Task 2.2).

A :class:`StrategyRunner` wraps one strategy instance targeted at either a
**single symbol** or a **symbol universe (pool)**:

* Ring-fenced capital bucket — its cash, positions, trades and PnL are fully
  isolated from every other runner (Task 1.2). A losing runner can never dip
  into another runner's bucket.
* Rolling candle buffers capped at ``MAX_BARS_PER_SYMBOL`` bars per symbol to
  keep memory light (< 15 MB/instance target, Task 5 performance rule).
* Pool mode evaluates the strategy across every symbol in the basket on each
  bar close, ranks the candidates, and opens at most ``max_pool_positions``
  concurrent positions (Task 2.2).
* Every order is emitted through the :class:`~backtest.forward.order_ledger.OrderLedger`
  (tagged ``PRT-{instance}-...``) and fills route back into :meth:`on_fill`.

The runner is a *state machine*: STOPPED → RUNNING ⇄ PAUSED, or ERROR.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

import pandas as pd

from backtest.data.universe import get_universe_symbols
from backtest.forward.order_ledger import (
    PaperBroker,
    SIDE_BUY,
    SIDE_SELL,
    FillEvent,
    OrderLedger,
)

logger = logging.getLogger("backtest.forward.runner")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_SINGLE = "SINGLE_SYMBOL"
TARGET_POOL = "SYMBOL_UNIVERSE"

STATUS_RUNNING = "RUNNING"
STATUS_PAUSED = "PAUSED"
STATUS_STOPPED = "STOPPED"
STATUS_ERROR = "ERROR"

MAX_BARS_PER_SYMBOL = 500          # Task 5: light rolling buffers
MAX_SIGNAL_LOG = 200
MAX_TRADE_LOG = 200
MAX_EQUITY_POINTS = 500
MIN_WARMUP_BARS = 12              # strategies need history to compute


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class RunnerConfig:
    """Spawn configuration for a runner (validated on construction)."""

    name: str
    strategy_name: str
    allocated_capital: float
    target_type: str = TARGET_SINGLE
    symbols: Optional[List[str]] = None
    universe_id: Optional[str] = None
    timeframe: str = "1h"
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    max_pool_positions: int = 5
    position_pct: Optional[float] = None      # fraction of bucket per entry
    instance_id: Optional[str] = None
    # Instance-level circuit breakers (fraction of allocation)
    max_drawdown_pct: float = 0.25
    daily_loss_limit_pct: float = 0.15
    allow_short: bool = False

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("runner name required")
        self.target_type = str(self.target_type).upper()
        if self.target_type not in (TARGET_SINGLE, TARGET_POOL):
            raise ValueError(f"target_type must be {TARGET_SINGLE} or {TARGET_POOL}")
        if self.allocated_capital <= 0:
            raise ValueError("allocated_capital must be positive")
        self.timeframe = str(self.timeframe).strip().lower() or "1h"

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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class StrategyRunner:
    """Isolated strategy execution worker (Layer 1 of the portfolio engine)."""

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
            from backtest.strategy.registry import get_strategy

            self.strategy = get_strategy(config.strategy_name)(**config.strategy_params)

        # -- isolated accounting -------------------------------------------
        self.cash: float = float(config.allocated_capital)
        self.positions: Dict[str, Dict[str, Any]] = {}   # symbol -> position
        self.closed_trades: List[Dict[str, Any]] = []
        self.signal_log: Deque[Dict[str, Any]] = deque(maxlen=MAX_SIGNAL_LOG)

        # -- rolling candle buffers ----------------------------------------
        self._bars: Dict[str, Deque[Dict[str, Any]]] = {
            sym: deque(maxlen=MAX_BARS_PER_SYMBOL) for sym in config.symbols
        }
        self._last_bar_ts: Dict[str, str] = {}

        # -- metrics ---------------------------------------------------------
        self.equity_curve: List[Dict[str, Any]] = []
        self.peak_equity: float = float(config.allocated_capital)
        self.max_drawdown_pct: float = 0.0
        self.realized_pnl: float = 0.0
        self.wins: int = 0
        self.losses: int = 0
        self._day_start_equity: float = float(config.allocated_capital)
        self._current_day: Optional[str] = None
        self.last_price: Dict[str, float] = {}

        # -- state -----------------------------------------------------------
        self.status: str = STATUS_STOPPED
        self.error: Optional[str] = None
        self.bars_processed: int = 0
        self.created_ts: str = datetime.now(timezone.utc).isoformat()

        self._lock = threading.RLock()
        self.ledger.register_handler(self.instance_id, self.on_fill)

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
            logger.info("Runner %s (%s) started: %s on %s",
                        self.instance_id[:8], self.config.name,
                        self.config.strategy_name, self.target_label)

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
            for symbol in list(self.positions.keys()):
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
                self._mark_to_market()

                if self.config.target_type == TARGET_SINGLE:
                    # Single-symbol runners act immediately on their own bar.
                    if len(self._bars[symbol]) >= MIN_WARMUP_BARS:
                        self._process_single(symbol, bar)
                        self._check_instance_risk()
                # Pool mode defers the basket scan to :meth:`on_tick_end`
                # (once per tick instead of once per symbol event — O(n) vs O(n^2)).

            except Exception as exc:  # noqa: BLE001 — one bad bar must not kill the runner
                logger.exception("Runner %s bar error on %s: %s",
                                 self.instance_id[:8], symbol, exc)
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
                logger.exception("Runner %s pool scan failed: %s",
                                 self.instance_id[:8], exc)
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
        scores: List[tuple] = []   # (score, symbol, signal)
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
            self._act_on_signal(symbol, signal, self.last_price.get(symbol),
                                tick_ts, score=score)

    # -- signal / action --------------------------------------------------

    @staticmethod
    def _bars_to_frame(buf: Deque[Dict[str, Any]]) -> pd.DataFrame:
        """Build the canonical OHLCV frame from a rolling buffer.

        Uses a positional ``RangeIndex`` rather than parsing every timestamp —
        the bundled strategies only read OHLCV columns — which keeps the
        per-symbol evaluation cheap enough to scan 50-symbol pools every tick.
        """
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
        return pd.DataFrame(data, columns=["open", "high", "low", "close", "volume"],
                            dtype="float64")

    def _signal_for(self, symbol: str) -> Optional[int]:
        """Run the strategy over the symbol's rolling buffer; return {-1,0,1}."""
        df = self._bars_to_frame(self._bars[symbol])
        try:
            series = self.strategy.generate_signals(df)
            return int(series.iloc[-1])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Strategy %s signal failed for %s: %s",
                         self.config.strategy_name, symbol, exc)
            self._log_signal(symbol, "ERROR", None, self.last_price.get(symbol),
                             f"signal error: {exc}")
            return None

    def _entry_score(self, symbol: str) -> float:
        """Generic, strategy-agnostic ranking score for pool entries.

        Mean-reversion-flavoured strength: how far the latest close sits below
        its 20-bar mean (more stretched → higher rank). Deterministic across
        strategies; trend strategies still get a stable, reproducible order.
        """
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
                self._log_signal(symbol, "BLOCKED", 1, price,
                                 f"entry blocked while {self.status}")
                return
            if len(self.positions) >= self.config.max_pool_positions:
                self._log_signal(symbol, "BLOCKED", 1, price,
                                 f"max positions ({self.config.max_pool_positions}) reached")
                return
            self._emit_entry(symbol, price, ts, side=SIDE_BUY, score=score)
        elif signal == 0 and held:
            # Strategy exit — allowed even when paused (it de-risks the book).
            self._emit_close(symbol, price, reason="strategy_exit", ts=ts)
        elif signal == -1 and held:
            # Long-only V1: treat a short signal as flat/exit.
            self._emit_close(symbol, price, reason="strategy_exit", ts=ts)

    # -- order emission (via ledger) --------------------------------------

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

    def _emit_entry(self, symbol: str, price: float, ts: str,
                    side: str = SIDE_BUY, score: Optional[float] = None) -> None:
        qty = self._position_size(price)
        if qty <= 0:
            self._log_signal(symbol, "NO_FILL", 1, price,
                             f"insufficient capital (cash={self.cash:.2f})")
            return
        try:
            fill = self.broker.submit_market(
                self.instance_id, symbol, side, qty, price, ts=ts,
                tag={"runner": self.config.name, "kind": "entry",
                     "score": score},
            )
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            logger.exception("Entry order failed for %s: %s", symbol, exc)
            return
        reason = (f"BUY signal"
                  + (f" | pool rank score {score:+.4f}" if score is not None else ""))
        self._log_signal(symbol, "ENTRY", 1, fill.price,
                         f"{reason} → {side} {qty:g} @ {fill.price:.2f}")

    def _emit_close(self, symbol: str, price: float, reason: str,
                    ts: Optional[str] = None) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        try:
            fill = self.broker.submit_market(
                self.instance_id, symbol, SIDE_SELL, pos["qty"], price, ts=ts,
                tag={"runner": self.config.name, "kind": "exit", "reason": reason},
            )
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            logger.exception("Exit order failed for %s: %s", symbol, exc)
            return
        self._log_signal(symbol, "EXIT", 0, fill.price,
                         f"{reason} → SELL {pos['qty']:g} @ {fill.price:.2f}")

    # ------------------------------------------------------------------ #
    # Fill routing — ledger calls back here (zero cross-contamination)
    # ------------------------------------------------------------------ #

    def on_fill(self, fill: FillEvent) -> None:
        """Apply a broker fill to this runner's isolated books."""
        if fill.instance_id != self.instance_id:
            # Ledger must never route here; guard anyway.
            logger.error("Fill routed to wrong runner: %s != %s",
                         fill.instance_id, self.instance_id)
            return
        with self._lock:
            if fill.side == SIDE_BUY:
                self.cash -= fill.quantity * fill.price
                self.positions[fill.symbol] = {
                    "symbol": fill.symbol,
                    "side": "LONG",
                    "qty": fill.quantity,
                    "entry_price": fill.price,
                    "entry_ts": fill.ts,
                    "coid": fill.client_order_id,
                }
            else:  # SELL — close the long
                pos = self.positions.pop(fill.symbol, None)
                self.cash += fill.quantity * fill.price
                if pos is not None:
                    pnl = (fill.price - pos["entry_price"]) * min(pos["qty"], fill.quantity)
                    self.realized_pnl += pnl
                    if pnl >= 0:
                        self.wins += 1
                    else:
                        self.losses += 1
                    self.closed_trades.append({
                        "symbol": fill.symbol,
                        "side": pos["side"],
                        "qty": pos["qty"],
                        "entry_price": pos["entry_price"],
                        "exit_price": fill.price,
                        "entry_ts": pos["entry_ts"],
                        "exit_ts": fill.ts,
                        "pnl": round(pnl, 2),
                        "win": pnl >= 0,
                        "coid": pos["coid"],
                        "exit_coid": fill.client_order_id,
                    })
                    if len(self.closed_trades) > MAX_TRADE_LOG:
                        self.closed_trades = self.closed_trades[-MAX_TRADE_LOG:]
            self._mark_to_market(record=True)

    # ------------------------------------------------------------------ #
    # Accounting / metrics
    # ------------------------------------------------------------------ #

    def _positions_value(self) -> float:
        return sum(
            p["qty"] * self.last_price.get(sym, p["entry_price"])
            for sym, p in self.positions.items()
        )

    def unrealized_pnl(self) -> float:
        return sum(
            (self.last_price.get(sym, p["entry_price"]) - p["entry_price"]) * p["qty"]
            for sym, p in self.positions.items()
        )

    def equity(self) -> float:
        return self.cash + self._positions_value()

    def deployed_capital(self) -> float:
        return sum(
            p["qty"] * p["entry_price"] for p in self.positions.values()
        )

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
            self.equity_curve.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "equity": round(equity, 2),
            })

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
            breach = (f"instance max drawdown {self.max_drawdown_pct:.1%} >= "
                      f"{self.config.max_drawdown_pct:.1%}")
        elif (alloc - equity) >= alloc * self.config.daily_loss_limit_pct and self.daily_pnl() < 0:
            loss_pct = (self._day_start_equity - equity) / alloc
            if loss_pct >= self.config.daily_loss_limit_pct:
                breach = (f"instance daily loss {loss_pct:.1%} >= "
                          f"{self.config.daily_loss_limit_pct:.1%}")
        if breach:
            self._log_signal("-", "RISK_HALT", None, None, breach)
            if self.status == STATUS_RUNNING:
                self.status = STATUS_PAUSED
                self.error = breach

    # ------------------------------------------------------------------ #
    # Logging / state snapshots
    # ------------------------------------------------------------------ #

    def _log_signal(self, symbol: str, kind: str, signal: Optional[int],
                    price: Optional[float], reason: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "kind": kind,
            "signal": signal,
            "price": round(price, 4) if price is not None else None,
            "reason": reason,
        }
        self.signal_log.append(entry)
        logger.info("Runner %s | %s %s sig=%s @ %s — %s",
                    self.config.name, symbol, kind, signal, price, reason)

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
                        (self.last_price.get(sym, p["entry_price"]) - p["entry_price"]) * p["qty"], 2),
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
        return (f"<StrategyRunner {self.config.name!r} {self.config.strategy_name} "
                f"{self.target_label} status={self.status}>")
