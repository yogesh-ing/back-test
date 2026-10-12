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
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from backtest.data.sources_policy import policy_key, synthetic_enabled
from backtest.forward.feed import SyntheticFeed
from backtest.forward.feed_registry import get_chain_bus, get_feed_registry
from backtest.forward.state_store import (
    PortfolioStateStore,
    capture_runner,
    restore_runner,
)
from backtest.forward.paper_runner import (
    ORDER_AGING_ALERT_S,
    ORDER_AGING_WARN_S,
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_PENDING,
    ORDER_REJECTED,
    RULE_STOP_LOSS,
    RULE_TARGET,
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
    HALT_PAUSE,
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
        mstock_feed_client: Optional[Any] = None,
        mstock_poll_interval_s: float = 60.0,
        state_path: Optional[Any] = None,
        live_broker: Optional[Any] = None,
        confirm_live_orders: bool = False,
    ) -> None:
        self.ledger = OrderLedger()
        self.broker = PaperBroker(self.ledger)
        self.supervisor = RiskSupervisor(risk_config or GlobalRiskConfig())

        # Separate risk configs for paper and live modes (per-mode independent limits)
        self._risk_configs: Dict[str, GlobalRiskConfig] = {
            "paper": risk_config or GlobalRiskConfig(max_drawdown_pct=0.80),  # 80% for paper
            "live": GlobalRiskConfig(
                daily_loss_limit=50_000.0,
                max_drawdown_pct=0.25,
                max_leverage=1.0,
                breach_mode=HALT_PAUSE,
                correlation_warning_threshold=3
            ),
        }

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
        # Phase D: per-segment breaker supervisors (limits from
        # config/segments.yaml `risk` blocks); latches/anchors live in the
        # `_bucket_*` dicts above under `segment:<name>` keys — the segment
        # breakers ARE the bucket machinery, applied to a narrower slice.
        self._segment_supervisors: Dict[str, RiskSupervisor] = {}

        # Feed — config-gated (2026-09-30). The random-walk feed used to be
        # built unconditionally, so a deployment with synthetic disabled still
        # had a live generator thread sitting there ready to drive any runner
        # whose source was anything other than "mstock". When the policy says
        # no, the object is simply never constructed: ``self.feed is None`` and
        # every routing check below refuses instead of subscribing to it.
        # Tests opt back in via the policy's ``testing`` profile.
        self._synthetic_allowed = synthetic_enabled()
        self.feed: Optional[SyntheticFeed] = None
        if self._synthetic_allowed:
            self.feed = SyntheticFeed(
                on_bar=self._on_bar,
                tick_seconds=tick_seconds,
                warmup_bars=warmup_bars,
                on_tick_end=self._on_tick_end,
            )
        self._auto_start_feed = auto_start_feed

        # U6.2 shared data bus: every runner's symbols + option underlyings
        # are accounted here. One feed per (source, symbol, timeframe); one
        # chain generator per option underlying (see feed_registry module doc).
        self.feed_registry = get_feed_registry()
        self.chain_bus = get_chain_bus()

        # Gap #1: live mStock bars ride the SAME fan-out as synthetic bars.
        # The feed owns ONE poll thread for all its symbols (rate-limit rule);
        # it only runs while at least one runner trades source=mstock.
        from backtest.forward.feed_registry import DhanBarFeed, MStockBarFeed

        self.mstock_feed = MStockBarFeed(
            feed_client=mstock_feed_client,
            poll_interval_s=mstock_poll_interval_s,
        )
        self.mstock_feed.on_bar = self._on_bar
        self.mstock_feed.on_tick_end = self._on_tick_end

        # 2026-09-25: Dhan live bars ride the same fan-out — one poll thread
        # per manager per broker (source=dhan runners).
        self.dhan_feed = DhanBarFeed(poll_interval_s=mstock_poll_interval_s)
        self.dhan_feed.on_bar = self._on_bar
        self.dhan_feed.on_tick_end = self._on_tick_end

        # U3.3: audit log with scope — every control action logs scope=paper|live|playbook|dashboard
        self._audit_log_entries: Deque[Dict[str, Any]] = deque(maxlen=1000)

        # Gap #3 / V2 (P2.4): optional JSON state persistence. No path →
        # byte-for-byte the old in-memory behaviour. With a path, every
        # control-plane mutation (and every 60th tick) snapshots configs +
        # books + breaker latches atomically, and a fresh manager rehydrates
        # them: runners come back PAUSED (fail-closed — nothing trades after
        # a restart until a human resumes), breakers stay tripped.
        if state_path is None:
            # Env default is a PRODUCTION convenience only. Under pytest the
            # repo-root .env is loaded too — a stale/production snapshot at
            # that path would leak 20+ runners into every clean-manager test.
            # Opt-in tests set the env var INSIDE the test (monkeypatch.setenv)
            # after collection, so PYTEST_CURRENT_TEST doesn't exist yet; it
            # only appears once a test is RUNNING. The opt-in test bypasses
            # this by passing the var explicitly before the manager builds.
            if "PYTEST_CURRENT_TEST" not in os.environ or os.environ.get(
                "PORTFOLIO_STATE_PATH_TEST_OPTIN"
            ):
                state_path = os.environ.get("PORTFOLIO_STATE_PATH") or None
        # F-12: live equity order seam. ``live_broker`` is the injected
        # session (tests pass a deterministic fake); None → the active
        # broker from the session manager. The gateway itself is built
        # lazily on the first mode='live' runner — a manager that never
        # goes live never pays the arming check.
        self._injected_live_broker = live_broker
        self._confirm_live_orders = bool(confirm_live_orders)
        # Multi-broker PRD Phase B/C: one gated gateway PER execution broker
        # (keyed by broker name; the legacy default/injected path uses
        # "default"). A runner's orders only ever flow through ITS broker's
        # gateway — never another's.
        self._live_gateways: Dict[str, Any] = {}
        #: Phase 3 order-aging alerts: coid → the bands already announced, so
        #: each working order is reported once per band instead of every tick.
        self._aging_alerted: Dict[str, set] = {}
        self._state_store = PortfolioStateStore(state_path) if state_path else None
        # GAP-4 (2026-09-21): permanent trade history in PostgreSQL. Fail-soft:
        # a missing/unreachable DB degrades to the JSON snapshot only.
        self._trade_persister: Optional[Any] = None
        try:
            from backtest.forward.trade_persistence import LiveTradePersister

            from backtest.db import DatabaseManager

            self._trade_persister = LiveTradePersister(DatabaseManager.from_env())
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.info("trade persistence disabled (no reachable database)", exc_info=True)
        self._restoring = False  # suppress saves while rehydrating
        # Portfolio Intelligence (2026-09-26): read-only aggregate analytics +
        # alert rules. Built before the restore so restored runners register
        # their alert subscriptions. It never touches a position.
        self.intelligence: Optional[Any] = None
        try:
            from backtest.intelligence.service import PortfolioIntelligence

            self.intelligence = PortfolioIntelligence(self)
        except Exception:  # noqa: BLE001 — analytics must never block trading
            logger.exception("portfolio intelligence disabled (init failed)")
        self._restore_state()

        # MTM Staleness Watchdog (R6): periodic 60s mark-repair timer + manual
        # operator trigger (~10s cooldown per runner).
        self._refresh_marks_cooldown_s: float = 10.0
        self._last_manual_refresh_mono: Dict[str, float] = {}
        self.mtm_watchdog_interval_s: float = 60.0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        if auto_start_feed and "PYTEST_CURRENT_TEST" not in os.environ:
            self._start_mtm_watchdog()

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
        """Derive open position count for a bucket.

        Equity positions (``runner.positions``) PLUS open option structures
        (they live in the bridge, not ``positions`` — an option runner holding
        a spread used to report 0 here while its premium was genuinely at
        work). Running-runner semantics: the aggregate views show live books.
        """
        total = 0
        for r in self._bucket_runners(mode):
            total += len(r.positions)
            bridge = getattr(r, "options_bridge", None)
            if bridge is not None:
                try:
                    total += int((bridge.summary() or {}).get("open_structures") or 0)
                except Exception:  # noqa: BLE001 — never break the summary
                    pass
        return total

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

    # ------------------------------------------------------------------ #
    # Segments (Phase D — per-segment breakers reuse the bucket machinery)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _segment_key(segment: str) -> str:
        """Bucket-dict key for a segment's breaker latch/anchor state."""
        return f"segment:{segment}"

    def _segment_runners(self, segment: str) -> List[StrategyRunner]:
        """LIVE runners created into ``segment``.

        Paper runners stay in the shared paper bucket (PRD recommendation:
        segment == bucket key for live only) — a paper experiment inside a
        segment never trips that segment's live breaker.
        """
        seg = str(segment).strip().lower()
        return [
            r
            for r in self._runners.values()
            if (getattr(r.config, "segment", None) or "") == seg
            and self._runner_bucket(r) == "live"
        ]

    def _segment_risk_supervisor(self, segment: str, risk: Dict[str, Any]) -> RiskSupervisor:
        """One cached supervisor per segment, limits from segments.yaml.

        ``risk.max_drawdown_pct`` is configured in PERCENT (e.g. ``15``);
        :class:`GlobalRiskConfig` wants a fraction.
        """
        cached = self._segment_supervisors.get(segment)
        if cached is not None:
            return cached
        dd = risk.get("max_drawdown_pct")
        dd_frac = (float(dd) / 100.0) if dd else GlobalRiskConfig.max_drawdown_pct
        cfg = GlobalRiskConfig(
            daily_loss_limit=abs(float(risk.get("daily_loss_limit") or 0.0)),
            max_drawdown_pct=min(max(dd_frac, 0.001), 0.999),
        )
        supervisor = RiskSupervisor(cfg)
        self._segment_supervisors[segment] = supervisor
        return supervisor

    def get_segment_aggregates(self) -> Dict[str, Dict[str, Any]]:
        """Per-segment risk/exposure rollup (risk page per-broker cards).

        Derived-not-duplicated: equity/P&L summed from the segment's LIVE
        runners; halt latches read from the shared bucket-state dicts under
        the ``segment:<name>`` key.
        """
        try:
            from backtest.brokers.segments import get_segments_config

            segments = get_segments_config().segments
        except Exception:  # noqa: BLE001 — a broken config never breaks the API
            segments = {}
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for name, seg in segments.items():
                runners = self._segment_runners(name)
                key = self._segment_key(name)
                equity = sum(r.equity() for r in runners)
                day_start = self._bucket_day_start.get(key, 0.0)
                peak = self._bucket_peak.get(key, 0.0)
                out[name] = {
                    "display_name": seg.display_name,
                    "broker": seg.broker,
                    "mode": seg.mode,
                    "allocated_capital": seg.allocated_capital,
                    "risk": dict(seg.risk),
                    "runner_count": len(runners),
                    "equity": equity,
                    "daily_pnl": (equity - day_start) if day_start > 0 else 0.0,
                    "deployed": sum(r.deployed_capital() for r in runners),
                    "drawdown_pct": ((peak - equity) / peak) if peak > 0 else 0.0,
                    "halted": self._bucket_halted.get(key, False),
                    "halt_reason": self._bucket_halt_reason.get(key),
                    "halted_ts": self._bucket_halted_ts.get(key),
                }
        return out

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
                "daily_pnl_pct": (round(daily / day_start, 6) if day_start else 0.0),
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

    # ------------------------------------------------------------------
    # U3.3 — Audit logging with scope (AC-16)
    # ------------------------------------------------------------------

    def _audit_log(
        self,
        action: str,
        scope: str = "paper",
        instance_id: Optional[str] = None,
        detail: str = "",
    ) -> None:
        """Log audit entry with scope — paper|live|playbook|dashboard.

        Every control action (spawn, flatten, kill, playbook CRUD, manual close)
        logs scope. Audit view filters on it.
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "scope": scope,
            "instance_id": instance_id,
            "detail": detail,
        }
        self._audit_log_entries.append(entry)
        logger.info(
            "[AUDIT] scope=%s action=%s instance_id=%s detail=%s",
            scope,
            action,
            instance_id or "-",
            detail,
        )

    def get_audit_log(self, scope: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Return audit log entries, optionally filtered by scope.

        GAP-1 fix (2026-09-21): ``scope="all"`` is a selector meaning "no
        filter" (per the U3.3 API contract) — it used to be treated as a
        literal scope value, matching no entries and always returning []
        from the Audit tab.
        """
        entries = list(self._audit_log_entries)
        if scope and scope.lower() != "all":
            scope = scope.lower()
            entries = [e for e in entries if e.get("scope") == scope]
        # Most recent first
        entries = entries[::-1]
        return entries[:limit]

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

    # ------------------------------------------------------------------
    # Options dashboard book — merge second book into bucket book
    # ------------------------------------------------------------------

    def _get_dashboard_book(self) -> Optional[Any]:
        """Return the singleton dashboard OptionPaperBroker if it exists.

        The dashboard book (options_api.get_option_broker) is the second
        execution engine that made trades invisible on Portfolio. Merging
        it here makes flatten and portfolio summary honest.
        """
        try:
            from backtest.web.options_api import get_option_broker, _broker

            # Only if singleton has been created
            if _broker is None:
                return None
            return get_option_broker()
        except Exception:
            return None

    def get_dashboard_book_summary(self) -> Dict[str, Any]:
        """Dashboard book as a runner-like summary for portfolio embedding.

        This is Step 1: Portfolio renders per-structure option trades.
        The dashboard book is presented as a special row so its positions
        are visible on Portfolio and flatten can close them.
        """
        broker = self._get_dashboard_book()
        if broker is None:
            return {
                "exists": False,
                "open_positions": 0,
                "open_structures": 0,
                "equity": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "capital": 0.0,
                "positions": [],
                "structures": [],
            }

        try:
            # Refresh MTM if possible
            try:
                from backtest.web.options_api import get_quote_provider

                quotes = get_quote_provider()
                broker.update_mtm(quotes)
            except Exception:
                pass

            open_positions = broker.get_open_positions()
            open_structures = broker.get_open_structures()

            return {
                "exists": True,
                "open_positions": len(open_positions),
                "open_structures": len(open_structures),
                "equity": float(broker.total_equity),
                "realized_pnl": float(broker.total_realized_pnl),
                "unrealized_pnl": float(broker.total_unrealized_pnl),
                "capital": float(broker.capital),
                "available_cash": float(broker.available_cash),
                "margin_used": float(broker.total_margin_used),
                "quote_source": getattr(broker, "quote_source", "unknown"),
                "positions": [
                    {
                        "position_id": p.position_id,
                        "structure_id": p.structure_id,
                        "trading_symbol": p.trading_symbol,
                        "underlying": p.underlying,
                        "option_type": p.option_type,
                        "strike": str(p.strike),
                        "expiry": str(p.expiry) if p.expiry else None,
                        "side": p.side,
                        "quantity": p.quantity,
                        "lot_size": p.lot_size,
                        "entry_price": float(p.entry_price),
                        "current_price": float(p.current_price),
                        "unrealized_pnl": float(p.unrealized_pnl),
                        "status": p.status.value,
                    }
                    for p in open_positions
                ],
                "structures": [
                    {
                        "structure_id": s.structure_id,
                        "structure_type": s.structure_type,
                        "underlying": s.underlying,
                        "expiry": str(s.expiry) if s.expiry else None,
                        "leg_count": len(s.legs),
                        "total_entry_cost": float(s.total_entry_cost),
                        "total_unrealized_pnl": float(s.total_unrealized_pnl),
                    }
                    for s in open_structures
                ],
            }
        except Exception as exc:
            logger.warning("Dashboard book summary failed: %s", exc)
            return {
                "exists": True,
                "error": str(exc),
                "open_positions": 0,
                "open_structures": 0,
                "equity": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "capital": 0.0,
                "positions": [],
                "structures": [],
            }

    def flatten_dashboard_book(self, reason: str = "emergency_flatten") -> int:
        """Flatten the dashboard book — part of merging second book.

        Called by emergency_flatten_all so flatten closes EVERYTHING,
        not just runner books.
        """
        broker = self._get_dashboard_book()
        if broker is None:
            return 0

        count = 0
        try:
            from backtest.web.options_api import get_quote_provider

            quotes = get_quote_provider()
            for structure in list(broker.get_open_structures()):
                try:
                    broker.close_structure(structure.structure_id, quotes, reason=reason)
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to close dashboard structure %s: %s",
                        structure.structure_id[:8],
                        exc,
                    )
        except Exception as exc:
            logger.warning("Dashboard flatten failed: %s", exc)

        if count:
            logger.critical("Dashboard book flattened: %d structures (%s)", count, reason)
            try:
                self._audit_log(
                    f"FLATTEN_DASHBOARD {reason}", scope="dashboard", detail=f"closed={count}"
                )
            except Exception:
                pass
        return count

    # ------------------------------------------------------------------ #
    # Runner lifecycle
    # ------------------------------------------------------------------ #

    def _resolve_live_broker(self, broker_name: Optional[str] = None) -> Any:
        """The broker session for live orders (F-12, multi-broker Phase B).

        The injected ``live_broker`` wins (tests pass a deterministic fake);
        an explicit ``broker_name`` resolves through the session manager to
        THAT broker's authenticated session (fail-closed: ``None`` when the
        session is absent/expired — NEVER another broker's session);
        otherwise the legacy default: the session manager's active broker.
        """
        if self._injected_live_broker is not None:
            return self._injected_live_broker
        if broker_name:
            try:
                from backtest.brokers.session_manager import get_session_manager

                return get_session_manager().get_authenticated_broker(broker_name)
            except Exception:  # noqa: BLE001 — resolution failure = refusal
                logger.exception("broker resolution failed for %s", broker_name)
                return None
        from backtest.forward.feed_registry import _default_quote_broker

        return _default_quote_broker()

    @property
    def _live_gateway(self) -> Optional[Any]:
        """Legacy single-gateway view: the sole gateway, or ``None``.

        Kept for V1 call sites/tests; multi-broker code paths use
        ``_live_gateways`` / ``_gateway_tracking`` directly.
        """
        if len(self._live_gateways) == 1:
            return next(iter(self._live_gateways.values()))
        return None

    def _live_gateway_for(self, broker_name: Optional[str] = None) -> Any:
        """Build (once per broker) the gated live-order gateway.

        Raises when disarmed or when the named broker has no authenticated
        session — a live runner is REFUSED rather than half-armed.
        """
        key = (broker_name or "").strip().lower() or "default"
        gateway = self._live_gateways.get(key)
        if gateway is None:
            from backtest.forward.live_gateway import LiveEquityGateway

            broker = self._resolve_live_broker(broker_name)
            if broker is None and broker_name:
                from backtest.brokers.execution_router import BrokerSessionExpired

                raise BrokerSessionExpired(broker_name)
            gateway = LiveEquityGateway(
                self.ledger,
                broker,
                confirm_live=self._confirm_live_orders,
            )
            self._live_gateways[key] = gateway
        return gateway

    def _gateway_tracking(self, client_order_id: str) -> Optional[Any]:
        """The gateway that owns a working order — the order's broker."""
        for gateway in self._live_gateways.values():
            try:
                if gateway.working_state(client_order_id) is not None:
                    return gateway
            except Exception:  # noqa: BLE001 — one bad gateway must not hide the rest
                continue
        return None

    def _runner_broker(self, runner: StrategyRunner) -> str:
        """Display label for the broker a runner's orders route to."""
        if runner.config.mode != "live":
            return "paper"
        return runner.config.execution_broker or "live"

    def broker_of(self, instance_id: str) -> str:
        """Public broker label for one runner, resolved by :meth:`_runner_broker`.

        The Orders tab, the risk rollup and the cross-broker analytics all need
        "which venue is this row's" and all three must answer identically, so
        the definition lives here once instead of being re-derived per view.
        Unknown instance → ``"paper"`` (the legacy default venue), never a
        guess at a live broker.
        """
        with self._lock:
            runner = self._runners.get(instance_id)
        if runner is None:
            return "paper"
        return self._runner_broker(runner)

    def reconcile_broker(self, broker_name: Optional[str] = None) -> Dict[str, Any]:
        """Reconcile working orders against one broker's venue book (10.5).

        ``broker_name=None`` reconciles the legacy default gateway. Raises
        ``KeyError`` when no gateway exists for that broker (nothing armed).
        """
        key = (broker_name or "").strip().lower() or "default"
        gateway = self._live_gateways.get(key)
        if gateway is None:
            raise KeyError(f"no live gateway for broker {key!r} — nothing to reconcile")
        result = gateway.reconcile()
        self._audit_log(
            f"RECONCILE · {key}",
            scope="live",
            detail=str(result),
        )
        return result

    def _feed_for(self, source: str):
        """The bar feed a runner with ``source`` may subscribe to.

        The old routing was ``mstock_feed if source == "mstock" else self.feed``
        — a catch-all that handed *anything* not exactly "mstock" (dhan, replay,
        a typo, an unset default) to the synthetic random-walk feed. That is how
        a runner labelled ``paper/MSTOCK`` ended up trading generated candles.
        Routing is now explicit per source and refuses instead of substituting.

        Raises ``ValueError`` (→ 409 at the API) when the source has no feed or
        when the only candidate is synthetic and the policy has it disabled.
        """
        key = str(source or "").strip().lower()
        if key in ("mstock", "dhan"):
            feed = self.mstock_feed if key == "mstock" else self.dhan_feed
            if feed is None:
                raise ValueError(
                    f"source={key} needs a {key} broker feed — none is configured "
                    f"on this portfolio manager"
                )
            return feed
        if policy_key(key) == "synthetic" or not key:
            if not self._synthetic_allowed:
                raise ValueError(
                    f"source={key or 'unset'!r} would run on the synthetic feed, which "
                    f"config/data_sources.yaml disables — forward runners must be created "
                    f"with source=mstock (or dhan). Synthetic stays available to the test "
                    f"suite via BACKTEST_DATA_PROFILE=testing, not to a deployment."
                )
            assert self.feed is not None  # built iff _synthetic_allowed
            return self.feed
        raise ValueError(
            f"no bar feed for source={source!r} on a forward runner — this manager "
            f"feeds runners from mstock, dhan, or (policy permitting) synthetic"
        )

    def add_runner(
        self,
        config: RunnerConfig,
        start: bool = True,
        strategy: Any = None,
    ) -> str:
        """Spawn a runner from config. Returns its instance_id."""
        with self._lock:
            broker_for_runner = self.broker
            if config.mode == "live":
                # F-12: live equity runners route orders to the REAL broker
                # through the gated gateway. Fail-closed: arming raises
                # here (refused runner) rather than paper-filling silently.
                # Multi-broker Phase B: the runner's segment/execution_broker
                # picks WHICH broker's gateway; no broker field keeps the
                # legacy default path.
                broker_for_runner = self._live_gateway_for(config.execution_broker)
            runner = StrategyRunner(
                config, ledger=self.ledger, broker=broker_for_runner, strategy=strategy
            )
            self._runners[runner.instance_id] = runner
            self.total_capital += config.allocated_capital

            # Per-bucket state initialization (idempotent)
            bucket = self._runner_bucket(runner)
            self._ensure_bucket_state(bucket)

            # Feed + anchors — subscriptions accounted in the shared bus
            # (U6.2). Bar-feed subscriptions are taken here; the chain-bus
            # subscription was already taken by StrategyRunner's constructor
            # (the bridge needs its generator at build time), so the manager
            # must NOT acquire again — one runner, one chain subscription.
            # Gap #1: source routes the feed — mstock/dhan runners bar off the
            # shared live poll thread. Synthetic only exists when the policy
            # enables it; ``_feed_for`` refuses every other case instead of
            # defaulting to the random-walk feed.
            feed = self._feed_for(config.source)
            for sym in config.symbols:
                self.feed_registry.subscribe(config.source, sym, config.timeframe, feed=feed)
            feed.add_symbols(config.symbols)
            self._refresh_anchors()

            if start:
                runner.start()
                if self._auto_start_feed:
                    feed.start(warmup=True)
                    # Gap #1: keep the live poll thread in step with the
                    # mstock runner population (one thread total).
                    self._sync_mstock_thread()

            logger.info(
                "Runner added: %s (%s) alloc=%.0f — %d runners total",
                runner.config.name,
                runner.instance_id[:8],
                config.allocated_capital,
                len(self._runners),
            )
            # U3.3 audit
            try:
                self._audit_log(
                    f"SPAWN {runner.config.name}",
                    scope=bucket,
                    instance_id=runner.instance_id,
                    detail=f"strategy={config.strategy_name} capital={config.allocated_capital}",
                )
            except Exception:
                pass
            if self.intelligence is not None:
                try:
                    self.intelligence.register_runner(runner)
                except Exception:  # noqa: BLE001
                    logger.exception("alert subscription failed for %s", runner.instance_id)
            self._persist_state()
            return runner.instance_id

    def remove_runner(self, instance_id: str) -> bool:
        with self._lock:
            runner = self._runners.pop(instance_id, None)
            if runner is None:
                return False
            bucket = self._runner_bucket(runner)
            if self.intelligence is not None:
                try:
                    self.intelligence.unregister_runner(instance_id, runner)
                except Exception:  # noqa: BLE001
                    logger.exception("alert unsubscribe failed for %s", instance_id)
            runner.stop()  # U6.2: stop() releases the runner's chain-bus subscription
            self.ledger.unregister_handler(instance_id)
            # U6.2: release the bar-feed subscription this runner held.
            # (The chain subscription went with runner.stop() — one release
            # per acquire, owned by the runner.)
            for sym in runner.config.symbols:
                self.feed_registry.release(runner.config.source, sym, runner.config.timeframe)
            # A runner whose feed no longer exists (policy flipped mid-life)
            # must not break removal — positions/ledger state matters more.
            try:
                feed = self._feed_for(runner.config.source)
            except ValueError:
                feed = self.mstock_feed
            feed.remove_symbols(runner.config.symbols)
            self._sync_mstock_thread()
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
            self._persist_state()
            try:
                self._audit_log(
                    f"DELETE {runner.config.name}",
                    scope=bucket,
                    instance_id=instance_id,
                    detail=f"bucket={bucket}",
                )
            except Exception:
                pass
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
            state = runner.get_state()
            # Gap #1: start/stop flips change whether any mstock runner is
            # live — keep the shared poll thread in step.
            if runner.config.source == "mstock":
                self._sync_mstock_thread()
            try:
                bucket = self._runner_bucket(runner)
                self._audit_log(
                    f"{action.upper()} {runner.config.name}",
                    scope=bucket,
                    instance_id=instance_id,
                    detail=f"action={action}",
                )
            except Exception:
                pass
            self._persist_state()
            return state

    # ------------------------------------------------------------------ #
    # Live Order Management (2026-09-23)
    # ------------------------------------------------------------------ #
    #
    # Two surfaces the command center reads/writes:
    #
    # * **positions** — one flat, action-ready row per open position across
    #   runners (equity tickets AND option structures), so the positions table
    #   stops being a runner-summary and shows what is actually held.
    # * **orders** — the ledger's order flow (pending / filled / cancelled /
    #   rejected) with real slippage, so "did my order fill?" has an answer.
    #
    # Every write goes through here (not straight from the API) so it lands in
    # the audit log, honours the bucket scope, and persists the state file.

    def list_positions(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """Flat rows for every open position, optionally bucket-scoped.

        ``stale`` marks a row whose runner is not RUNNING: the P&L is a frozen
        last mark rather than a live one. The row is still returned — an
        operator must be able to close, stop or target a paused runner's
        position — but the UI can label it instead of implying it is live.
        """
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in VALID_INSTANCE_MODES:
                raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")
        with self._lock:
            runners = [
                r for r in self._runners.values() if mode is None or self._runner_bucket(r) == mode
            ]
        rows: List[Dict[str, Any]] = []
        for runner in runners:
            base = {
                "instance_id": runner.instance_id,
                "runner": runner.config.name,
                "strategy_name": runner.config.strategy_name,
                "mode": runner.config.mode,
                "source": runner.config.source,
                # Multi-broker Phase B: positions carry their broker/segment
                # so manual close routes to the position's broker (LOM rule).
                "broker": self._runner_broker(runner),
                "segment": runner.config.segment,
                "status": runner.status,
                "stale": runner.status != STATUS_RUNNING,
                "target_label": runner.target_label,
            }
            try:
                for row in runner.positions_detail():
                    rows.append(
                        {
                            **base,
                            "mark_ts": None,
                            "mark_stale": False,
                            "quote_error": None,
                            **row,
                        }
                    )
            except Exception:  # noqa: BLE001 — one bad book must not blank the table
                logger.exception("positions_detail failed for %s", runner.instance_id[:8])
            summary = runner.options_summary() or {}
            for row in summary.get("open_structures_detail") or []:
                rows.append(
                    {
                        **base,
                        "position_key": row.get("position_key") or row.get("structure_id"),
                        "kind": "option",
                        "symbol": row.get("symbol"),
                        "label": row.get("label"),
                        "side": row.get("side"),
                        "qty": row.get("qty"),
                        "units": row.get("units"),
                        "entry_price": row.get("entry_price"),
                        "current_price": row.get("current_price"),
                        "unrealized_pnl": row.get("unrealized_pnl"),
                        "pnl_pct": row.get("open_pnl_pct"),
                        "entry_ts": row.get("entry_ts"),
                        # R1: mark freshness contract on position rows
                        "mark_ts": row.get("mark_ts"),
                        "mark_stale": bool(row.get("mark_stale")),
                        "quote_error": row.get("quote_error"),
                        "mtm_failures": int(row.get("mtm_failures") or 0),
                        "stop_loss": row.get("stop_loss"),
                        "target": row.get("target"),
                        "structure_type": row.get("structure_type"),
                        "strikes": row.get("strikes"),
                        "expiry": row.get("expiry"),
                        "bars_held": row.get("bars_held"),
                        "net_delta": row.get("net_delta"),
                        "net_theta": row.get("net_theta"),
                        "greeks_source": row.get("greeks_source"),
                        "legs": row.get("legs_detail") or [],
                        # Multi-leg structures close atomically (V1).
                        "can_partial_close": False,
                    }
                )
        return rows

    # ------------------------------------------------------------------ #
    # MTM Staleness Watchdog & Manual Refresh (R6)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_mtm_watchdog_enabled() -> bool:
        """True unless ``MTM_WATCHDOG`` env flag disables it (``off``/``0``/``false``)."""
        flag = os.environ.get("MTM_WATCHDOG", "on").strip().lower()
        return flag not in ("off", "0", "false", "no", "disabled")

    @staticmethod
    def _in_trading_window(now_utc: Optional[datetime] = None) -> bool:
        """NSE trading-window check (09:15–15:30 IST, Mon–Fri) — R6 / AC #9."""
        from backtest.data.mstock_live_feed import _market_open

        return bool(_market_open(now_utc))

    def _start_mtm_watchdog(self) -> None:
        if not self._is_mtm_watchdog_enabled():
            return
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        thread = threading.Thread(
            target=self._mtm_watchdog_loop,
            name="mtm-watchdog",
            daemon=True,
        )
        self._watchdog_thread = thread
        thread.start()

    def _mtm_watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(timeout=max(1.0, float(self.mtm_watchdog_interval_s))):
            try:
                self.run_mtm_watchdog()
            except Exception:  # noqa: BLE001 — watchdog must never crash
                logger.exception("[mtm-watchdog] periodic pass failed")

    def _repair_runner_marks_internal(
        self,
        runner: StrategyRunner,
        *,
        force: bool,
        now_utc: Optional[datetime] = None,
        max_age_s: float = 120.0,
    ) -> Dict[str, Any]:
        """Shared mark-repair + audit routine called by both timer and manual trigger (R6)."""
        res = runner.refresh_option_marks(
            force=force,
            now_utc=now_utc,
            max_age_s=max_age_s,
        )
        if force or int(res.get("checked") or 0) > 0:
            self._audit_log(
                f"REFRESH_MARKS {runner.config.name}",
                scope=self._runner_bucket(runner),
                instance_id=runner.instance_id,
                detail=(
                    f"checked={res.get('checked', 0)} "
                    f"repaired={res.get('repaired', 0)} "
                    f"still_failing={res.get('still_failing', 0)} "
                    f"mark_stale={res.get('mark_stale', False)}"
                ),
            )
            self._persist_state()
        return res

    def run_mtm_watchdog(
        self,
        *,
        now_utc: Optional[datetime] = None,
        max_age_s: float = 120.0,
    ) -> Dict[str, Any]:
        """Periodic 60s position watchdog pass (R6).

        * Respects ``MTM_WATCHDOG=on/off`` config flag.
        * Silent outside market hours (AC #9: zero broker calls, zero notifications).
        * For every RUNNING option runner with open positions where
          ``mark_age > 2 min`` (or failing marks), runs the shared repair routine.
        """
        if not self._is_mtm_watchdog_enabled():
            return {
                "skipped": True,
                "reason": "watchdog_disabled",
                "checked": 0,
                "repaired": 0,
                "still_failing": 0,
                "runners": [],
            }
        if not self._in_trading_window(now_utc):
            return {
                "skipped": True,
                "reason": "outside_market_hours",
                "checked": 0,
                "repaired": 0,
                "still_failing": 0,
                "runners": [],
            }
        with self._lock:
            runners = [
                r
                for r in self._runners.values()
                if r.status == STATUS_RUNNING and r.options_bridge is not None
            ]
        checked_total = 0
        repaired_total = 0
        failing_total = 0
        runner_results: List[Dict[str, Any]] = []
        for runner in runners:
            res = self._repair_runner_marks_internal(
                runner,
                force=False,
                now_utc=now_utc,
                max_age_s=max_age_s,
            )
            if int(res.get("checked") or 0) > 0:
                checked_total += int(res.get("checked") or 0)
                repaired_total += int(res.get("repaired") or 0)
                failing_total += int(res.get("still_failing") or 0)
                runner_results.append(
                    {"instance_id": runner.instance_id, "name": runner.config.name, **res}
                )
        return {
            "skipped": False,
            "checked": checked_total,
            "repaired": repaired_total,
            "still_failing": failing_total,
            "runners": runner_results,
        }

    def refresh_runner_marks(
        self,
        instance_id: str,
        *,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Manual operator mark refresh with ~10s per-runner rate-limit (R6/R7, AC #7)."""
        with self._lock:
            runner = self._runners.get(instance_id)
            if runner is None:
                raise KeyError(f"unknown runner: {instance_id}")
            now_mono = time.monotonic()
            last_mono = self._last_manual_refresh_mono.get(instance_id)
            cooldown = float(self._refresh_marks_cooldown_s)
            if last_mono is not None and (now_mono - last_mono) < cooldown:
                retry_after = round(cooldown - (now_mono - last_mono), 2)
                bridge = runner.options_bridge
                return {
                    "instance_id": runner.instance_id,
                    "runner": runner.config.name,
                    "cooldown": True,
                    "rate_limited": True,
                    "retry_after_s": retry_after,
                    "checked": 0,
                    "repaired": 0,
                    "still_failing": 0,
                    "mark_stale": bool(bridge.mark_stale) if bridge is not None else False,
                    "mark_ts": bridge.mark_ts if bridge is not None else None,
                    "quote_error": bridge.quote_error if bridge is not None else None,
                    "message": f"cooldown active — retry in {max(1, int(round(retry_after)))}s",
                }
            self._last_manual_refresh_mono[instance_id] = now_mono

        res = self._repair_runner_marks_internal(
            runner,
            force=True,
            now_utc=now_utc,
            max_age_s=0.0,
        )
        return {
            "instance_id": runner.instance_id,
            "runner": runner.config.name,
            "cooldown": False,
            "rate_limited": False,
            "retry_after_s": 0.0,
            **res,
        }

    def position_action(self, instance_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Apply one manual action to one position and audit it.

        ``action`` ∈ ``modify_stop_loss`` | ``modify_target`` |
        ``clear_stop_loss`` | ``clear_target`` | ``close_fraction`` |
        ``close_all``. Raises ``KeyError`` for an unknown runner/position and
        ``ValueError`` for a bad level or fraction — the API maps those onto
        404 / 409 so the UI can show the reason verbatim.
        """
        action = str(payload.get("action", "")).strip().lower()
        key = str(payload.get("position_key") or payload.get("symbol") or "").strip()
        with self._lock:
            runner = self._runners.get(instance_id)
            if runner is None:
                raise KeyError(f"unknown runner: {instance_id}")
            if not key:
                raise ValueError("position_key (or symbol) is required")

            if action in ("modify_stop_loss", "modify_target"):
                field = RULE_STOP_LOSS if action.endswith("stop_loss") else RULE_TARGET
                value = payload.get("value", payload.get("price"))
                if value is None:
                    raise ValueError(f"{action} needs a 'value' price")
                result = runner.set_position_rule(key, field, value)
            elif action in ("clear_stop_loss", "clear_target"):
                field = RULE_STOP_LOSS if action.endswith("stop_loss") else RULE_TARGET
                result = runner.set_position_rule(key, field, None)
            elif action == "close_fraction":
                result = runner.close_position(key, payload.get("fraction", 0.5))
            elif action in ("close_all", "close_position"):
                result = runner.close_position(key, 1.0)
            else:
                raise ValueError(f"unknown position action: {action}")

            bucket = self._runner_bucket(runner)
            detail = (
                f"{action} {key}"
                + (f" = {result.get('value')}" if result.get("value") is not None else "")
                + (f" qty={result.get('qty_closed')}" if result.get("qty_closed") else "")
            )
            self._audit_log(
                f"MANUAL_{action.upper()} · {runner.config.name}",
                scope=bucket,
                instance_id=instance_id,
                detail=detail,
            )
            self._persist_state()
        result["runner"] = runner.get_state()
        return result

    def list_orders(
        self,
        mode: Optional[str] = None,
        instance_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """The ledger's order flow + a summary strip (Orders tab).

        Scope rules: an explicit ``instance_id`` wins; otherwise ``mode``
        narrows to that bucket's runners; otherwise the whole ledger. Orders
        are only attributed to a bucket through their runner, so a ledger row
        whose runner was removed simply stops appearing — it is not silently
        reassigned.
        """
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in VALID_INSTANCE_MODES:
                raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 200
        with self._lock:
            if instance_id:
                if instance_id not in self._runners:
                    raise KeyError(f"unknown runner: {instance_id}")
                instances = [instance_id]
            elif mode is not None:
                instances = [r.instance_id for r in self._bucket_runners(mode)]
            else:
                instances = None  # no filter — the whole ledger

            if instances is None:
                orders = self.ledger.snapshot(statuses=statuses, limit=limit)
                summary = self.ledger.summary()
            else:
                orders, summary = [], None
                # Fetch newest-first from the ledger, then keep only the
                # in-scope rows, so the limit never hides a scoped order
                # behind unrelated traffic.
                for coid_owner in list(instances):
                    rows = self.ledger.snapshot(
                        instance_id=coid_owner, statuses=statuses, limit=limit
                    )
                    orders.extend(rows)
                orders.sort(
                    key=lambda o: (o.get("created_ts") or "", o["client_order_id"]),
                    reverse=True,
                )
                orders = orders[:limit]
                summary = self._scoped_orders_summary(instances)

            labels = {
                r.instance_id: {
                    "runner": r.config.name,
                    "strategy_name": r.config.strategy_name,
                    "mode": r.config.mode,
                    # Multi-broker Phase B: the Orders tab gains a broker
                    # column (audit: WHICH venue each order went to).
                    "broker": self._runner_broker(r),
                    "segment": r.config.segment,
                    "symbols": list(r.config.symbols),
                }
                for r in self._runners.values()
            }
        for row in orders:
            row["runner"] = labels.get(row["instance_id"], {}).get("runner")
            row["strategy_name"] = labels.get(row["instance_id"], {}).get("strategy_name")
            row["mode"] = labels.get(row["instance_id"], {}).get("mode")
            row["broker"] = labels.get(row["instance_id"], {}).get("broker")
            row["segment"] = labels.get(row["instance_id"], {}).get("segment")
        return {"orders": orders, "summary": summary}

    def _scoped_orders_summary(self, instances: List[str]) -> Dict[str, Any]:
        """Ledger summary restricted to a set of runners."""
        totals: Dict[str, Any] = {
            "total": 0,
            "pending": 0,
            "filled": 0,
            "cancelled": 0,
            "rejected": 0,
            "status_counts": {},
            "fills": 0,
            "avg_slippage": 0.0,
            "avg_slippage_pct": 0.0,
            "worst_slippage": 0.0,
            "slippage_samples": 0,
            "oldest_pending_age_s": 0.0,
            # Phase 3 order aging. Shipped on BOTH summary paths (scoped and
            # whole-ledger) so the Orders tab's badge and strip cannot show
            # different aging counts than the rows they sit above.
            "aging_warn_s": ORDER_AGING_WARN_S,
            "aging_alert_s": ORDER_AGING_ALERT_S,
            "aging_warn_count": 0,
            "aging_alert_count": 0,
            "oldest_pending_coid": None,
            "oldest_pending_symbol": None,
        }
        slips: List[float] = []
        slips_pct: List[float] = []
        for instance_id in instances:
            rows = self.ledger.snapshot(instance_id=instance_id, limit=1000)
            for row in rows:
                totals["total"] += 1
                # Statuses are stored upper-case (ORDER_PENDING & co). Lower-
                # casing the key here made every scoped count read 0 while the
                # unscoped ledger summary read the truth — the same order,
                # two different answers depending on the tab's scope.
                status = str(row.get("status", "")).upper()
                if status in totals["status_counts"]:
                    totals["status_counts"][status] += 1
                else:
                    totals["status_counts"][status] = 1
                if row.get("slippage") is not None:
                    slips.append(float(row["slippage"]))
                if row.get("slippage_pct") is not None:
                    slips_pct.append(float(row["slippage_pct"]))
                if row.get("status") == ORDER_PENDING:
                    age = self.ledger.age_seconds(row.get("created_ts"))
                    if age >= totals["oldest_pending_age_s"]:
                        totals["oldest_pending_age_s"] = age
                        totals["oldest_pending_coid"] = row.get("client_order_id")
                        totals["oldest_pending_symbol"] = row.get("symbol")
                    band = self.ledger.aging_band(age)
                    if band:
                        totals[f"aging_{band}_count"] += 1
        counts = totals["status_counts"]
        totals["pending"] = counts.get(ORDER_PENDING, 0)
        totals["filled"] = counts.get(ORDER_FILLED, 0)
        totals["cancelled"] = counts.get(ORDER_CANCELLED, 0)
        totals["rejected"] = counts.get(ORDER_REJECTED, 0)
        totals["fills"] = totals["filled"]
        totals["slippage_samples"] = len(slips)
        totals["avg_slippage"] = round(sum(slips) / len(slips), 6) if slips else 0.0
        totals["avg_slippage_pct"] = round(sum(slips_pct) / len(slips_pct), 8) if slips_pct else 0.0
        totals["worst_slippage"] = round(max(slips), 6) if slips else 0.0
        totals["oldest_pending_age_s"] = round(totals["oldest_pending_age_s"], 1)
        return totals

    def check_order_aging(self) -> List[Dict[str, Any]]:
        """Raise an alert for every working order that has aged past a band.

        An aged order is the quietest failure in a trading system: nothing
        throws, the book simply never moves, and the operator's attention is
        on the P&L rather than on the absence of a fill. This sweep gives each
        order ONE alert per band (warn, then alert) — an alert that repeats
        every tick is noise the operator learns to ignore, which is worse than
        no alert at all.

        Returns the alerts raised by this call. Never raises: it runs inside
        the tick loop, where a monitoring failure must not stop trading.
        """
        raised: List[Dict[str, Any]] = []
        try:
            rows = self.ledger.snapshot(statuses=[ORDER_PENDING], limit=1000)
        except Exception:  # noqa: BLE001 — monitoring never breaks the tick
            logger.exception("order-aging sweep could not read the ledger")
            return raised
        live: set = set()
        for row in rows:
            coid = row.get("client_order_id")
            band = row.get("aging") or ""
            live.add(coid)
            if not band:
                continue
            seen = self._aging_alerted.setdefault(coid, set())
            if band in seen:
                continue
            seen.add(band)
            runner = self._runners.get(row.get("instance_id"))
            bucket = self._runner_bucket(runner) if runner is not None else "paper"
            age = float(row.get("age_s") or 0.0)
            alert = {
                "client_order_id": coid,
                "instance_id": row.get("instance_id"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "quantity": row.get("quantity"),
                "age_s": age,
                "band": band,
                "scope": bucket,
            }
            raised.append(alert)
            level = logging.WARNING if band == "alert" else logging.INFO
            logger.log(
                level,
                "[orders] %s aged %gs (%s %s x%g) — still working, coid=%s",
                band.upper(),
                age,
                row.get("side"),
                row.get("symbol"),
                float(row.get("quantity") or 0.0),
                coid,
            )
            try:
                self._audit_log(
                    f"ORDER_{band.upper()}ING · {row.get('symbol')}",
                    scope=bucket,
                    instance_id=row.get("instance_id"),
                    detail=(
                        f"coid={coid} working {age:.0f}s without a fill "
                        f"({row.get('side')} {float(row.get('quantity') or 0):g})"
                    ),
                )
            except Exception:  # noqa: BLE001 — an audit hiccup is not a trade
                logger.exception("order-aging audit failed for %s", coid)
        # Forget orders that filled/cancelled so a later, second-order problem
        # (same symbol, new coid) is not suppressed by a stale memory.
        for coid in [c for c in self._aging_alerted if c not in live]:
            self._aging_alerted.pop(coid, None)
        return raised

    def _orders_summary_for(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """Ledger summary scoped to one bucket (whole ledger when unscoped).

        Both read paths use this: the REST ``/orders`` call and the SSE
        snapshot that seeds the Orders-tab badge. They must agree, or the
        badge nudges about work the page cannot show.
        """
        if mode is None:
            return self.ledger.summary()
        return self._scoped_orders_summary([r.instance_id for r in self._bucket_runners(mode)])

    def cancel_order(self, client_order_id: str) -> Dict[str, Any]:
        """Cancel a still-pending order; audited like every other control.

        Only PENDING orders can be cancelled — a filled order is history, and
        pretending otherwise in the UI would be a lie. Raises ``KeyError`` for
        an unknown id and ``ValueError`` when the order is already terminal.

        A LIVE working order is cancelled **at the venue first**: marking it
        cancelled locally while it still rests at the broker is how a position
        opens after the operator was told the order was dead. If the venue
        refuses, the refusal propagates and nothing changes locally.
        """
        order = self.ledger.get_order(client_order_id)
        if order is None:
            raise KeyError(f"unknown order: {client_order_id}")
        if order.status != ORDER_PENDING:
            raise ValueError(
                f"order {client_order_id} is {order.status} — only PENDING orders can be cancelled"
            )
        runner = self._runners.get(order.instance_id)
        at_venue = ""
        if order.broker_order_id is not None:
            # Multi-broker Phase C: cancel routes to the ORDER's broker —
            # the gateway that is tracking this coid.
            gateway = self._gateway_tracking(client_order_id)
            if gateway is None:
                # The venue order exists but this process is not tracking it
                # (restored state, or the gateway was rebuilt). Cancelling
                # locally would leave it live at the broker.
                raise ValueError(
                    f"order {client_order_id} is working at the venue "
                    f"({order.broker_order_id}) but is not tracked by this process — "
                    "reconcile with the broker before cancelling"
                )
            gateway.cancel_working(client_order_id)
            at_venue = f" at venue {order.broker_order_id}"
        elif not self.ledger.cancel(client_order_id):
            raise ValueError(f"order {client_order_id} could not be cancelled")
        bucket = self._runner_bucket(runner) if runner is not None else "paper"
        self._audit_log(
            f"ORDER_CANCELLED · {order.symbol}",
            scope=bucket,
            instance_id=order.instance_id,
            detail=f"coid={client_order_id} (was {order.quantity:g} {order.side}){at_venue}",
        )
        self._persist_state()
        return self.ledger.row_with_age(self.ledger.get_order(client_order_id))

    def modify_order(
        self,
        client_order_id: str,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Amend a working order (Phase 3); audited like every other control.

        Only a LIVE working order can be amended: a paper order fills or
        rejects within the same call, so there is never anything resting to
        change. Saying so is more useful than accepting an amendment that
        silently applies to nothing.

        Raises ``KeyError`` for an unknown id, ``ValueError`` for a terminal or
        paper order / a nonsense value, and ``OrderRefused`` when the venue
        refuses the amendment (nothing changes locally in that case).
        """
        order = self.ledger.get_order(client_order_id)
        if order is None:
            raise KeyError(f"unknown order: {client_order_id}")
        if order.status != ORDER_PENDING:
            raise ValueError(
                f"order {client_order_id} is {order.status} — only a working order "
                "can be amended"
            )
        if quantity is None and limit_price is None:
            raise ValueError("modify needs a new quantity and/or limit price")
        runner = self._runners.get(order.instance_id)
        bucket = self._runner_bucket(runner) if runner is not None else "paper"
        if order.broker_order_id is None:
            raise ValueError(
                f"order {client_order_id} is a paper order — it fills or rejects "
                "immediately, so there is nothing at a venue to amend; cancel it "
                "and let the strategy re-arm"
            )
        # Multi-broker Phase C: amend routes to the ORDER's broker — the
        # gateway that is tracking this coid.
        gateway = self._gateway_tracking(client_order_id)
        if gateway is None:
            raise ValueError(
                f"order {client_order_id} is working at the venue "
                f"({order.broker_order_id}) but is not tracked by this process — "
                "reconcile with the broker before amending"
            )

        before = (order.quantity, order.limit_price)
        result = gateway.modify_working(client_order_id, quantity=quantity, limit_price=limit_price)
        after = (result["quantity"], result["limit_price"])
        self._audit_log(
            f"ORDER_AMENDED · {order.symbol}",
            scope=bucket,
            instance_id=order.instance_id,
            detail=(
                f"coid={client_order_id} qty {before[0]:g}→{after[0]:g} "
                f"limit {before[1]}→{after[1]}"
            ),
        )
        self._persist_state()
        return {**result, "order": self.ledger.row_with_age(self.ledger.get_order(client_order_id))}

    def pause_all(self, mode: Optional[str] = None) -> int:
        """Pause runners. ``mode=None`` pauses all; mode='paper'|'live' pauses only that bucket."""
        with self._lock:
            targets = self._bucket_runners(mode) if mode else list(self._runners.values())
            n = 0
            for runner in targets:
                if runner.status == STATUS_RUNNING:
                    runner.pause()
                    n += 1
            self._sync_mstock_thread()
            logger.warning("Paused %d runners%s", n, f" [{mode}]" if mode else "")
            self._persist_state()
            return n

    def resume_all(self, mode: Optional[str] = None) -> int:
        """Resume runners.

        ``mode=None`` resumes all; mode='paper'|'live' resumes only that bucket.
        """
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
            targets = self._bucket_runners(mode) if mode else list(self._runners.values())
            n = 0
            for runner in targets:
                if runner.status == STATUS_PAUSED:
                    runner.resume()
                    n += 1
            # 2026-09-23: resume flips runners to RUNNING — the shared mStock
            # poll thread must follow, or nothing feeds bars (the "prices are
            # frozen at 10:56" symptom after a restart + resume_all).
            self._sync_mstock_thread()
            logger.info("Resumed %d runners%s", n, f" [{mode}]" if mode else "")
            self._persist_state()
            return n

    def stop_all(self, mode: Optional[str] = None) -> int:
        """Stop runners. ``mode=None`` stops all; mode='paper'|'live' stops only that bucket."""
        with self._lock:
            targets = self._bucket_runners(mode) if mode else list(self._runners.values())
            n = 0
            for runner in targets:
                if runner.status != STATUS_STOPPED:
                    runner.stop()
                    n += 1
            # Gap #1: stopping the last mstock runner retires the poll thread.
            self._sync_mstock_thread()
            self._persist_state()
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

            targets = self._bucket_runners(mode) if mode else list(self._runners.values())
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

            # Merge: also flatten the dashboard book (second book) — this is
            # why flatten missed option positions before.
            try:
                dashboard_closed = self.flatten_dashboard_book(reason=reason)
                count += dashboard_closed
            except Exception:
                pass

            logger.critical(
                "EMERGENCY FLATTEN%s: %d positions closed across %d runners",
                f" [{mode}]" if mode else "",
                count,
                len(targets),
            )
            try:
                scope = mode or "all"
                self._audit_log(
                    f"EMERGENCY_FLATTEN {reason}",
                    scope=scope,
                    detail=f"closed={count} mode={scope}",
                )
            except Exception:
                pass
            self._evaluate_risk()
            self._persist_state()
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
            self._persist_state()

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
                # Master reset clears every latch — paper/live buckets AND
                # each `segment:<name>` latch (Phase D).
                for m in list(self._bucket_halted.keys()):
                    self._bucket_halted[m] = False
                    self._bucket_halt_reason[m] = None
                    self._bucket_halt_mode[m] = None
                    self._bucket_halted_ts[m] = None
                logger.info("Circuit breaker reset (all buckets + segments)")
                self._persist_state()
                try:
                    self._audit_log("RESET_BREAKER all", scope="all", detail="master reset")
                except Exception:
                    pass
                self._resolve_breaker_alerts(["portfolio", "bucket:paper", "bucket:live"])
            else:
                # Scoped reset: one bucket or one `segment:<name>` latch.
                mode = str(mode).strip().lower()
                if mode not in VALID_INSTANCE_MODES and not mode.startswith("segment:"):
                    raise ValueError(
                        f"mode must be one of {VALID_INSTANCE_MODES} or 'segment:<name>', "
                        f"got {mode!r}"
                    )
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
                self._persist_state()
                try:
                    self._audit_log(f"RESET_BREAKER {mode}", scope=mode, detail=f"bucket={mode}")
                except Exception:
                    pass
                cleared = [f"bucket:{mode}"]
                if not self.halted:
                    # Manager-level was cleared alongside (no buckets left halted)
                    cleared.append("portfolio")
                self._resolve_breaker_alerts(cleared)
            self._refresh_anchors()

    # ------------------------------------------------------------------ #
    # Tick dispatch
    # ------------------------------------------------------------------ #

    @staticmethod
    def _bar_date(bar: Dict[str, Any]) -> str:
        """Extract YYYY-MM-DD from a bar's timestamp."""
        return str(bar.get("ts", ""))[:10] or datetime.now(timezone.utc).date().isoformat()

    def _sync_mstock_thread(self) -> None:
        """Run the live poll threads iff at least one live-source runner exists.

        The threads are expensive (API polls) — they must not idle-spin while
        no runner consumes live bars, and they must be running before the
        first live runner's bars are expected. 2026-09-25: extended to the
        Dhan thread (source=dhan) alongside the mStock thread.
        """
        for source, feed in (("mstock", self.mstock_feed), ("dhan", self.dhan_feed)):
            has_live = any(
                r.config.source == source and r.status != STATUS_STOPPED
                for r in self._runners.values()
            )
            if has_live and not feed.running:
                feed.start()
            elif not has_live and feed.running:
                feed.stop()

    # ------------------------------------------------------------------ #
    # State persistence (Gap #3 / V2 — P2.4). No-op unless a state path is
    # configured. Saves are atomic and cheap (control-plane rate, plus one
    # write per 60 ticks for fresh scalars); a failure NEVER breaks trading.
    # ------------------------------------------------------------------ #

    def _snapshot_payload(self) -> Dict[str, Any]:
        """Everything needed to rehydrate a manager: anchors, breaker
        latches, and each runner's config + full book."""
        runners = [capture_runner(r) for r in self._runners.values()]
        return {
            "manager": {
                "total_capital": self.total_capital,
                "peak_equity": self.peak_equity,
                "day_start_equity": self._day_start_equity,
                "current_day": self._current_day,
                "halted": self.halted,
                "halt_reason": self.halt_reason,
                "halt_mode": self.halt_mode,
                "halted_ts": self.halted_ts,
                "tick_count": self.tick_count,
                "tick_index": self.tick_index,
            },
            "buckets": {
                mode: {
                    "peak": self._bucket_peak.get(mode, 0.0),
                    "halted": self._bucket_halted.get(mode, False),
                    "halt_reason": self._bucket_halt_reason.get(mode),
                    "halt_mode": self._bucket_halt_mode.get(mode),
                    "halted_ts": self._bucket_halted_ts.get(mode),
                    "day_start": self._bucket_day_start.get(mode, 0.0),
                    "day": self._bucket_day.get(mode),
                }
                for mode in ("paper", "live")
            },
            "runners": runners,
        }

    def save_state(self) -> bool:
        """Persist now (public — also usable from ops/API). Returns success."""
        if self._state_store is None:
            return False
        try:
            self._state_store.save(self._snapshot_payload())
            return True
        except Exception:  # noqa: BLE001 — persistence must never break trading
            logger.exception("portfolio state save failed (%s)", self._state_store.path)
            return False

    def persist_risk_config(self) -> None:
        """Best-effort snapshot save after a live risk-config change.

        The supervisor's breaker limits live in-process only; persisting the
        full snapshot keeps the state file in sync with whatever config the
        operator just set, so a restart does not silently revert anything
        the snapshot already carries. Bucket-limit overrides belong in run
        config (``risk.buckets.<bucket>``) for durable changes.
        """
        try:
            self.save_state()
        except Exception:  # noqa: BLE001 — a config save must never 500 the API
            logger.exception("risk config persist failed")

    def get_risk_config(self, mode: str = "paper") -> GlobalRiskConfig:
        """Get risk config for specific mode (paper or live)."""
        return self._risk_configs.get(mode, self.supervisor.config)

    def set_risk_config(self, mode: str, values: Dict[str, Any]) -> List[str]:
        """Update risk config for specific mode and return changed fields."""
        if mode not in ("paper", "live"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'paper' or 'live'")

        config = self._risk_configs[mode]
        changed = config.update_limits(values)

        # If this is the active mode, update the supervisor too
        # (for now, supervisor uses paper config by default)
        if mode == "paper":
            self.supervisor.config = config

        return changed

    def get_all_risk_configs(self) -> Dict[str, Dict[str, Any]]:
        """Get all risk configs as dicts for API response."""
        result = {}
        for mode, config in self._risk_configs.items():
            result[mode] = {
                "daily_loss_limit": config.daily_loss_limit,
                "max_drawdown_pct": config.max_drawdown_pct,
                "max_leverage": config.max_leverage,
                "breach_mode": config.breach_mode,
                "correlation_warning_threshold": config.correlation_warning_threshold,
            }
        return result

    def _persist_state(self) -> None:
        if self._restoring:
            return  # never re-write the file we are reading from
        self.save_state()

    def _restore_state(self) -> None:
        """Rehydrate configs, books and breaker latches from the state file.

        Runners saved as RUNNING come back PAUSED (fail-closed). Any failure
        logs loudly and boots clean — persistence must never prevent a start.
        """
        if self._state_store is None:
            return
        payload = self._state_store.load()
        if not payload:
            return
        try:
            self._restoring = True
            try:
                self._apply_state_payload(payload)
            finally:
                self._restoring = False
            logger.warning(
                "[state] restored %d runner(s) + breaker latches from %s — "
                "restored runners are PAUSED; resume explicitly",
                len(payload.get("runners", [])),
                self._state_store.path,
            )
        except Exception:  # noqa: BLE001 — a bad restore must not block boot
            logger.exception(
                "[state] restore from %s failed — starting clean", self._state_store.path
            )

    def _apply_state_payload(self, payload: Dict[str, Any]) -> None:
        # Runners FIRST (add_runner re-subscribes feeds + the registry and
        # re-books capital + recomputes anchors), then manager scalars and
        # breaker latches override whatever add_runner initialised.
        for runner_snap in payload.get("runners", []):
            from backtest.forward.state_store import _config_from_dict

            config = _config_from_dict(runner_snap["config"])
            try:
                self.add_runner(config, start=False)
            except ValueError as exc:
                # e.g. a mode='live' runner with no armed broker session this
                # boot — fail-closed: skip THAT runner, never block the boot.
                logger.warning(
                    "[state] runner %s (%s) refused on restore: %s — skipped",
                    config.instance_id,
                    config.name,
                    exc,
                )
                continue
            runner = self._runners.get(config.instance_id)
            if runner is None:
                logger.warning(
                    "[state] runner %s could not be re-created — skipped",
                    config.instance_id,
                )
                continue
            restore_runner(runner, runner_snap)

        manager = payload.get("manager", {})
        self.total_capital = float(manager.get("total_capital", 0.0))
        self.peak_equity = float(manager.get("peak_equity", 0.0))
        self._day_start_equity = float(manager.get("day_start_equity", 0.0))
        self._current_day = manager.get("current_day")
        self.halted = False
        self.halt_reason = None
        self.halt_mode = None
        self.halted_ts = None
        self.tick_count = int(manager.get("tick_count", 0))
        self.tick_index = int(manager.get("tick_index", 0))
        # 2026-09-23: halt latches are deliberately NOT restored. Yesterday's
        # emergency-stop latched the breaker into the state file and it
        # re-armed on every boot — the dashboard showed "🔴 HALTED: Emergency
        # flatten (user requested full stop)" forever despite fresh trading.
        # A halt is a session-scoped circuit state: it guards THIS session's
        # P&L, so it starts clean each boot (fail-safe for trading, unlike
        # fail-closed for the boot — owner hit this mid-testing).
        # Same rule for the DAY ANCHOR + PEAK: a saved day_start from a
        # previous session is a GHOST — the book may have been rebuilt since
        # (the ₹1.73 Cr anchor vs a ₹20 L book computed daily P&L of
        # -₹1.53 Cr and tripped the global breaker instantly). The anchor is
        # re-baselined to the restored book on the first bar (see _on_bar).
        self._day_start_equity = 0.0
        self.peak_equity = 0.0
        self._current_day = None
        self._bucket_day_start = {"paper": 0.0, "live": 0.0}
        self._bucket_day = {"paper": None, "live": None}

        for mode, bucket in payload.get("buckets", {}).items():
            self._bucket_peak[mode] = 0.0  # session-scoped like the day anchor
            self._bucket_halted[mode] = False
            self._bucket_halt_reason[mode] = None
            self._bucket_halt_mode[mode] = None
            self._bucket_halted_ts[mode] = None
            # day_start / day stay session-scoped (re-baselined on first bar)

    def _on_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        """Fan one closed candle out to every runner trading that symbol."""
        self._on_bar_core(symbol, bar)
        intel = self.intelligence
        if intel is not None:
            try:
                intel.on_bar(symbol, bar)
            except Exception:  # noqa: BLE001 — analytics never break the bar
                logger.debug("intelligence on_bar failed", exc_info=True)

    def _on_bar_core(self, symbol: str, bar: Dict[str, Any]) -> None:
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

            # SOURCE ISOLATION (2026-09-30): every feed stamps its bars with
            # ``_source`` ("synthetic" | "mstock" | "dhan"). A bar may only
            # drive runners whose ``config.source`` matches — otherwise the
            # 1s synthetic random-walk feed (started by ANY synthetic-source
            # runner on a symbol) sprays fake bars into every paper/mstock
            # runner on that same symbol, marking their option books off a
            # synthetic spot and manufacturing phantom P&L. ``bar_source is
            # None`` (hand-built bars in tests / legacy callers) preserves the
            # old symbol-only behaviour. Popped so the tag never leaks into a
            # runner's candle buffer / DataFrame / persisted state.
            bar_source = bar.pop("_source", None)
            for runner in self._runners.values():
                if symbol.upper() not in [s.upper() for s in runner.config.symbols]:
                    continue
                if not self._source_matches(runner, bar_source):
                    continue
                runner.process_candle_event(symbol, bar)

            # GAP-4: flush closed trades the moment they close (a bar can exit
            # a structure). The persister's dedupe makes this cheap — already-
            # flushed trades are skipped in memory without touching the DB.
            if self._trade_persister is not None and not self._restoring:
                for runner in self._runners.values():
                    if symbol.upper() not in [s.upper() for s in runner.config.symbols]:
                        continue
                    if not self._source_matches(runner, bar_source):
                        continue
                    try:
                        self._trade_persister.flush_runner(runner)
                    except Exception:  # noqa: BLE001 — never break the bar
                        logger.exception("trade flush failed for %s", runner.instance_id)

    @staticmethod
    def _source_matches(runner: "StrategyRunner", bar_source: Optional[str]) -> bool:
        """True when ``bar_source`` may drive ``runner`` (see _on_bar_core).

        ``bar_source is None`` → no tagging (legacy/test bars) → deliver to any
        runner holding the symbol. Otherwise the runner's configured source
        must equal the bar's originating feed source.
        """
        if bar_source is None:
            return True
        return str(getattr(runner.config, "source", "")).lower() == str(bar_source).lower()

    def _on_tick_end(self, tick_ts: str) -> None:
        """All symbols have their bar for this tick: run pool scans + risk."""
        self._on_tick_end_core(tick_ts)
        # Outside the manager lock: evaluation reads books one runner lock at
        # a time and strategy alert callbacks take their runner's lock.
        intel = self.intelligence
        if intel is not None:
            try:
                intel.on_tick_end()
            except Exception:  # noqa: BLE001 — analytics never break the tick
                logger.debug("intelligence on_tick_end failed", exc_info=True)

    def _on_tick_end_core(self, tick_ts: str) -> None:
        with self._lock:
            self.tick_index += 1
            for runner in self._runners.values():
                runner.on_tick_end(tick_ts)
            # Phase 3: an order that has been working for a minute without a
            # fill is the quietest failure mode there is — nothing throws, the
            # book just never moves. Sweep once per tick (the ledger only
            # reports a band once per order, so this is not spam).
            self.check_order_aging()
            self._evaluate_risk()
            # V2: bounded save rate. The %60 was tuned for 1s synthetic ticks
            # (one save/min); the live mStock feed ticks once per SWEEP (60s),
            # so %60 meant one save per HOUR (live-session finding 2026-09-21).
            # Every 5 sweeps ≈ 5 min on live, 5s on synthetic — bounded either way.
            if self._state_store is not None and self.tick_index % 5 == 0:
                self._persist_state()
            # GAP-4: flush newly closed trades to Postgres on the same cadence
            # — idempotent (dedupe in the persister), fail-soft.
            if self._trade_persister is not None and self.tick_index % 5 == 0:
                for runner in list(self._runners.values()):
                    try:
                        self._trade_persister.flush_runner(runner)
                    except Exception:  # noqa: BLE001 — never break the tick
                        logger.exception("trade flush failed for %s", runner.instance_id)

            # F-12: poll EVERY live gateway every tick — a fill sitting at a
            # venue must reach the book in the same loop that drives risk.
            # Multi-broker Phase C: fills poll per broker (each gateway owns
            # one broker's order book). Reconciliation (the honesty check vs
            # each broker's order book) runs on a slower cadence; neither may
            # ever break the tick.
            for gw_key, gateway in list(self._live_gateways.items()):
                if not gateway.working_count():
                    continue
                try:
                    gateway.poll_pending()
                except Exception:  # noqa: BLE001
                    logger.exception("[live:%s] gateway poll failed", gw_key)
                if self.tick_index % 300 == 0:
                    try:
                        gateway.reconcile()
                    except Exception:  # noqa: BLE001
                        logger.exception("[live:%s] gateway reconcile failed", gw_key)

    def _evaluate_risk(self) -> None:
        """Evaluate risk per-bucket (independent breakers) + manager-level.

        Each bucket gets its own risk evaluation. A breach in 'paper' does
        NOT halt 'live' — the breakers are independent (C1/C5). The manager
        also tracks a global halt for the master-kill path.
        """
        now_ts = datetime.now(timezone.utc).isoformat()
        # Buckets that tripped *in this pass* — the master alert is suppressed
        # when one of them already raised its own (no double notification).
        fresh_buckets = []

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
                fresh_buckets.append(mode)
                logger.critical(
                    "BUCKET HALT [%s]: %s (mode=%s)",
                    mode.upper(),
                    report.halt_reason,
                    report.halt_mode,
                )
                self._raise_breaker_alert(
                    scope="bucket",
                    bucket=mode,
                    reason=report.halt_reason,
                    halt_mode=report.halt_mode,
                    equity=equity,
                    daily_pnl=daily_pnl,
                    drawdown_pct=report.drawdown_pct,
                )
                # Only pause/flatten runners in THIS bucket
                for runner in bucket_runners:
                    if runner.status == STATUS_RUNNING:
                        runner.pause()
                if report.halt_mode == HALT_FLATTEN:
                    for runner in bucket_runners:
                        runner.flatten_all(reason=f"circuit_breaker_flatten_{mode}")

        # --- Per-segment risk evaluation (Phase D — narrower than buckets) ---
        # Same machinery, one slice per live segment: a breach halts THAT
        # segment's runners only (its broker's book), never the neighbours.
        try:
            from backtest.brokers.segments import get_segments_config

            segment_defs = get_segments_config().segments
        except Exception:  # noqa: BLE001 — a broken segments config never kills the tick
            segment_defs = {}
        for seg_name, seg_def in segment_defs.items():
            if not seg_def.risk:
                continue  # no per-segment limits configured
            seg_runners = self._segment_runners(seg_name)
            if not seg_runners:
                continue
            key = self._segment_key(seg_name)
            self._ensure_bucket_state(key)

            seg_equity = sum(r.equity() for r in seg_runners)
            if seg_equity > self._bucket_peak.get(key, 0.0):
                self._bucket_peak[key] = seg_equity
            if self._bucket_day_start.get(key, 0.0) <= 0:
                self._bucket_day_start[key] = seg_equity

            seg_daily_pnl = seg_equity - self._bucket_day_start.get(key, 0.0)
            seg_halted = self._bucket_halted.get(key, False)
            report = self._segment_risk_supervisor(seg_name, seg_def.risk).evaluate(
                runners=seg_runners,
                total_equity=seg_equity,
                peak_equity=self._bucket_peak[key],
                daily_pnl=seg_daily_pnl,
                already_halted=seg_halted,
            )
            if report.halted and not seg_halted:
                self._bucket_halted[key] = True
                self._bucket_halt_reason[key] = report.halt_reason
                self._bucket_halt_mode[key] = report.halt_mode
                self._bucket_halted_ts[key] = now_ts
                logger.critical(
                    "SEGMENT HALT [%s → %s]: %s (mode=%s)",
                    seg_name,
                    seg_def.broker,
                    report.halt_reason,
                    report.halt_mode,
                )
                # Only pause/flatten runners in THIS segment.
                for runner in seg_runners:
                    if runner.status == STATUS_RUNNING:
                        runner.pause()
                if report.halt_mode == HALT_FLATTEN:
                    for runner in seg_runners:
                        runner.flatten_all(reason=f"circuit_breaker_flatten_segment_{seg_name}")

        # --- Manager-level risk (combined view — for master kill) ---
        # GLOBAL breakers evaluate the SUM across every bucket, segment and
        # broker (PRD Phase D): a combined-loss breach halts EVERYTHING even
        # when each individual segment is inside its own limits.
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
            if not fresh_buckets:
                # Combined-book breach with no single bucket tripping: this is
                # its own alert. When a bucket tripped in this same pass, its
                # alert already covers the trader-facing notification.
                self._raise_breaker_alert(
                    scope="portfolio",
                    bucket=None,
                    reason=report.halt_reason,
                    halt_mode=report.halt_mode,
                    equity=equity,
                    daily_pnl=report.daily_pnl,
                    drawdown_pct=report.drawdown_pct,
                )
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

    def _raise_breaker_alert(
        self,
        *,
        scope: str,
        bucket: Optional[str],
        reason: str,
        halt_mode: str,
        equity: float,
        daily_pnl: float,
        drawdown_pct: float,
    ) -> None:
        """Publish a breaker trip as a platform alert (best-effort).

        The supervisor already logs trips; the alert broker is what the
        widget *and* the outbound notifier (Telegram/email) read. Bucket
        trips use subject ``bucket:<mode>``; the master kill uses
        ``portfolio``. Never raises — alerting must not break the halt path.
        """
        try:
            from backtest.alerts.broker import get_alert_broker
            from backtest.alerts.types import AlertType

            reason_text = str(reason or "risk limit breached")
            limit_type = (
                "portfolio_max_drawdown"
                if "drawdown" in reason_text.lower()
                else "portfolio_daily_loss"
            )
            subject = f"bucket:{bucket}" if scope == "bucket" else "portfolio"
            label = str(bucket or "portfolio").upper()
            get_alert_broker().raise_alert(
                AlertType.RISK_LIMIT_BREACH.value,
                "critical",
                f"{label} halted: {reason_text}",
                subject=subject,
                data={
                    "scope": scope,
                    "bucket": bucket,
                    "symbol": label,
                    "limit_type": limit_type,
                    "reason": reason_text,
                    "halt_mode": str(halt_mode or ""),
                    "equity": round(float(equity), 2),
                    "daily_pnl": round(float(daily_pnl), 2),
                    "drawdown_pct": round(float(drawdown_pct or 0.0), 4),
                },
            )
        except Exception:  # noqa: BLE001 — alerting never breaks the halt path
            logger.exception("breaker alert raise failed")

    @staticmethod
    def _resolve_breaker_alerts(subjects: List[str]) -> None:
        """Resolve breaker alerts for scopes a reset cleared (best-effort)."""
        try:
            from backtest.alerts.broker import get_alert_broker
            from backtest.alerts.types import AlertType

            broker = get_alert_broker()
            for subject in subjects:
                broker.resolve_key(
                    AlertType.RISK_LIMIT_BREACH.value, subject, reason="breaker reset"
                )
        except Exception:  # noqa: BLE001
            logger.exception("breaker alert resolve failed")

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

        # Per-segment anchors (Phase D) — same rule, one slice per live
        # segment present in the book (derived from runner configs; no
        # config read needed here).
        seg_names = {
            (getattr(r.config, "segment", None) or "")
            for r in self._runners.values()
            if getattr(r.config, "segment", None) and self._runner_bucket(r) == "live"
        }
        for seg_name in seg_names:
            key = self._segment_key(seg_name)
            self._ensure_bucket_state(key)
            s_equity = sum(r.equity() for r in self._segment_runners(seg_name))
            if s_equity > self._bucket_peak.get(key, 0.0):
                self._bucket_peak[key] = s_equity
            if self._bucket_day_start.get(key, 0.0) <= 0:
                self._bucket_day_start[key] = s_equity

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

            # Step 1: include dashboard book (second book) in summary so portfolio
            # renders per-structure option trades — even manual ones.
            # This is the fix for "trades taken on options tab not visible on portfolio"
            try:
                dashboard_book = self.get_dashboard_book_summary()
            except Exception:
                dashboard_book = {
                    "exists": False,
                    "open_positions": 0,
                    "open_structures": 0,
                    "positions": [],
                    "structures": [],
                }

            # Include dashboard book in totals when it exists — so portfolio
            # overview is honest about total money at risk (fix for invisible trades).
            dashboard_equity = (
                dashboard_book.get("equity", 0.0) if dashboard_book.get("exists") else 0.0
            )
            dashboard_daily = (
                dashboard_book.get("unrealized_pnl", 0.0) if dashboard_book.get("exists") else 0.0
            )
            dashboard_realized = (
                dashboard_book.get("realized_pnl", 0.0) if dashboard_book.get("exists") else 0.0
            )
            dashboard_positions = (
                dashboard_book.get("open_positions", 0) if dashboard_book.get("exists") else 0
            )

            # Combined totals include dashboard book so flatten/emergency is honest
            combined_equity = equity + dashboard_equity
            combined_daily = daily + dashboard_daily
            combined_realized = realized + dashboard_realized
            combined_positions = open_positions + dashboard_positions
            stale_marks_count = sum(int(s.get("stale_positions") or 0) for s in states)

            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_capital": round(total_capital, 2),
                "total_equity": round(combined_equity, 2),
                "deployed_capital": round(deployed, 2),
                "deployed_pct": (
                    round(deployed / self.total_capital, 4) if self.total_capital > 0 else 0.0
                ),
                "daily_pnl": round(combined_daily, 2),
                "daily_pnl_pct": (round(combined_daily / day_start, 6) if day_start else 0.0),
                "realized_pnl": round(combined_realized, 2),
                "open_positions": combined_positions,
                "stale_marks_count": stale_marks_count,
                "runner_count": len(states),
                "running": running,
                "paused": paused,
                "stopped": stopped,
                "errors": errors,
                "daily_loss_limit": limit,
                "daily_loss_used": max(0.0, -combined_daily),
                "daily_loss_pct": round(max(0.0, -combined_daily) / limit, 4) if limit > 0 else 0.0,
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
                # Feed clock for the UI countdown: freshest bar any runner
                # processed + the poll cadence it arrived on (2026-09-23).
                "last_bar_ts": max((r.get("last_bar_ts") or "" for r in states), default="")
                or None,
                "fill_count": self.ledger.fill_count,
                "order_count": self.ledger.order_count,
                "runners": states,
                "buckets": buckets,
                "capability": capability,
                # Step 1: dashboard book embedded — portfolio renders per-structure option trades
                "dashboard_book": dashboard_book,
                # Live Order Management (2026-09-23): one flat row per open
                # position across runners, so the positions table renders
                # holdings (with their stop/target and the actions key) from
                # the same 1 Hz snapshot as everything else instead of
                # re-deriving them per render.
                "positions": self.list_positions(mode),
                # Scoped like the positions above and the REST read: the badge
                # is seeded from this snapshot, so a paper page counting live
                # traffic would nudge the operator about an order they cannot
                # see (or cancel) from that page.
                "orders_summary": self._orders_summary_for(mode),
                # Market-status awareness (finding #11): holiday/weekend/session truth
                "market": self._market_summary(),
            }

    def _market_summary(self) -> Dict[str, Any]:
        try:
            from backtest.live.market_status import get_market_day_state

            return get_market_day_state()
        except Exception:  # noqa: BLE001
            return {
                "day_state": "TRADING_DAY",
                "session_state": "OPEN",
                "holiday_name": None,
                "next_open_ts": None,
                "gap_warning": "market status lookup failed",
                "is_open": True,
            }

    def get_runner_detail(self, instance_id: str) -> Dict[str, Any]:
        runner = self._runners.get(instance_id)
        if runner is None:
            raise KeyError(f"unknown runner: {instance_id}")
        return runner.get_detail()

    def get_equity_snapshots(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """On-demand equity snapshot views (replaces the ticking live curve).

        Owner decision 2026-09-21: the combined equity line ticking on every
        SSE frame carried no information for a 1-minute validator fleet.
        What teaches something is asked-for views computed here, once:

        * ``per_runner``   — one row per runner: equity, realized/unrealized,
          daily PnL, win rate, trade count. The "which strategy earns" table.
        * ``day_closes``   — the combined-book equity at each day's last
          recorded point, derived from runner equity curves (no new storage).
        * ``session``      — today's session summary: day start, current,
          net, peak, drawdown.

        ``mode`` scopes to one bucket, same semantics as the summary.
        """
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in VALID_INSTANCE_MODES:
                raise ValueError(f"mode must be one of {VALID_INSTANCE_MODES}, got {mode!r}")

        with self._lock:
            runners = list(self._runners.values())
        if mode is not None:
            runners = [r for r in runners if self._runner_bucket(r) == mode]

        # -- per-runner rows -------------------------------------------------
        per_runner = []
        day_points: Dict[str, float] = {}  # date -> last equity point of that day
        intraday_today: List[Dict[str, Any]] = []
        today = datetime.now(timezone.utc).date().isoformat()
        for r in runners:
            with r._lock:
                curve = list(r.equity_curve)
                trades = r.closed_trades
                trades_today = [t for t in trades if (t.get("exit_ts") or "")[:10] == today]
            state = r.get_state()
            per_runner.append(
                {
                    "instance_id": state["instance_id"],
                    "name": state["name"],
                    "strategy": state["strategy_name"],
                    "target": state["target_label"],
                    "status": state["status"],
                    "mode": state["mode"],
                    "equity": state["equity"],
                    "realized_pnl": state["realized_pnl"],
                    "daily_pnl": state["daily_pnl"],
                    "open_pnl": state["open_pnl"],
                    "win_rate": state["win_rate"],
                    "trades": len(trades),
                    "trades_today": len(trades_today),
                    "bars_processed": state["bars_processed"],
                }
            )
            for point in curve:
                ts = str(point.get("ts") or "")
                day = ts[:10]
                if not day:
                    continue
                eq = float(point.get("equity") or 0.0)
                day_points[day] = eq  # curve is chronological → last write wins
                if day == today:
                    intraday_today.append({"ts": ts, "equity": eq})

        day_closes = [{"date": d, "equity": round(day_points[d], 2)} for d in sorted(day_points)]

        # -- session summary --------------------------------------------------
        equity = sum(p["equity"] for p in per_runner)
        daily = sum(p["daily_pnl"] for p in per_runner)
        realized = sum(p["realized_pnl"] for p in per_runner)
        allocated = sum(r.config.allocated_capital for r in runners)
        day_start = (
            self._bucket_day_start.get(mode, equity) if mode is not None else self._day_start_equity
        )
        peak = (
            self._bucket_peak.get(mode, equity)
            if mode is not None
            else max(self.peak_equity, equity)
        )
        drawdown = ((peak - equity) / peak) if peak > 0 else 0.0
        # Portfolio win rate = closed-won trades / closed trades (2026-09-22 fix:
        # this used to sum per-runner win-rate FRACTIONS and divide by total
        # trades — 2 runners at 50% each reported as 5%). Re-derive from the
        # actual trade outcomes instead of the rounded per-runner stats.
        total_trades = sum(p["trades"] for p in per_runner)
        wins_count = 0
        for r in runners:
            with r._lock:
                wins_count += sum(1 for t in r.closed_trades if float(t.get("pnl") or 0) > 0)
        portfolio_win_rate = round(wins_count / total_trades, 4) if total_trades else None

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode or "all",
            "session": {
                "day_start_equity": round(day_start or equity, 2),
                "equity": round(equity, 2),
                "net_today": round(daily, 2),
                "realized_total": round(realized, 2),
                "peak_equity": round(peak, 2),
                "drawdown_pct": round(drawdown, 4),
                "allocated_capital": round(allocated, 2),
                "trades_total": total_trades,
                "wins": wins_count,
                "win_rate": portfolio_win_rate,
            },
            "per_runner": per_runner,
            "day_closes": day_closes,
            "intraday_today": intraday_today,
        }

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
        if self.feed is None:
            raise ValueError(
                "no synthetic feed on this manager — source=synthetic is disabled by "
                "config/data_sources.yaml; tests opt in via BACKTEST_DATA_PROFILE=testing"
            )
        self.feed.emit_one(**bar_kwargs)

    def shutdown(self) -> None:
        for gw_key, gateway in list(self._live_gateways.items()):
            if not gateway.working_count():
                continue
            try:
                gateway.poll_pending()
            except Exception:  # noqa: BLE001
                logger.exception("[live:%s] final poll before shutdown failed", gw_key)
        self._persist_state()  # V2: last write wins — leave a restorable state
        if self.intelligence is not None:
            try:
                self.intelligence.stop()
                for iid, runner in list(self._runners.items()):
                    self.intelligence.unregister_runner(iid, runner)
            except Exception:  # noqa: BLE001
                logger.debug("intelligence shutdown failed", exc_info=True)
        if self.feed is not None:
            self.feed.stop()
        self.mstock_feed.stop()
        self._watchdog_stop.set()
        with self._lock:
            for runner in self._runners.values():
                runner.stop()  # releases the runner's chain-bus subscription
                # U6.2: bar-feed subscriptions must go too, or the process-wide
                # registry leaks entries that shadow the next manager's feed.
                for sym in runner.config.symbols:
                    self.feed_registry.release(runner.config.source, sym, runner.config.timeframe)


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
    live_broker: Optional[Any] = None,
    confirm_live_orders: bool = False,
) -> PortfolioManager:
    """Tear down and recreate the singleton (tests / restart)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.shutdown()
        # Alerts belong to the manager's book: a fresh manager starts with a
        # fresh alert broker (no stale alerts/subscriptions from the old one),
        # and the outbound notifier is stopped before its broker goes away.
        try:
            from backtest.alerts.broker import reset_alert_broker
            from backtest.alerts.notifier import stop_alert_notifier

            stop_alert_notifier()
            reset_alert_broker()
        except Exception:  # noqa: BLE001
            pass
        _MANAGER = PortfolioManager(
            risk_config=risk_config,
            tick_seconds=tick_seconds,
            warmup_bars=warmup_bars,
            auto_start_feed=auto_start_feed,
            live_broker=live_broker,
            confirm_live_orders=confirm_live_orders,
        )
        return _MANAGER
