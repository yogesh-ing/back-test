"""PortfolioManager — multi-strategy orchestration engine (PRD Phase 3 / Task 3.1).

Layer 2 of the forward-testing architecture: a single control tower that
lifecycles up to 50+ :class:`~backtest.forward.runner.StrategyRunner` instances,
fans feed ticks out to them, aggregates portfolio-wide PnL, and enforces global
circuit breakers via :class:`~backtest.forward.risk_supervisor.RiskSupervisor`.

Key properties:
* **Isolated buckets** — every runner rings its own capital; the manager only
  *aggregates* (it never moves capital between runners).
* **One order ledger** — all runners share a single tagging/routing ledger,
  guaranteeing zero cross-contamination of fills.
* **Tick-driven risk** — the supervisor runs on every feed tick; a breach halts
  all runners within the same tick (< 500 ms, Task 7.2).
* **In-memory V1** — state lives in the process; persistence is V2 (matching
  the repo's existing forward-testing V1 scope).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backtest.forward.feed import SyntheticFeed
from backtest.forward.paper_runner import OrderLedger, PaperBroker
from backtest.forward.risk_supervisor import (
    HALT_FLATTEN,
    STATE_HALTED,
    GlobalRiskConfig,
    RiskSupervisor,
)
from backtest.forward.paper_runner import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    VALID_INSTANCE_MODES,
    RunnerConfig,
    StrategyRunner,
)

logger = logging.getLogger("backtest.forward.portfolio")


class PortfolioManager:
    """Master orchestrator controlling all strategy runners."""

    def __init__(
        self,
        risk_config: Optional[GlobalRiskConfig] = None,
        tick_seconds: float = 1.0,
        warmup_bars: int = 30,
        auto_start_feed: bool = True,
    ) -> None:
        self.ledger = OrderLedger()
        self.broker = PaperBroker(self.ledger)
        self.supervisor = RiskSupervisor(risk_config or GlobalRiskConfig())

        self._runners: Dict[str, StrategyRunner] = {}
        self._lock = threading.RLock()

        # Aggregate anchors
        self.total_capital: float = 0.0
        self.peak_equity: float = 0.0
        self._day_start_equity: float = 0.0
        self._current_day: Optional[str] = None

        # Circuit-breaker latch
        self.halted: bool = False
        self.halt_reason: Optional[str] = None
        self.halt_mode: Optional[str] = None
        self.halted_ts: Optional[str] = None
        self.last_report: Dict[str, Any] = {}
        self.tick_count: int = 0    # bar events processed
        self.tick_index: int = 0   # complete feed ticks (all symbols)

        # Feed
        self.feed = SyntheticFeed(
            on_bar=self._on_bar, tick_seconds=tick_seconds,
            warmup_bars=warmup_bars, on_tick_end=self._on_tick_end,
        )
        self._auto_start_feed = auto_start_feed

    # ------------------------------------------------------------------ #
    # Runner lifecycle
    # ------------------------------------------------------------------ #

    def add_runner(
        self,
        config: RunnerConfig,
        start: bool = True,
        strategy: Any = None,
    ) -> str:
        """Spawn a runner from config. Returns its instance_id."""
        with self._lock:
            runner = StrategyRunner(config, ledger=self.ledger,
                                    broker=self.broker, strategy=strategy)
            self._runners[runner.instance_id] = runner
            self.total_capital += config.allocated_capital

            # Feed + anchors
            self.feed.add_symbols(config.symbols)
            self._refresh_anchors()

            if start:
                runner.start()
                if self._auto_start_feed:
                    self.feed.start(warmup=True)

            logger.info("Runner added: %s (%s) alloc=%.0f — %d runners total",
                        runner.config.name, runner.instance_id[:8],
                        config.allocated_capital, len(self._runners))
            return runner.instance_id

    def remove_runner(self, instance_id: str) -> bool:
        with self._lock:
            runner = self._runners.pop(instance_id, None)
            if runner is None:
                return False
            runner.stop()
            self.ledger.unregister_handler(instance_id)
            self.feed.remove_symbols(runner.config.symbols)
            self.total_capital -= runner.config.allocated_capital
            self._refresh_anchors()
            logger.info("Runner removed: %s", runner.config.name)
            return True

    def get_runner(self, instance_id: str) -> Optional[StrategyRunner]:
        return self._runners.get(instance_id)

    def _control(self, instance_id: str, action: str) -> StrategyRunner:
        runner = self._runners.get(instance_id)
        if runner is None:
            raise KeyError(f"unknown runner: {instance_id}")
        action = action.lower()
        if action == "pause":
            runner.pause()
        elif action in ("resume", "start"):
            runner.resume()
        elif action == "stop":
            runner.stop()
        elif action == "flatten":
            runner.flatten_all()
        else:
            raise ValueError(f"unknown control action: {action}")
        return runner

    def control_runner(self, instance_id: str, action: str) -> Dict[str, Any]:
        with self._lock:
            runner = self._control(instance_id, action)
            return runner.get_state()

    def pause_all(self) -> int:
        with self._lock:
            n = 0
            for runner in self._runners.values():
                if runner.status == STATUS_RUNNING:
                    runner.pause()
                    n += 1
            logger.warning("Paused %d runners", n)
            return n

    def resume_all(self) -> int:
        with self._lock:
            if self.halted:
                raise RuntimeError("portfolio is halted by circuit breaker; reset first")
            n = 0
            for runner in self._runners.values():
                if runner.status == STATUS_PAUSED:
                    runner.resume()
                    n += 1
            logger.info("Resumed %d runners", n)
            return n

    def stop_all(self) -> int:
        with self._lock:
            n = 0
            for runner in self._runners.values():
                if runner.status != STATUS_STOPPED:
                    runner.stop()
                    n += 1
            return n

    def emergency_flatten_all(self, reason: str = "manual_emergency") -> int:
        """Mode B: flatten every open position across every runner + halt."""
        with self._lock:
            count = 0
            for runner in self._runners.values():
                count += runner.flatten_all(reason=reason)
            self.halted = True
            self.halt_reason = f"Emergency flatten ({reason})"
            self.halt_mode = HALT_FLATTEN
            self.halted_ts = datetime.now(timezone.utc).isoformat()
            for runner in self._runners.values():
                if runner.status == STATUS_RUNNING:
                    runner.pause()
            logger.critical("EMERGENCY FLATTEN: %d positions closed across %d runners",
                            count, len(self._runners))
            self._evaluate_risk()
            return count

    def reset_daily_anchors(self) -> None:
        """Re-baseline daily PnL for the whole portfolio (session open/tests)."""
        with self._lock:
            for runner in self._runners.values():
                runner.reset_daily_anchor()
            self._day_start_equity = self._aggregate_equity()
            logger.info("Daily PnL anchors reset (baseline=%.2f)", self._day_start_equity)

    def reset_circuit_breaker(self) -> None:
        """Manually clear the halt latch after a breach is acknowledged."""
        with self._lock:
            self.halted = False
            self.halt_reason = None
            self.halt_mode = None
            self.halted_ts = None
            self._refresh_anchors()
            for runner in self._runners.values():
                if runner.status == STATUS_PAUSED and (runner.error or "").startswith("instance "):
                    pass  # leave instance-level halts for the user to resume
            logger.info("Circuit breaker reset")

    # ------------------------------------------------------------------ #
    # Tick dispatch
    # ------------------------------------------------------------------ #

    def _on_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        """Fan one closed candle out to every runner trading that symbol."""
        with self._lock:
            self.tick_count += 1
            # Daily PnL is session-anchored (baseline fixed at first bar /
            # explicit reset) so warmup/replay does not roll the day.
            if self._current_day is None:
                self._current_day = (str(bar.get("ts", ""))[:10]
                                     or datetime.now(timezone.utc).date().isoformat())
                self._day_start_equity = self._aggregate_equity()

            for runner in self._runners.values():
                if symbol.upper() in [s.upper() for s in runner.config.symbols]:
                    runner.process_candle_event(symbol, bar)

    def _on_tick_end(self, tick_ts: str) -> None:
        """All symbols have their bar for this tick: run pool scans + risk."""
        with self._lock:
            self.tick_index += 1
            for runner in self._runners.values():
                runner.on_tick_end(tick_ts)
            self._evaluate_risk()

    def _evaluate_risk(self) -> None:
        runners = list(self._runners.values())
        equity = self._aggregate_equity()
        if equity > self.peak_equity:
            self.peak_equity = equity

        report = self.supervisor.evaluate(
            runners=runners,
            total_equity=equity,
            peak_equity=self.peak_equity,
            daily_pnl=equity - self._day_start_equity,
            already_halted=self.halted,
        )
        self.last_report = report.to_dict()

        if report.halted and not self.halted:
            self.halted = True
            self.halt_reason = report.halt_reason
            self.halt_mode = report.halt_mode
            self.halted_ts = datetime.now(timezone.utc).isoformat()
            logger.critical("PORTFOLIO HALT: %s (mode=%s)", self.halt_reason, self.halt_mode)
            for runner in runners:
                if runner.status == STATUS_RUNNING:
                    runner.pause()
            if self.halt_mode == HALT_FLATTEN:
                for runner in runners:
                    runner.flatten_all(reason="circuit_breaker_flatten")
        elif self.halted and not report.halted:
            # Supervisor says clear but latch stays until explicit reset.
            pass

    # ------------------------------------------------------------------ #
    # Aggregation
    # ------------------------------------------------------------------ #

    def _aggregate_equity(self) -> float:
        return sum(r.equity() for r in self._runners.values())

    def _aggregate_daily_pnl(self) -> float:
        return sum(r.daily_pnl() for r in self._runners.values())

    def _refresh_anchors(self) -> None:
        equity = self._aggregate_equity()
        if equity > self.peak_equity:
            self.peak_equity = equity
        # Session baseline: fixed at the first runner's first bar (or reset).
        if self._day_start_equity <= 0:
            self._day_start_equity = equity

    def list_instances(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-instance rows, optionally filtered to one bucket (ticket P4.1).

        ``mode`` is ``None`` (all buckets), ``"paper"`` or ``"live"``;
        any other value raises :class:`ValueError`.
        """
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in VALID_INSTANCE_MODES:
                raise ValueError(
                    f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}"
                )
        with self._lock:
            states = [r.get_state() for r in self._runners.values()]
        if mode is not None:
            states = [s for s in states if s.get("mode") == mode]
        return states

    def get_portfolio_summary(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """Aggregate stats + per-instance rows for the command center.

        ``mode`` scopes the view to one bucket ('paper'/'live'); ``None``
        keeps the classic combined view. Scoped views aggregate only over
        that bucket's instances — peak/drawdown tracking is manager-level,
        so a scoped view reports the bucket's current equity as its peak
        (v1; bucket-level risk anchors land with the live wiring, F-12).
        """
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in VALID_INSTANCE_MODES:
                raise ValueError(
                    f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}"
                )
        with self._lock:
            states = [r.get_state() for r in self._runners.values()]
        if mode is not None:
            states = [s for s in states if s.get("mode") == mode]
        with self._lock:
            equity = sum(s["equity"] for s in states)
            deployed = sum(s["deployed_capital"] for s in states)
            daily = sum(s["daily_pnl"] for s in states)
            realized = sum(s["realized_pnl"] for s in states)
            open_positions = sum(s["open_positions"] for s in states)
            running = sum(1 for s in states if s["status"] == STATUS_RUNNING)
            paused = sum(1 for s in states if s["status"] == STATUS_PAUSED)
            stopped = sum(1 for s in states if s["status"] == STATUS_STOPPED)
            errors = sum(1 for s in states if s["status"] == "ERROR")

            if mode is not None:
                total_capital = round(sum(s["allocated_capital"] for s in states), 2)
                peak_equity = equity  # scoped: no bucket-level peak tracking (v1)
            else:
                total_capital = self.total_capital
                peak_equity = self.peak_equity
            drawdown = ((peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0
            limit = abs(self.supervisor.config.daily_loss_limit)

            warnings = self.last_report.get("warnings", []) if self.last_report else []

            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_capital": round(total_capital, 2),
                "total_equity": round(equity, 2),
                "deployed_capital": round(deployed, 2),
                "deployed_pct": round(deployed / self.total_capital, 4) if self.total_capital > 0 else 0.0,
                "daily_pnl": round(daily, 2),
                "daily_pnl_pct": round(daily / self._day_start_equity, 6) if self._day_start_equity else 0.0,
                "realized_pnl": round(realized, 2),
                "open_positions": open_positions,
                "runner_count": len(states),
                "running": running,
                "paused": paused,
                "stopped": stopped,
                "errors": errors,
                "daily_loss_limit": limit,
                "daily_loss_used": max(0.0, -daily),
                "daily_loss_pct": round(max(0.0, -daily) / limit, 4) if limit > 0 else 0.0,
                "peak_equity": round(peak_equity, 2),
                "drawdown_pct": round(drawdown, 4),
                "max_drawdown_limit_pct": self.supervisor.config.max_drawdown_pct,
                "halted": self.halted,
                "halt_state": STATE_HALTED if self.halted else "NORMAL",
                "halt_reason": self.halt_reason,
                "halt_mode": self.halt_mode,
                "halted_ts": self.halted_ts,
                "warnings": warnings,
                "bar_events": self.tick_count,
                "tick": self.tick_index,
                "fill_count": self.ledger.fill_count,
                "order_count": self.ledger.order_count,
                "runners": states,
            }

    def get_runner_detail(self, instance_id: str) -> Dict[str, Any]:
        runner = self._runners.get(instance_id)
        if runner is None:
            raise KeyError(f"unknown runner: {instance_id}")
        return runner.get_detail()

    # ------------------------------------------------------------------ #
    # Test / debug helpers
    # ------------------------------------------------------------------ #

    def simulate_crash(self, crash_pct: float = 0.20) -> int:
        """Force a sharp markdown on all open positions (circuit-breaker test).

        Re-prices every *held* symbol at ``(1 - crash_pct) * last_price`` as a
        pure risk event (no strategy scan), so the supervisor sees the loss and
        trips on its own merits. Returns the number of positions marked.
        """
        from datetime import timedelta as _td

        with self._lock:
            ts = (datetime.now(timezone.utc) + _td(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            injected = 0
            for runner in self._runners.values():
                for symbol in list(runner.positions.keys()):
                    price = runner.last_price.get(symbol)
                    if price is None:
                        continue
                    runner.apply_markdown(symbol, price * (1 - crash_pct), ts=ts)
                    injected += 1
            self._evaluate_risk()
        logger.warning("Crash simulation: %d positions marked at -%.0f%%", injected, crash_pct * 100)
        return injected

    def stress_test(
        self,
        crash_pct: float = 0.25,
        daily_loss_limit: Optional[float] = 1_000.0,
        max_drawdown_pct: Optional[float] = 0.05,
    ) -> Dict[str, Any]:
        """Deterministic circuit-breaker demonstration (PRD acceptance step 5).

        Re-baselines the daily PnL anchors so the loss is measured from the
        pre-crash equity, optionally tightens the limits so the breach fires,
        then applies the crash and returns the resulting summary.
        """
        with self._lock:
            self.reset_daily_anchors()
            if daily_loss_limit is not None:
                self.supervisor.config.daily_loss_limit = abs(daily_loss_limit)
            if max_drawdown_pct is not None:
                self.supervisor.config.max_drawdown_pct = max_drawdown_pct
            self.simulate_crash(crash_pct=crash_pct)
            return self.get_portfolio_summary()

    def tick(self, **bar_kwargs: Any) -> None:
        """Advance one synthetic feed tick synchronously (used in tests)."""
        self.feed.emit_one(**bar_kwargs)

    def shutdown(self) -> None:
        self.feed.stop()
        with self._lock:
            for runner in self._runners.values():
                runner.stop()


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_MANAGER: Optional[PortfolioManager] = None
_MANAGER_LOCK = threading.Lock()


def get_portfolio_manager() -> PortfolioManager:
    """Return the process-wide :class:`PortfolioManager` (lazy singleton)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = PortfolioManager()
        return _MANAGER


def reset_portfolio_manager(
    risk_config: Optional[GlobalRiskConfig] = None,
    tick_seconds: float = 1.0,
    warmup_bars: int = 30,
    auto_start_feed: bool = True,
) -> PortfolioManager:
    """Tear down and recreate the singleton (tests / restart)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.shutdown()
        _MANAGER = PortfolioManager(
            risk_config=risk_config,
            tick_seconds=tick_seconds,
            warmup_bars=warmup_bars,
            auto_start_feed=auto_start_feed,
        )
        return _MANAGER
