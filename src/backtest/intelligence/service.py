"""PortfolioIntelligence — the read-only intelligence layer over the portfolio.

One instance lives on each :class:`~backtest.forward.portfolio_manager.
PortfolioManager` (``manager.intelligence``). It

* computes aggregate Greeks, concentration, correlation and market regime
  from the books (cached per bucket for ``greeks_cache_s`` so every SSE
  client shares one computation);
* evaluates the alert rules and publishes/resolves alerts on the
  :class:`~backtest.alerts.broker.AlertBroker`;
* registers each runner's strategy alert subscriptions with the broker.

It never closes, resizes or pauses anything. Strategies that subscribe
decide for themselves; the runner executes their *requests* through the
normal engine paths (C2: strategies never touch a broker).

Evaluation runs from three places, all funnelled through a non-blocking
guard so only one evaluation is ever in flight: the manager's tick end, the
optional background thread (web app), and the alert API (so a stale-feed
alert can still fire when no ticks arrive at all).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backtest.alerts.broker import AlertBroker, get_alert_broker
from backtest.alerts.catalog import context_for
from backtest.alerts.types import AlertType, Severity
from backtest.data.sources_policy import default_broker_source
from backtest.intelligence.collectors import collect_legs
from backtest.intelligence.concentration import ConcentrationMonitor
from backtest.intelligence.config import IntelligenceConfig, load_config
from backtest.intelligence.correlation import CorrelationCalculator
from backtest.intelligence.factors import (
    FACTOR_ORDER,
    build_factors,
    iv_rv_factor,
    session_factor,
    trend_factor,
)
from backtest.intelligence.greeks import PortfolioGreeksAggregator
from backtest.intelligence.market_activity import MarketActivityMonitor
from backtest.intelligence.regime import LABELS, MarketRegimeDetector, regime_fit

logger = logging.getLogger("backtest.intelligence")

BROKER_SOURCES = frozenset({"mstock", "dhan", "upstox"})


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


def _regime_word(regime: str) -> str:
    return LABELS.get(regime, regime).split()[0]


class PortfolioIntelligence:
    def __init__(
        self,
        manager: Any,
        broker: Optional[AlertBroker] = None,
        config: Optional[IntelligenceConfig] = None,
        persister: Any = None,
    ) -> None:
        self.manager = manager
        self.config = config or load_config()
        cfg = self.config
        self.broker = broker or get_alert_broker()
        self.broker.event_ttl_s = cfg.event_alert_ttl_s
        self.broker.ignore_ttl_s = cfg.ignore_ttl_s
        self.broker.renotify_cooldown_s = cfg.renotify_cooldown_s
        self.broker.auto_dismiss_exempt = set(cfg.auto_dismiss_exempt)
        self.aggregator = PortfolioGreeksAggregator(risk_free_rate=cfg.risk_free_rate)
        self.concentration_monitor = ConcentrationMonitor(
            max_pct=cfg.concentration_max_pct,
            min_positions=cfg.concentration_min_positions,
            strike_cluster_min=cfg.strike_cluster_min,
        )
        self.correlation = CorrelationCalculator(
            window=cfg.correlation_window,
            min_samples=cfg.correlation_min_samples,
            warning=cfg.correlation_warning,
            cache_s=cfg.correlation_cache_s,
        )
        self.regime = MarketRegimeDetector(
            low_max=cfg.regime_low_max,
            high_min=cfg.regime_high_min,
            hysteresis=cfg.regime_hysteresis,
            sample_s=cfg.regime_sample_s,
            benchmark=cfg.regime_benchmark,
            realized_window=cfg.realized_vol_window,
            vix_symbols=cfg.vix_symbols,
            vix_stale_s=cfg.vix_stale_s,
        )
        self.activity = MarketActivityMonitor(
            oi_multiplier=cfg.oi_spike_multiplier,
            oi_min_history=cfg.oi_min_history,
            spread_multiplier=cfg.spread_multiplier,
        )
        #: ``False`` (``--disable-portfolio-intelligence``) turns every hook
        #: and evaluation into a no-op; the API answers 503.
        self.enabled = True
        self.persister = None
        if persister is not None:
            self.attach_persister(persister)

        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        #: cached daily-bar inputs for the multi-factor engine (value, fetched_at).
        self._factor_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        #: latest trend-factor status, refreshed with the factor inputs.
        self._trend_status: Optional[str] = None
        #: last trend status we raised an alert for (None = no baseline yet).
        self._last_trend_status: Optional[str] = None
        #: last NSE session phase we raised an alert for.
        self._last_session_phase: Optional[str] = None
        #: last IV/RV dislocation status we raised an alert for.
        self._last_iv_rv_status: Optional[str] = None
        #: dev-gated manual factor overrides: {factor_key: override_value}.
        #: Used only for testing regime impact without waiting for live flips.
        self._factor_overrides: Dict[str, Any] = {}
        self._eval_guard = threading.Lock()
        self._last_eval = 0.0
        self._last_greeks_persist = 0.0
        self._last_regime_persisted: Optional[str] = None
        self.evaluations = 0
        #: symbol → wall-clock time the last bar arrived (stale-feed rule).
        self._last_bar_wall: Dict[str, float] = {}
        #: instance id → wall-clock time the runner was registered (grace).
        self._registered_at: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    # Manager hooks
    # ------------------------------------------------------------------ #

    def attach_persister(self, persister: Any) -> None:
        """Route alert lifecycle events + history samples to the database."""
        if self.persister is persister:
            return
        self.persister = persister
        self.broker.add_listener(persister.on_alert_event)

    def on_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        self._last_bar_wall[str(symbol).upper()] = time.time()
        try:
            self.regime.observe_bar(symbol, bar)
        except Exception:  # noqa: BLE001 — intelligence never breaks the feed
            logger.debug("[intelligence] regime observe failed", exc_info=True)

    def on_tick_end(self) -> None:
        if not self.enabled:
            return
        # Books moved this tick: drop the shared snapshot so the next reader
        # (evaluation or SSE) sees post-tick state rather than a mid-tick one.
        self.invalidate()
        try:
            runners = list(self.manager._runners.values())
            self.correlation.record({r.instance_id: r.equity() for r in runners})
        except Exception:  # noqa: BLE001
            logger.debug("[intelligence] correlation sample failed", exc_info=True)
        self.maybe_evaluate()

    def register_runner(self, runner: Any) -> List[str]:
        """Wire a runner's strategy alert subscriptions into the broker."""
        self._registered_at[runner.instance_id] = time.time()
        strategy = getattr(runner, "strategy", None)
        getter = getattr(strategy, "alert_subscriptions", None)
        types: List[str] = []
        if callable(getter):
            try:
                types = [str(t) for t in getter()]
            except Exception:  # noqa: BLE001
                logger.exception("[intelligence] %s: bad alert subscriptions", runner.instance_id)
        self.broker.unsubscribe(runner.instance_id)
        meta = {
            "instance_id": runner.instance_id,
            "runner": runner.config.name,
            "strategy": runner.config.strategy_name,
            "mode": runner.config.mode,
        }
        for alert_type in types:
            self.broker.subscribe(
                alert_type,
                self._callback_for(runner),
                subscriber_id=runner.instance_id,
                meta=meta,
                data_hook=self._data_hook_for(runner),
                on_resolve=self._callback_for(runner, "on_alert_resolved"),
            )
        if strategy is not None:
            # Runtime subscribe_to_alerts() calls re-register through here.
            try:
                strategy.__dict__["_alert_registrar"] = lambda: self.register_runner(runner)
            except Exception:  # noqa: BLE001 — exotic strategy objects
                pass
        if types:
            logger.info(
                "[intelligence] %s (%s) subscribed to %s",
                runner.config.name,
                runner.config.strategy_name,
                ", ".join(types),
            )
        return types

    def unregister_runner(self, instance_id: str, runner: Any = None) -> None:
        self.broker.unsubscribe(instance_id)
        self._registered_at.pop(instance_id, None)
        strategy = getattr(runner, "strategy", None)
        if strategy is not None:
            try:
                strategy.__dict__.pop("_alert_registrar", None)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _callback_for(runner: Any, method: str = "on_alert"):
        def callback(alert_type: str, data: Dict[str, Any]) -> Any:
            handler = getattr(runner.strategy, method, None)
            if handler is None:
                return None
            lock = getattr(runner, "_lock", None)
            if lock is None:
                return handler(alert_type, data)
            with lock:
                result = handler(alert_type, data)
            runner_log = getattr(runner, "_log_signal", None)
            if callable(runner_log):
                try:
                    runner_log(
                        (runner.config.symbols or ["-"])[0],
                        "ALERT_RESOLVED" if method == "on_alert_resolved" else "ALERT",
                        None,
                        None,
                        f"{alert_type}: {data.get('message', '')}"[:200],
                    )
                except Exception:  # noqa: BLE001
                    pass
            return result

        return callback

    @staticmethod
    def _data_hook_for(runner: Any):
        def hook(alert: Any) -> Dict[str, Any]:
            data = dict(alert.data)
            data.setdefault("alert_id", alert.alert_id)
            data.setdefault("severity", str(alert.severity))
            data.setdefault("message", alert.message)
            mine = [
                c for c in data.get("contributors") or []
                if c.get("instance_id") == runner.instance_id
            ]
            data["self_contribution"] = mine[0] if mine else None
            data["is_contributor"] = bool(mine)
            data["instance_id"] = runner.instance_id
            return data

        return hook

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #

    def _compute(self, mode: Optional[str]) -> Dict[str, Any]:
        legs, meta = collect_legs(self.manager, mode=mode)
        greeks = self.aggregator.calculate(legs, self.config.scenarios)
        concentration = self.concentration_monitor.calculate(legs, greeks)
        now = datetime.now(timezone.utc)
        greeks["timestamp"] = now.isoformat()
        greeks["mode"] = mode or "all"
        return {
            "legs": legs,
            "roster": meta.get("roster", {}),
            "greeks": greeks,
            "concentration": concentration,
            "timestamp": now.isoformat(),
        }

    def snapshot(self, mode: Optional[str] = None, max_age: Optional[float] = None) -> Dict:
        key = mode or "all"
        ttl = self.config.greeks_cache_s if max_age is None else max_age
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
        snap = self._compute(mode)
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), snap)
        return snap

    def invalidate(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def greeks(self, mode: Optional[str] = None) -> Dict[str, Any]:
        return self.snapshot(mode)["greeks"]

    def concentration(self, mode: Optional[str] = None) -> Dict[str, Any]:
        return self.snapshot(mode)["concentration"]

    def _groups(self, roster: Dict[str, Dict], group_by: str) -> Dict[str, Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for iid, r in roster.items():
            if group_by == "strategy":
                g = groups.setdefault(
                    r["strategy"], {"label": r["strategy"], "members": [], "mode": r["mode"]}
                )
                g["members"].append(iid)
            else:
                groups[iid] = {
                    "label": f"{r['name']}",
                    "members": [iid],
                    "strategy": r["strategy"],
                    "mode": r["mode"],
                }
        return groups

    def correlation_matrix(
        self, mode: Optional[str] = None, group_by: str = "runner", use_cache: bool = True
    ) -> Dict[str, Any]:
        roster = self.snapshot(mode)["roster"]
        result = self.correlation.matrix(self._groups(roster, group_by), use_cache=use_cache)
        return {**result, "group_by": group_by, "mode": mode or "all"}

    def _factor_inputs(self) -> Dict[str, Any]:
        """Daily bars + net gamma for the factor engine, cached for ``factor_cache_s``.

        Any failure (no DB, empty cache) degrades to empty inputs rather than
        raising — the VIX / session / gamma factors still render.
        """
        now = time.time()
        with self._cache_lock:
            cached = self._factor_cache
            if cached is not None and now - cached[0] < self.config.factor_cache_s:
                return cached[1]

        inputs: Dict[str, Any] = {
            "benchmark_closes": [],
            "breadth_closes": {},
            "net_gamma": None,
        }
        try:
            from datetime import timedelta

            from backtest.data.coverage import load_equity_universe
            from backtest.data.db_source import DbSource
            from backtest.timekeeping import now_utc

            end_dt = now_utc()
            start = (
                end_dt - timedelta(days=int(self.config.factor_lookback_days))
            ).date().isoformat()
            end = end_dt.date().isoformat()
            benchmark = self.config.regime_benchmark
            symbols = [benchmark] + [
                r["symbol"]
                for r in load_equity_universe()
                if r.get("symbol") and r["symbol"] != benchmark
            ]
            frames = DbSource().get_candles_bulk(symbols, start, end, "1day")
            for sym, df in frames.items():
                closes = [
                    float(x)
                    for x in df["close"].tolist()
                    if x is not None and x == x  # drop NaN
                ]
                if not closes:
                    continue
                if sym == benchmark:
                    inputs["benchmark_closes"] = closes
                else:
                    inputs["breadth_closes"][sym] = closes
        except Exception:  # noqa: BLE001 — a data hiccup must not break the snapshot
            logger.debug("[intelligence] factor bar inputs unavailable", exc_info=True)

        try:
            inputs["net_gamma"] = self.greeks().get("net_gamma")
        except Exception:  # noqa: BLE001
            logger.debug("[intelligence] factor gamma input unavailable", exc_info=True)

        try:
            trend = trend_factor(
                inputs.get("benchmark_closes") or [], self.config.factor_sma_window
            )
            with self._cache_lock:
                self._trend_status = trend.status
        except Exception:  # noqa: BLE001
            logger.debug("[intelligence] trend status update failed", exc_info=True)

        with self._cache_lock:
            self._factor_cache = (now, inputs)
        return inputs

    @staticmethod
    def _runner_pause_status(runner: Any) -> Dict[str, Any]:
        """Return pause status for a runner: {active: bool, reason: str|None, sources: list[str]}.

        Reads pause_reason/block_reason from the runner object when available,
        otherwise returns inactive. Used by the regime card to show inline
        pause-status chips next to each strategy fit row.
        """
        if runner is None:
            return {"active": False, "reason": None, "sources": []}
        pause_reason = getattr(runner, "pause_reason", None) or ""
        block_reason = getattr(runner, "block_reason", None) or ""
        sources: List[str] = []
        if pause_reason:
            sources.append(pause_reason)
        if block_reason:
            sources.append(block_reason)
        return {
            "active": bool(sources),
            "reason": sources[0] if sources else None,
            "sources": sources,
        }

    def regime_snapshot(self, mode: Optional[str] = None) -> Dict[str, Any]:
        snap = self.regime.snapshot()
        book = self.snapshot(mode)
        roster = book["roster"]
        fits = []
        seen = set()
        for iid, r in roster.items():
            runner = self.manager.get_runner(iid) if hasattr(self.manager, "get_runner") else None
            vix_range = getattr(getattr(runner, "strategy", None), "regime_vix_range", None)
            fit = regime_fit(vix_range, snap.get("vix") if snap["regime"] != "unknown" else None)
            dedupe = (r["strategy"], r["name"])
            if dedupe in seen:
                continue
            seen.add(dedupe)
            fits.append(
                {
                    "instance_id": iid,
                    "runner": r["name"],
                    "strategy": r["strategy"],
                    "runner_status": r["status"],
                    "mode": r["mode"],
                    "pause_status": self._runner_pause_status(runner),
                    **fit,
                }
            )
        snap["strategy_fit"] = fits
        snap["feed_synthetic"] = any(r.get("source") == "synthetic" for r in roster.values())
        try:
            inputs = self._factor_inputs()
            snap["factors"] = build_factors(
                regime_snap=snap,
                benchmark_closes=inputs.get("benchmark_closes"),
                breadth_closes=inputs.get("breadth_closes"),
                net_gamma=inputs.get("net_gamma"),
                atm_iv=self._atm_iv_proxy(book.get("legs")),
                sma_window=self.config.factor_sma_window,
                gamma_critical=self.config.gamma_critical,
                iv_rv_buffer=self.config.iv_rv_buffer,
                overrides=self._factor_overrides or None,
            )
        except Exception:  # noqa: BLE001 — factors are additive, never fatal
            logger.debug("[intelligence] factor build failed", exc_info=True)
        return snap

    def set_factor_override(self, key: str, override: Dict[str, Any]) -> None:
        """Dev-gated: manually override a factor's fields for testing."""
        valid_keys = set(FACTOR_ORDER)
        if key not in valid_keys:
            raise ValueError(f"unknown factor key: {key}")
        self._factor_overrides[key] = override

    def clear_factor_override(self, key: Optional[str] = None) -> None:
        """Dev-gated: clear a specific factor override, or all if key is None."""
        if key is None:
            self._factor_overrides.clear()
        else:
            self._factor_overrides.pop(key, None)

    def oi_activity(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        return self.activity.activity(symbol)

    def broker_segment_rollup(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """``{"by_broker": [...], "by_segment": [...]}`` (Phase D, additive).

        Derived from the live runner roster: capital/equity/positions per
        execution broker and per segment, plus each broker's share of total
        equity (`concentration_pct`) — the cross-broker concentration view.
        """
        by_broker: Dict[str, Dict[str, Any]] = {}
        by_segment: Dict[str, Dict[str, Any]] = {}
        try:
            rows = self.manager.list_instances(mode=mode)
        except Exception:  # noqa: BLE001 — a manager hiccup never breaks intelligence
            rows = []
        total_equity = sum(float(r.get("equity") or 0.0) for r in rows) or 0.0
        for r in rows:
            broker = str(r.get("broker") or ("live" if r.get("mode") == "live" else "paper"))
            segment = str(r.get("segment") or "")
            for key, store in ((broker, by_broker), (segment, by_segment)):
                if not key:
                    continue
                g = store.setdefault(
                    key,
                    {
                        "name": key,
                        "runner_count": 0,
                        "equity": 0.0,
                        "allocated_capital": 0.0,
                        "daily_pnl": 0.0,
                        "open_positions": 0,
                    },
                )
                g["runner_count"] += 1
                g["equity"] += float(r.get("equity") or 0.0)
                g["allocated_capital"] += float(r.get("allocated_capital") or 0.0)
                g["daily_pnl"] += float(r.get("daily_pnl") or 0.0)
                g["open_positions"] += int(r.get("open_positions") or 0)
        for g in by_broker.values():
            g["concentration_pct"] = (
                round(g["equity"] / total_equity, 4) if total_equity > 0 else 0.0
            )
        for store in (by_broker, by_segment):
            for g in store.values():
                for k in ("equity", "allocated_capital", "daily_pnl"):
                    g[k] = round(g[k], 2)
        return {
            "by_broker": sorted(by_broker.values(), key=lambda x: x["name"]),
            "by_segment": sorted(by_segment.values(), key=lambda x: x["name"]),
        }

    def overview(self, mode: Optional[str] = None) -> Dict[str, Any]:
        snap = self.snapshot(mode)
        rollup = self.broker_segment_rollup(mode)
        return {
            "greeks": snap["greeks"],
            "concentration": snap["concentration"],
            "by_broker": rollup["by_broker"],
            "by_segment": rollup["by_segment"],
            "correlation": self.correlation_matrix(mode),
            "regime": self.regime_snapshot(mode),
            "market_activity": self.oi_activity(),
            "alerts": self.alerts_payload(),
            "thresholds": {
                "gamma_critical": self.config.gamma_critical,
                "delta_warning_abs": self.config.delta_warning_abs,
                "concentration_max_pct": self.config.concentration_max_pct,
                "strike_cluster_min": self.config.strike_cluster_min,
                "correlation_warning": self.config.correlation_warning,
            },
            "timestamp": snap["timestamp"],
        }

    def stream_summary(self) -> Dict[str, Any]:
        """Compact 1 s payload for the portfolio SSE stream (shared cache)."""
        g = self.greeks(None)
        return {
            "net_delta": g.get("net_delta"),
            "net_gamma": g.get("net_gamma"),
            "net_vega": g.get("net_vega"),
            "net_theta": g.get("net_theta"),
            "delta_rupees_1pct": g.get("delta_rupees_1pct"),
            "gamma_rupees_1pct": g.get("gamma_rupees_1pct"),
            "scenarios": g.get("scenarios"),
            "positions": g.get("positions"),
            "by_strategy": [
                {
                    k: row.get(k)
                    for k in ("source_id", "label", "strategy", "mode", "delta", "gamma",
                              "vega", "theta", "positions")
                }
                for row in g.get("breakdown_by_strategy", [])
            ],
            "alerts": {"counts": self.broker.counts(), "version": self.broker.version},
            "timestamp": g.get("timestamp"),
        }

    # ------------------------------------------------------------------ #
    # Alerts payloads for the API / widget
    # ------------------------------------------------------------------ #

    def alerts_payload(self, include_dismissed: bool = False) -> Dict[str, Any]:
        alerts = self.broker.get_active_alerts(include_dismissed=include_dismissed)
        return {
            "alerts": [self._with_context(a.to_dict(), brief=True) for a in alerts],
            "counts": self.broker.counts(),
            "version": self.broker.version,
        }

    def _with_context(self, row: Dict[str, Any], brief: bool = False) -> Dict[str, Any]:
        ctx = context_for(row["alert_type"])
        row["title"] = ctx["title"]
        row["section"] = ctx["section"]
        if not brief:
            row["what_it_means"] = ctx["what_it_means"]
            row["typical_responses"] = ctx["typical_responses"]
        return row

    def alert_detail(self, alert_id: str) -> Optional[Dict[str, Any]]:
        alert = self.broker.get(alert_id)
        if alert is None:
            return None
        row = self._with_context(alert.to_dict())
        subscribed = self.broker.subscribers_for(str(alert.alert_type))
        sub_ids = {s.get("instance_id") for s in subscribed}
        not_subscribed = [
            {
                "instance_id": c.get("instance_id"),
                "runner": c.get("label"),
                "strategy": c.get("strategy"),
            }
            for c in row["data"].get("contributors") or []
            if c.get("instance_id") and c.get("instance_id") not in sub_ids
        ]
        row["subscriptions"] = {"subscribed": subscribed, "not_subscribed": not_subscribed}
        return row

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def maybe_evaluate(self, force: bool = False) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic()
        if not force and now - self._last_eval < self.config.evaluate_interval_s:
            return False
        if not self._eval_guard.acquire(blocking=False):
            return False
        try:
            self._last_eval = now
            self.evaluate()
            return True
        except Exception:  # noqa: BLE001 — never let a rule bug break the tick
            logger.exception("[intelligence] evaluation failed")
            return False
        finally:
            self._eval_guard.release()

    def evaluate(self) -> Dict[str, Any]:
        """Run every rule once against a fresh all-bucket snapshot."""
        self.evaluations += 1
        snap = self.snapshot(None, max_age=0.0)
        greeks, conc, roster = snap["greeks"], snap["concentration"], snap["roster"]
        self._rule_gamma(greeks, snap["legs"])
        self._rule_delta(greeks)
        self._rule_concentration(conc)
        self._rule_regime(roster)
        self._rule_trend()
        self._rule_session()
        self._rule_iv_rv(snap["legs"])
        self._rule_correlation()
        self._rule_feed_stale(roster)
        self.broker.sweep()
        self._persist(greeks, conc)
        return {"greeks": greeks, "concentration": conc}

    def _contributors(self, greeks: Dict[str, Any], field: str, sign: int) -> List[Dict]:
        rows = [
            r for r in greeks.get("breakdown_by_strategy", [])
            if (r.get(field) or 0) * sign > 0
        ]
        total = sum(abs(r[field]) for r in rows) or 1.0
        out = []
        for r in sorted(rows, key=lambda x: -abs(x[field])):
            out.append(
                {
                    "instance_id": r["source_id"],
                    "label": r["label"],
                    "strategy": r["strategy"],
                    "mode": r["mode"],
                    field: r[field],
                    "share": round(abs(r[field]) / total, 4),
                    "positions": r["positions"],
                }
            )
        return out

    @staticmethod
    def _bucket_hint(contributors: List[Dict[str, Any]]) -> str:
        return "live" if any(c.get("mode") == "live" for c in contributors) else "paper"

    def _move_pnl(self, legs: List[Any], pct: float) -> float:
        """Worst full-revaluation P&L of a ±pct move (all underlyings)."""
        worst = 0.0
        for signed in (pct, -pct):
            total = 0.0
            for leg in legs:
                value = self.aggregator.scenario_pnl(leg, signed, 0.0)
                if value is not None:
                    total += value
            worst = min(worst, total)
        return round(worst, 0)

    def _rule_gamma(self, greeks: Dict[str, Any], legs: List[Any]) -> None:
        t = AlertType.PORTFOLIO_GAMMA_CRITICAL.value
        threshold = float(self.config.gamma_critical)
        net = float(greeks.get("net_gamma") or 0.0)
        if net < threshold:
            contributors = self._contributors(greeks, "gamma", -1)
            breach = abs((net - threshold) / threshold) * 100.0 if threshold else 0.0
            names = ", ".join(f"{c['label']} ({c['positions']})" for c in contributors[:3])
            self.broker.raise_alert(
                t,
                Severity.CRITICAL.value,
                f"Portfolio gamma {net:,.1f} (threshold {threshold:,.0f})",
                data={
                    "net_gamma": net,
                    "threshold": threshold,
                    "breach_pct": round(breach, 1),
                    "gamma_rupees_1pct": greeks.get("gamma_rupees_1pct"),
                    "move_1pct_pnl": self._move_pnl(legs, 0.01),
                    "move_2pct_pnl": self._move_pnl(legs, 0.02),
                    "net_theta": greeks.get("net_theta"),
                    "contributors": contributors,
                    "summary": f"Contributing: {names}" if names else "",
                    "bucket_hint": self._bucket_hint(contributors),
                    "units": greeks.get("units", {}).get("net_gamma"),
                },
            )
        else:
            self.broker.resolve_missing(t, [])

    def _rule_delta(self, greeks: Dict[str, Any]) -> None:
        t = AlertType.PORTFOLIO_DELTA_WARNING.value
        threshold = abs(float(self.config.delta_warning_abs))
        net = float(greeks.get("net_delta") or 0.0)
        if abs(net) > threshold:
            sign = 1 if net > 0 else -1
            contributors = self._contributors(greeks, "delta", sign)
            self.broker.raise_alert(
                t,
                Severity.WARNING.value,
                f"Portfolio delta {net:+,.0f} (limit ±{threshold:,.0f}) — "
                f"{'long' if sign > 0 else 'short'} bias",
                data={
                    "net_delta": net,
                    "threshold": threshold,
                    "delta_rupees_1pct": greeks.get("delta_rupees_1pct"),
                    "contributors": contributors,
                    "summary": "Contributing: "
                    + ", ".join(f"{c['label']} ({c['delta']:+,.0f})" for c in contributors[:3]),
                    "bucket_hint": self._bucket_hint(contributors),
                },
            )
        else:
            self.broker.resolve_missing(t, [])

    def _rule_concentration(self, conc: Dict[str, Any]) -> None:
        t_conc = AlertType.CONCENTRATION_HIGH.value
        t_strike = AlertType.STRIKE_CLUSTERING.value
        active_u, active_s = [], []
        for a in conc.get("alerts", []):
            if a["type"] == "high_concentration":
                u = a["underlying"]
                row = conc["by_underlying"].get(u, {})
                active_u.append(u)
                self.broker.raise_alert(
                    t_conc,
                    Severity.WARNING.value,
                    f"{u} concentration: {a['pct']:.0f}% "
                    f"(max recommended {a['threshold_pct']:.0f}%)",
                    data={
                        **a,
                        "exposure": row.get("exposure"),
                        "total_exposure": conc.get("total_exposure"),
                        "sources": row.get("sources", []),
                        "summary": "Held by: " + ", ".join(row.get("sources", [])[:4]),
                    },
                    subject=u,
                )
            elif a["type"] == "strike_clustering":
                row = conc["by_strike"].get(a["key"], {})
                active_s.append(a["key"])
                self.broker.raise_alert(
                    t_strike,
                    Severity.INFO.value,
                    f"{a['underlying']} {a['strike']:g}: {a['positions']} positions "
                    f"share one strike",
                    data={
                        **a,
                        "exposure": row.get("exposure"),
                        "sources": row.get("sources", []),
                        "summary": "Held by: " + ", ".join(row.get("sources", [])[:4]),
                    },
                    subject=a["key"],
                )
        self.broker.resolve_missing(t_conc, active_u)
        self.broker.resolve_missing(t_strike, active_s)

    def _rule_regime(self, roster: Dict[str, Dict[str, Any]]) -> None:
        transition = self.regime.evaluate()
        if transition is None:
            return
        t = AlertType.VIX_REGIME_CHANGE.value
        self.broker.resolve_missing(t, [], reason="superseded")
        vix = transition.get("vix")
        affected = []
        for iid, r in roster.items():
            runner = self.manager.get_runner(iid) if hasattr(self.manager, "get_runner") else None
            vix_range = getattr(getattr(runner, "strategy", None), "regime_vix_range", None)
            fit = regime_fit(vix_range, vix)
            if fit["status"] == "unfavorable":
                affected.append(
                    {"instance_id": iid, "label": r["name"], "strategy": r["strategy"],
                     "mode": r["mode"], "fit": fit["label"]}
                )
        old, new = transition["old_regime"], transition["new_regime"]
        severity = Severity.WARNING.value
        self.broker.raise_alert(
            t,
            severity,
            f"VIX regime change: {_regime_word(old)} → {_regime_word(new)} "
            f"(VIX {vix:.1f})",
            data={
                **transition,
                "affected": affected,
                "contributors": affected,
                "summary": (
                    "Affects: " + ", ".join(f"{a['label']} (unfavorable)" for a in affected[:4])
                    if affected else "No running strategy declares this regime unfavorable"
                ),
                "is_proxy": str(transition.get("source", "")).startswith("realized_vol_proxy"),
            },
        )

    def _rule_trend(self) -> None:
        """Raise TREND_REGIME_CHANGE when the trend factor flips.

        The status is refreshed by :meth:`_factor_inputs` (the 30 s
        intelligence poll / background evaluator), so this rule never blocks
        the tick on a database read. The first observation only sets the
        baseline — no alert storm at startup.
        """
        with self._cache_lock:
            status = self._trend_status
        if status is None or status == "unknown":
            return
        previous = self._last_trend_status
        self._last_trend_status = status
        if previous is None or previous == status:
            return
        t = AlertType.TREND_REGIME_CHANGE.value
        self.broker.resolve_missing(t, [], reason="superseded")
        window = int(self.config.factor_sma_window)
        self.broker.raise_alert(
            t,
            Severity.WARNING.value,
            f"Trend regime change: {previous} → {status} (vs {window}-day average)",
            data={
                "regime": status,
                "previous": previous,
                "sma_window": window,
                "summary": f"Benchmark {status} its {window}-day moving average",
            },
        )

    def _rule_session(self) -> None:
        """Raise SESSION_PHASE when the NSE intraday window changes.

        The phase is a pure clock read (no DB), so it is evaluated every tick;
        the first observation only sets the baseline.
        """
        phase = session_factor().status
        previous = self._last_session_phase
        self._last_session_phase = phase
        if previous is None or previous == phase:
            return
        t = AlertType.SESSION_PHASE.value
        self.broker.resolve_missing(t, [], reason="superseded")
        self.broker.raise_alert(
            t,
            Severity.INFO.value,
            f"Session phase: {previous} → {phase}",
            data={
                "phase": phase,
                "previous": previous,
                "summary": f"NSE session entered {phase}",
            },
        )

    @staticmethod
    def _atm_iv_proxy(legs: List[Any]) -> Optional[float]:
        """Volume-weighted IV of the book's option legs (an ATM-IV proxy).

        Uses the legs the aggregator already collected — no chain feed or new
        storage. Returns ``None`` when no leg carries a usable IV.
        """
        num = 0.0
        den = 0.0
        for leg in legs or []:
            iv = getattr(leg, "iv", None)
            qty = getattr(leg, "units", 0) or 0
            try:
                iv = float(iv)
                qty = abs(float(qty))
            except (TypeError, ValueError):
                continue
            if iv > 0 and qty > 0:
                num += iv * qty
                den += qty
        return round(num / den, 4) if den > 0 else None

    def _rule_iv_rv(self, legs: List[Any]) -> None:
        """Raise IV_RV_DISLOCATION when the IV/RV edge flips state.

        The first observation only sets the baseline; ``unknown`` (no legs /
        no realised vol) never alerts.
        """
        iv = self._atm_iv_proxy(legs)
        realized = self.regime.realized_vol()
        status = iv_rv_factor(iv, realized, self.config.iv_rv_buffer).status
        if status == "unknown":
            return
        previous = self._last_iv_rv_status
        self._last_iv_rv_status = status
        if previous is None or previous == status:
            return
        t = AlertType.IV_RV_DISLOCATION.value
        self.broker.resolve_missing(t, [], reason="superseded")
        severity = Severity.WARNING.value if status == "buy_vol" else Severity.INFO.value
        self.broker.raise_alert(
            t,
            severity,
            f"IV/RV dislocation: {previous} → {status} (IV {iv:.1f} vs RV {realized:.1f})",
            data={
                "state": status,
                "previous": previous,
                "iv": iv,
                "realized_vol": realized,
                "buffer": float(self.config.iv_rv_buffer),
                "summary": f"Implied vol is {status.replace('_', ' ')} versus realised",
            },
        )

    def _rule_correlation(self) -> None:
        t = AlertType.CORRELATION_SPIKE.value
        result = self.correlation_matrix(None)
        active = []
        for a in result.get("alerts", []):
            subject = "|".join(sorted([a["id_a"], a["id_b"]]))
            active.append(subject)
            self.broker.raise_alert(
                t,
                Severity.WARNING.value,
                f"Correlation {a['strategy_a']} ↔ {a['strategy_b']}: "
                f"{a['correlation']:.2f} (threshold {a['threshold']:.2f})",
                data={**a, "summary": f"{a['samples']} aligned samples"},
                subject=subject,
            )
        self.broker.resolve_missing(t, active)

    def _market_open(self) -> bool:
        try:
            from backtest.forward.feed_registry import _BrokerBarFeedBase

            return bool(_BrokerBarFeedBase._market_open())
        except Exception:  # noqa: BLE001
            return True

    def _rule_feed_stale(self, roster: Dict[str, Dict[str, Any]]) -> None:
        t = AlertType.DATA_FEED_STALE.value
        limit = float(self.config.feed_stale_s)
        now = time.time()
        stale: Dict[str, Dict[str, Any]] = {}
        market_open: Optional[bool] = None
        for iid, r in roster.items():
            if r.get("status") != "RUNNING":
                continue
            # A runner row with no recorded source is this deployment's broker
            # feed (config/data_sources.yaml), never a named synthetic one —
            # the literal here chose the alert path by accident.
            source = str(r.get("source") or default_broker_source()).lower()
            if source in BROKER_SOURCES:
                if market_open is None:
                    market_open = self._market_open()
                if not market_open:
                    continue
            since = self._registered_at.get(iid, now)
            for sym in r.get("symbols") or []:
                last = self._last_bar_wall.get(str(sym).upper())
                age = now - (last if last is not None else since)
                if age > limit:
                    entry = stale.setdefault(
                        source, {"source": source, "symbols": set(), "runners": set(), "age": 0.0}
                    )
                    entry["symbols"].add(str(sym).upper())
                    entry["runners"].add(r["name"])
                    entry["age"] = max(entry["age"], age)
        for source, e in stale.items():
            self.broker.raise_alert(
                t,
                Severity.CRITICAL.value,
                f"Data feed stale ({source}): no bar for {e['age']:.0f}s "
                f"(limit {limit:.0f}s)",
                data={
                    "source": source,
                    "age_s": round(e["age"], 1),
                    "threshold_s": limit,
                    "symbols": sorted(e["symbols"]),
                    "runners": sorted(e["runners"]),
                    "summary": "Symbols: " + ", ".join(sorted(e["symbols"])[:6]),
                },
                subject=source,
            )
        self.broker.resolve_missing(t, list(stale))

    # ------------------------------------------------------------------ #
    # Market activity ingestion (event alerts)
    # ------------------------------------------------------------------ #

    def ingest_chain(
        self, underlying: str, rows: List[Dict[str, Any]], ts: Any = None, source: str = "chain"
    ) -> Dict[str, Any]:
        anomalies, dryups = self.activity.ingest(underlying, rows, ts=ts, source=source)
        held = self._held_strikes()
        for a in anomalies:
            key = f"{a['underlying']}:{a['strike']:g}{a['option_type']}"
            self.broker.raise_alert(
                AlertType.OI_ANOMALY.value,
                Severity.INFO.value,
                f"OI spike {a['symbol']}: {a['oi_change']:+,.0f} "
                f"({a['multiplier']:.1f}× normal)",
                data={
                    **a,
                    "held": (a["underlying"], float(a["strike"])) in held,
                    "summary": f"avg |ΔOI| {a['avg_change']:,.0f}",
                },
                subject=key,
            )
        for d in dryups:
            key = f"{d['underlying']}:{d['strike']:g}{d['option_type']}"
            self.broker.raise_alert(
                AlertType.LIQUIDITY_DRY_UP.value,
                Severity.INFO.value,
                f"Spread widened on {d['symbol']}: {d['spread']:.2f} "
                f"({d['multiplier']:.1f}× avg {d['avg_spread']:.2f})",
                data={**d, "held": (d["underlying"], float(d["strike"])) in held},
                subject=key,
            )
        return {"oi_anomalies": anomalies, "liquidity_events": dryups}

    def _held_strikes(self) -> set:
        try:
            legs = self.snapshot(None)["legs"]
        except Exception:  # noqa: BLE001
            return set()
        return {(leg.underlying, float(leg.strike)) for leg in legs if leg.strike}

    def ingest_vix(self, value: float, source: str = "manual") -> Dict[str, Any]:
        self.regime.ingest_vix(value, source=source)
        self.maybe_evaluate(force=True)
        return self.regime_snapshot()

    # ------------------------------------------------------------------ #
    # Persistence + background loop
    # ------------------------------------------------------------------ #

    def _persist(self, greeks: Dict[str, Any], conc: Dict[str, Any]) -> None:
        if self.persister is None:
            return
        now = time.monotonic()
        if now - self._last_greeks_persist >= self.config.greeks_history_s and greeks.get("legs"):
            self._last_greeks_persist = now
            try:
                self.persister.record_greeks(greeks, conc)
            except Exception:  # noqa: BLE001
                logger.debug("[intelligence] greeks persist failed", exc_info=True)
        sample = self.regime.last_sample()
        if sample and sample.get("ts") != self._last_regime_persisted:
            self._last_regime_persisted = sample.get("ts")
            try:
                self.persister.record_regime(sample)
            except Exception:  # noqa: BLE001
                logger.debug("[intelligence] regime persist failed", exc_info=True)

    def start(self, interval: Optional[float] = None) -> None:
        """Background evaluator (web app only — tests drive evaluate())."""
        if self._thread is not None and self._thread.is_alive():
            return
        if interval is not None:
            self.config.evaluate_interval_s = max(0.2, float(interval))
        self._stop.clear()

        def loop() -> None:
            logger.info(
                "[intelligence] evaluator started (every %.1fs)", self.config.evaluate_interval_s
            )
            while not self._stop.wait(self.config.evaluate_interval_s):
                # Refresh the (cached) factor inputs so the trend rule sees a
                # fresh status without the tick path ever touching the DB.
                self._factor_inputs()
                self.maybe_evaluate()

        self._thread = threading.Thread(target=loop, daemon=True, name="portfolio-intelligence")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        if self.persister is not None:
            try:
                self.persister.stop()
            except Exception:  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
