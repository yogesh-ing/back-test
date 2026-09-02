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
from backtest.forward.paper_runner import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    VALID_INSTANCE_MODES,
    OrderLedger,
    PaperBroker,
    RunnerConfig,
    StrategyRunner,
)
from backtest.forward.risk_supervisor import (
    HALT_FLATTEN,
    STATE_HALTED,
    GlobalRiskConfig,
    RiskSupervisor,
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

        # Aggregate anchors (manager-level — used for combined views)
        self.total_capital: float = 0.0
        self.peak_equity: float = 0.0
        self._day_start_equity: float = 0.0
        self._current_day: Optional[str] = None

        # Manager-level circuit-breaker latch (master kill)
        self.halted: bool = False
        self.halt_reason: Optional[str] = None
        self.halt_mode: Optional[str] = None
        self.halted_ts: Optional[str] = None
        self.last_report: Dict[str, Any] = {}
        self.tick_count: int = 0  # bar events processed
        self.tick_index: int = 0  # complete feed ticks (all symbols)

        # ----------------------------------------------------------------
        # Per-bucket state (C2: derived-not-duplicated — equity/P&L are
        # computed from runner states; we only store anchors + halt latches
        # that cannot be derived.)
        # ----------------------------------------------------------------
        self._bucket_peak: Dict[str, float] = {"paper": 0.0, "live": 0.0}
        self._bucket_halted: Dict[str, bool] = {"paper": False, "live": False}
        self._bucket_halt_reason: Dict[str, Optional[str]] = {"paper": None, "live": None}
        self._bucket_halt_mode: Dict[str, Optional[str]] = {"paper": None, "live": None}
        self._bucket_halted_ts: Dict[str, Optional[str]] = {"paper": None, "live": None}
        self._bucket_day_start: Dict[str, float] = {"paper": 0.0, "live": 0.0}
        self._bucket_day: Dict[str, Optional[str]] = {"paper": None, "live": None}

        # Feed
        self.feed = SyntheticFeed(
            on_bar=self._on_bar,
            tick_seconds=tick_seconds,
            warmup_bars=warmup_bars,
            on_tick_end=self._on_tick_end,
        )
        self._auto_start_feed = auto_start_feed

    # ------------------------------------------------------------------ #
    # Bucket helpers (C2: derived-not-duplicated)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _runner_bucket(runner: StrategyRunner) -> str:
        """Return the bucket key ('paper'|'live') for a runner."""
        return (runner.config.mode or "paper").strip().lower()

    def _bucket_runners(self, mode: str) -> List[StrategyRunner]:
        """Return all runners in a specific bucket."""
        return [r for r in self._runners.values() if self._runner_bucket(r) == mode]

    def _bucket_equity(self, mode: str) -> float:
        """Derive total equity for a bucket from its runners' current state."""
        return sum(r.equity() for r in self._bucket_runners(mode))

    def _bucket_capital(self, mode: str) -> float:
        """Derive total allocated capital for a bucket."""
        return sum(r.config.allocated_capital for r in self._bucket_runners(mode))

    def _bucket_daily_pnl(self, mode: str) -> float:
        """Derive daily P&L for a bucket from its runners."""
        return sum(r.daily_pnl() for r in self._bucket_runners(mode))

    def _bucket_realized_pnl(self, mode: str) -> float:
        """Derive realized P&L for a bucket from its runners."""
        return sum(r.realized_pnl for r in self._bucket_runners(mode))

    def _bucket_deployed(self, mode: str) -> float:
        """Derive deployed capital for a bucket from its runners."""
        return sum(r.deployed_capital() for r in self._bucket_runners(mode))

    def _bucket_open_positions(self, mode: str) -> int:
        """Derive open position count for a bucket."""
        return sum(len(r.positions) for r in self._bucket_runners(mode))

    def _bucket_runner_counts(self, mode: str) -> Dict[str, int]:
        """Derive runner status counts for a bucket."""
        runners = self._bucket_runners(mode)
        return {
            "count": len(runners),
            "running": sum(1 for r in runners if r.status == STATUS_RUNNING),
            "paused": sum(1 for r in runners if r.status == STATUS_PAUSED),
            "stopped": sum(1 for r in runners if r.status == STATUS_STOPPED),
            "errors": sum(1 for r in runners if r.status == "ERROR"),
        }

    def _bucket_drawdown(self, mode: str) -> float:
        """Derive drawdown for a bucket from its peak and current equity."""
        equity = self._bucket_equity(mode)
        peak = self._bucket_peak.get(mode, 0.0)
        return ((peak - equity) / peak) if peak > 0 else 0.0

    def _ensure_bucket_state(self, mode: str) -> None:
        """Initialize per-bucket state if not yet present (idempotent)."""
        if mode not in self._bucket_peak:
            self._bucket_peak[mode] = 0.0
            self._bucket_halted[mode] = False
            self._bucket_halt_reason[mode] = None
            self._bucket_halt_mode[mode] = None
            self._bucket_halted_ts[mode] = None
            self._bucket_day_start[mode] = 0.0
            self._bucket_day[mode] = None

    def get_bucket_aggregates(self) -> Dict[str, Dict[str, Any]]:
        """Per-bucket aggregate snapshot — the C4 embedded bucket data.

        Returns a dict keyed by bucket mode ('paper', 'live') with derived
        equity, P&L, peak, drawdown, halt state, and runner counts. This is
        the SINGLE source of truth for bucket-level metrics — the frontend
        and SSE stream consume this directly.
        """
        buckets: Dict[str, Dict[str, Any]] = {}
        for mode in ("paper", "live"):
            equity = self._bucket_equity(mode)
            capital = self._bucket_capital(mode)
            peak = self._bucket_peak.get(mode, 0.0)
            day_start = self._bucket_day_start.get(mode, 0.0)
            daily = self._bucket_daily_pnl(mode)
            counts = self._bucket_runner_counts(mode)
            limit = abs(self.supervisor.config.daily_loss_limit)

            buckets[mode] = {
                "equity": round(equity, 2),
                "capital": round(capital, 2),
                "peak_equity": round(peak, 2),
                "drawdown_pct": round(self._bucket_drawdown(mode), 4),
                "daily_pnl": round(daily, 2),
                "daily_pnl_pct": (
                    round(daily / day_start, 6) if day_start else 0.0
                ),
                "realized_pnl": round(self._bucket_realized_pnl(mode), 2),
                "deployed_capital": round(self._bucket_deployed(mode), 2),
                "open_positions": self._bucket_open_positions(mode),
                "halted": self._bucket_halted.get(mode, False),
                "halt_reason": self._bucket_halt_reason.get(mode),
                "halt_mode": self._bucket_halt_mode.get(mode),
                "halted_ts": self._bucket_halted_ts.get(mode),
                "daily_loss_used": max(0.0, -daily),
                "daily_loss_pct": round(max(0.0, -daily) / limit, 4) if limit > 0 else 0.0,
                **counts,
            }
        return buckets

    def _check_broker_connected(self) -> bool:
        """C6: Check if the broker session is authenticated.

        Returns ``False`` if the session manager is unavailable (tests,
        standalone usage) or if no session is active.
        """
        try:
            from backtest.brokers.session_manager import get_session_manager
            return get_session_manager().is_authenticated()
        except Exception:  # noqa: BLE001 — never break summary on broker check
            return False

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
            runner = StrategyRunner(
                config, ledger=self.ledger, broker=self.broker, strategy=strategy
            )
            self._runners[runner.instance_id] = runner
            self.total_capital += config.allocated_capital

            # Per-bucket state initialization (idempotent)
            bucket = self._runner_bucket(runner)
            self._ensure_bucket_state(bucket)

            # Feed + anchors
            self.feed.add_symbols(config.symbols)
            self._refresh_anchors()

            if start:
                runner.start()
                if self._auto_start_feed:
                    self.feed.start(warmup=True)

            logger.info(
                "Runner added: %s (%s) alloc=%.0f — %d runners total",
                runner.config.name,
                runner.instance_id[:8],
                config.allocated_capital,
                len(self._runners),
            )
            return runner.instance_id

    def remove_runner(self, instance_id: str) -> bool:
        with self._lock:
            runner = self._runners.pop(instance_id, None)
            if runner is None:
                return False
            bucket = self._runner_bucket(runner)
            runner.stop()
            self.ledger.unregister_handler(instance_id)
            self.feed.remove_symbols(runner.config.symbols)
            self.total_capital -= runner.config.allocated_capital
            self._refresh_anchors()
            # If this was the last runner in the bucket, reset bucket state
            if not self._bucket_runners(bucket):
                self._bucket_peak[bucket] = 0.0
                self._bucket_halted[bucket] = False
                self._bucket_halt_reason[bucket] = None
                self._bucket_halt_mode[bucket] = None
                self._bucket_halted_ts[bucket] = None
                self._bucket_day_start[bucket] = 0.0
                self._bucket_day[bucket] = None
            logger.info("Runner removed: %s (bucket=%s)", runner.config.name, bucket)
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

    def pause_all(self, mode: Optional[str] = None) -> int:
        """Pause runners. ``mode=None`` pauses all; mode='paper'|'live' pauses only that bucket."""
        with self._lock:
            targets = (
                self._bucket_runners(mode) if mode else list(self._runners.values())
            )
            n = 0
            for runner in targets:
                if runner.status == STATUS_RUNNING:
                    runner.pause()
                    n += 1
            logger.warning("Paused %d runners%s", n, f" [{mode}]" if mode else "")
            return n

    def resume_all(self, mode: Optional[str] = None) -> int:
        """Resume runners. ``mode=None`` resumes all; mode='paper'|'live' resumes only that bucket."""
        with self._lock:
            # Check halt state — scoped resume checks bucket halt, not just manager
            if mode is not None:
                if self._bucket_halted.get(mode, False):
                    raise RuntimeError(
                        f"{mode} bucket is halted by circuit breaker; reset that bucket first"
                    )
            else:
                if self.halted:
                    raise RuntimeError("portfolio is halted by circuit breaker; reset first")
            targets = (
                self._bucket_runners(mode) if mode else list(self._runners.values())
            )
            n = 0
            for runner in targets:
                if runner.status == STATUS_PAUSED:
                    runner.resume()
                    n += 1
            logger.info("Resumed %d runners%s", n, f" [{mode}]" if mode else "")
            return n

    def stop_all(self, mode: Optional[str] = None) -> int:
        """Stop runners. ``mode=None`` stops all; mode='paper'|'live' stops only that bucket."""
        with self._lock:
            targets = (
                self._bucket_runners(mode) if mode else list(self._runners.values())
            )
            n = 0
            for runner in targets:
                if runner.status != STATUS_STOPPED:
                    runner.stop()
                    n += 1
            return n

    def emergency_flatten_all(
        self, reason: str = "manual_emergency", mode: Optional[str] = None
    ) -> int:
        """Flatten open positions + halt.

        ``mode=None`` (master kill): flatten ALL runners across ALL buckets
        + set manager-level halt + per-bucket halts (C5). ``mode='paper'``
        or ``mode='live'``: flatten ONLY that bucket's runners + set that
        bucket's halt latch (does NOT touch the other bucket).
        """
        with self._lock:
            now_ts = datetime.now(timezone.utc).isoformat()
            count = 0

            if mode is not None:
                mode = str(mode).strip().lower()
                if mode not in VALID_INSTANCE_MODES:
                    raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")

            targets = (
                self._bucket_runners(mode) if mode else list(self._runners.values())
            )
            for runner in targets:
                count += runner.flatten_all(reason=reason)
            for runner in targets:
                if runner.status == STATUS_RUNNING:
                    runner.pause()

            # Set halt latches
            if mode is None:
                # Master kill: halt everything
                self.halted = True
                self.halt_reason = f"Emergency flatten ({reason})"
                self.halt_mode = HALT_FLATTEN
                self.halted_ts = now_ts
                for m in ("paper", "live"):
                    self._bucket_halted[m] = True
                    self._bucket_halt_reason[m] = f"Emergency flatten ({reason})"
                    self._bucket_halt_mode[m] = HALT_FLATTEN
                    self._bucket_halted_ts[m] = now_ts
            else:
                # Scoped: halt only this bucket
                self._bucket_halted[mode] = True
                self._bucket_halt_reason[mode] = f"Emergency flatten ({reason})"
                self._bucket_halt_mode[mode] = HALT_FLATTEN
                self._bucket_halted_ts[mode] = now_ts
                # If all buckets are halted, set manager-level too
                if all(self._bucket_halted.values()):
                    self.halted = True
                    self.halt_reason = f"Emergency flatten ({reason})"
                    self.halt_mode = HALT_FLATTEN
                    self.halted_ts = now_ts

            logger.critical(
                "EMERGENCY FLATTEN%s: %d positions closed across %d runners",
                f" [{mode}]" if mode else "",
                count,
                len(targets),
            )
            self._evaluate_risk()
            return count

    def reset_daily_anchors(self) -> None:
        """Re-baseline daily PnL for the whole portfolio (session open/tests)."""
        with self._lock:
            for runner in self._runners.values():
                runner.reset_daily_anchor()
            self._day_start_equity = self._aggregate_equity()
            # Per-bucket day anchors
            for mode in ("paper", "live"):
                self._bucket_day_start[mode] = self._bucket_equity(mode)
                self._bucket_day[mode] = datetime.now(timezone.utc).date().isoformat()
            logger.info("Daily PnL anchors reset (baseline=%.2f)", self._day_start_equity)

    def reset_circuit_breaker(self, mode: Optional[str] = None) -> None:
        """Manually clear the halt latch after a breach is acknowledged.

        ``mode=None`` clears BOTH the manager-level halt AND all per-bucket
        halts (master reset). ``mode='paper'`` or ``mode='live'`` clears
        only that bucket's halt — the other bucket and the manager-level
        halt are untouched.
        """
        with self._lock:
            if mode is None:
                # Master reset: clear everything
                self.halted = False
                self.halt_reason = None
                self.halt_mode = None
                self.halted_ts = None
                for m in ("paper", "live"):
                    self._bucket_halted[m] = False
                    self._bucket_halt_reason[m] = None
                    self._bucket_halt_mode[m] = None
                    self._bucket_halted_ts[m] = None
                logger.info("Circuit breaker reset (all buckets)")
            else:
                # Scoped reset: only one bucket
                mode = str(mode).strip().lower()
                if mode not in VALID_INSTANCE_MODES:
                    raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")
                self._bucket_halted[mode] = False
                self._bucket_halt_reason[mode] = None
                self._bucket_halt_mode[mode] = None
                self._bucket_halted_ts[mode] = None
                # If no buckets are halted, clear manager-level too
                if not any(self._bucket_halted.values()):
                    self.halted = False
                    self.halt_reason = None
                    self.halt_mode = None
                    self.halted_ts = None
                logger.info("Circuit breaker reset (bucket=%s)", mode)
            self._refresh_anchors()

    # ------------------------------------------------------------------ #
    # Tick dispatch
    # ------------------------------------------------------------------ #

    @staticmethod
    def _bar_date(bar: Dict[str, Any]) -> str:
        """Extract YYYY-MM-DD from a bar's timestamp."""
        return (
            str(bar.get("ts", ""))[:10]
            or datetime.now(timezone.utc).date().isoformat()
        )

    def _on_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        """Fan one closed candle out to every runner trading that symbol."""
        with self._lock:
            self.tick_count += 1
            bar_date = self._bar_date(bar)

            # Manager-level day anchor (combined views)
            if self._current_day is None:
                self._current_day = bar_date
                self._day_start_equity = self._aggregate_equity()

            # Per-bucket day anchors (C3: flow-aware P&L)
            for mode in ("paper", "live"):
                if self._bucket_day.get(mode) is None and self._bucket_runners(mode):
                    self._bucket_day[mode] = bar_date
                    self._bucket_day_start[mode] = self._bucket_equity(mode)

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
        """Evaluate risk per-bucket (independent breakers) + manager-level.

        Each bucket gets its own risk evaluation. A breach in 'paper' does
        NOT halt 'live' — the breakers are independent (C1/C5). The manager
        also tracks a global halt for the master-kill path.
        """
        now_ts = datetime.now(timezone.utc).isoformat()

        # --- Per-bucket risk evaluation (independent breakers) ---
        for mode in ("paper", "live"):
            bucket_runners = self._bucket_runners(mode)
            if not bucket_runners:
                continue

            equity = self._bucket_equity(mode)
            if equity > self._bucket_peak.get(mode, 0.0):
                self._bucket_peak[mode] = equity

            daily_pnl = equity - self._bucket_day_start.get(mode, 0.0)
            already_halted = self._bucket_halted.get(mode, False)

            report = self.supervisor.evaluate(
                runners=bucket_runners,
                total_equity=equity,
                peak_equity=self._bucket_peak[mode],
                daily_pnl=daily_pnl,
                already_halted=already_halted,
            )

            if report.halted and not already_halted:
                self._bucket_halted[mode] = True
                self._bucket_halt_reason[mode] = report.halt_reason
                self._bucket_halt_mode[mode] = report.halt_mode
                self._bucket_halted_ts[mode] = now_ts
                logger.critical(
                    "BUCKET HALT [%s]: %s (mode=%s)",
                    mode.upper(), report.halt_reason, report.halt_mode,
                )
                # Only pause/flatten runners in THIS bucket
                for runner in bucket_runners:
                    if runner.status == STATUS_RUNNING:
                        runner.pause()
                if report.halt_mode == HALT_FLATTEN:
                    for runner in bucket_runners:
                        runner.flatten_all(reason=f"circuit_breaker_flatten_{mode}")

        # --- Manager-level risk (combined view — for master kill) ---
        all_runners = list(self._runners.values())
        equity = self._aggregate_equity()
        if equity > self.peak_equity:
            self.peak_equity = equity

        report = self.supervisor.evaluate(
            runners=all_runners,
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
            self.halted_ts = now_ts
            logger.critical("PORTFOLIO HALT: %s (mode=%s)", self.halt_reason, self.halt_mode)
            # Master kill: pause ALL runners across ALL buckets
            for runner in all_runners:
                if runner.status == STATUS_RUNNING:
                    runner.pause()
            if self.halt_mode == HALT_FLATTEN:
                for runner in all_runners:
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
        """Refresh manager-level + per-bucket peak/day-start anchors."""
        equity = self._aggregate_equity()
        if equity > self.peak_equity:
            self.peak_equity = equity
        # Session baseline: fixed at the first runner's first bar (or reset).
        if self._day_start_equity <= 0:
            self._day_start_equity = equity

        # Per-bucket anchors
        for mode in ("paper", "live"):
            b_equity = self._bucket_equity(mode)
            if b_equity > self._bucket_peak.get(mode, 0.0):
                self._bucket_peak[mode] = b_equity
            if self._bucket_day_start.get(mode, 0.0) <= 0 and self._bucket_runners(mode):
                self._bucket_day_start[mode] = b_equity

    def list_instances(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-instance rows, optionally filtered to one bucket (ticket P4.1).

        ``mode`` is ``None`` (all buckets), ``"paper"`` or ``"live"``;
        any other value raises :class:`ValueError`.
        """
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in VALID_INSTANCE_MODES:
                raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")
        with self._lock:
            states = [r.get_state() for r in self._runners.values()]
        if mode is not None:
            states = [s for s in states if s.get("mode") == mode]
        return states

    def get_portfolio_summary(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """Aggregate stats + per-instance rows for the command center.

        ``mode`` scopes the view to one bucket ('paper'/'live'); ``None``
        keeps the classic combined view. Scoped views use per-bucket
        peak/drawdown tracking (C2: derived-not-duplicated).

        The response always includes a ``buckets`` dict (C4) with per-bucket
        aggregates — the frontend and SSE stream consume this directly.
        """
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in VALID_INSTANCE_MODES:
                raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")
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

            # C2: Use per-bucket peak when scoped; manager-level when combined
            if mode is not None:
                total_capital = round(sum(s["allocated_capital"] for s in states), 2)
                peak_equity = self._bucket_peak.get(mode, equity)
                day_start = self._bucket_day_start.get(mode, 0.0)
                bucket_halted = self._bucket_halted.get(mode, False)
                bucket_halt_reason = self._bucket_halt_reason.get(mode)
                bucket_halt_mode = self._bucket_halt_mode.get(mode)
                bucket_halted_ts = self._bucket_halted_ts.get(mode)
            else:
                total_capital = self.total_capital
                peak_equity = self.peak_equity
                day_start = self._day_start_equity
                bucket_halted = self.halted
                bucket_halt_reason = self.halt_reason
                bucket_halt_mode = self.halt_mode
                bucket_halted_ts = self.halted_ts
            drawdown = ((peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0
            limit = abs(self.supervisor.config.daily_loss_limit)

            warnings = self.last_report.get("warnings", []) if self.last_report else []

            # C4: embed per-bucket aggregates in the single stream
            buckets = self.get_bucket_aggregates()

            # C6: capability flag — drives the REAL MONEY / Simulated fills banner.
            # broker_connected is a process-level property (is the broker session active?).
            # live_banner differs per bucket: live gets "REAL MONEY" when connected,
            # paper always shows "Simulated fills".
            broker_connected = self._check_broker_connected()
            capability = {
                "broker_connected": broker_connected,
                "live_banner": "REAL MONEY" if broker_connected else "Simulated fills",
                "paper_banner": "Simulated fills",
            }

            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_capital": round(total_capital, 2),
                "total_equity": round(equity, 2),
                "deployed_capital": round(deployed, 2),
                "deployed_pct": (
                    round(deployed / self.total_capital, 4) if self.total_capital > 0 else 0.0
                ),
                "daily_pnl": round(daily, 2),
                "daily_pnl_pct": (
                    round(daily / day_start, 6) if day_start else 0.0
                ),
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
                "halted": bucket_halted,
                "halt_state": STATE_HALTED if bucket_halted else "NORMAL",
                "halt_reason": bucket_halt_reason,
                "halt_mode": bucket_halt_mode,
                "halted_ts": bucket_halted_ts,
                "warnings": warnings,
                "bar_events": self.tick_count,
                "tick": self.tick_index,
                "fill_count": self.ledger.fill_count,
                "order_count": self.ledger.order_count,
                "runners": states,
                "buckets": buckets,
                "capability": capability,
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
        logger.warning(
            "Crash simulation: %d positions marked at -%.0f%%", injected, crash_pct * 100
        )
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
