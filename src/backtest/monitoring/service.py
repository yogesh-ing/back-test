"""PortfolioMonitor — the meta-layer above every strategy.

One object per portfolio manager. It owns the analytics, the alert book and a
short-lived snapshot cache, and exposes two entry points:

* :meth:`snapshot` — the API/UI read (cached for ``cache_ttl_s`` because the
  page polls at 1 Hz and several panels share one evaluation);
* :meth:`sweep`    — the server-side pass the manager runs every
  ``sweep_every_ticks`` feed ticks, so alerts fire and land in the audit log
  with no browser open. It never fetches external data (tick-safe): the
  regime pass reads the bars the runners already hold.

Historical data for the regime panel (when the runners have too few bars) is
pulled only on an explicit API read, through an injected ``history_loader``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

import pandas as pd

from backtest.monitoring.alerts import EVENT_RESOLVED, AlertBook
from backtest.monitoring.collector import PositionCollector
from backtest.monitoring.concentration import ConcentrationMonitor
from backtest.monitoring.config import MonitorConfig, load_monitor_config
from backtest.monitoring.correlation import CorrelationMonitor
from backtest.monitoring.greeks import PortfolioGreeksAggregator
from backtest.monitoring.models import Alert, PortfolioInputs, StrategyBook, sort_alerts
from backtest.monitoring.regime import MarketRegimeDetector, bars_to_frame, legs_side

logger = logging.getLogger("backtest.monitoring")

SECTIONS = ("greeks", "concentration", "correlation", "regime")
MIN_REGIME_BARS = 25

#: (symbol) -> OHLC DataFrame, or None. Must be safe to call from a request.
HistoryLoader = Callable[[str], Optional[pd.DataFrame]]


def _alerts_from(section: Dict[str, Any]) -> List[Alert]:
    out = []
    for raw in section.get("alerts") or []:
        out.append(
            Alert(**{k: raw.get(k) for k in (
                "key", "category", "severity", "title", "message", "recommendation",
                "metric", "value", "threshold", "subject",
            )}, context=raw.get("context") or {})
        )
    return out


class PortfolioMonitor:
    def __init__(
        self,
        manager: Any,
        config: Optional[MonitorConfig] = None,
        history_loader: Optional[HistoryLoader] = None,
        audit: Optional[Callable[..., None]] = None,
    ) -> None:
        self.manager = manager
        self.config = config or load_monitor_config()
        self.history_loader = history_loader
        self._audit = audit
        self.collector = PositionCollector(self.config)
        self.greeks = PortfolioGreeksAggregator(self.config)
        self.concentration = ConcentrationMonitor(self.config)
        self.correlation = CorrelationMonitor(self.config)
        self.regime = MarketRegimeDetector(self.config)
        self.alerts = AlertBook()
        self._lock = threading.RLock()
        self._cache: Dict[tuple, tuple] = {}
        self._history_cache: Dict[str, tuple] = {}
        self.last_sweep: Optional[Dict[str, Any]] = None
        #: Same-instant equity samples of every runner, one per feed tick —
        #: aligned by construction, so correlation never depends on runners'
        #: wall-clock stamps lining up (they don't in fast replay).
        self._samples: Deque[Dict[str, Any]] = deque(
            maxlen=max(self.config.correlation.lookback + 1, 50)
        )

    # ------------------------------------------------------------------ #
    # Tick sampling (correlation input)
    # ------------------------------------------------------------------ #

    def record_tick(self, tick_ts: Any, runners: List[Any]) -> None:
        """Sample every runner's equity at one instant (called once per tick)."""
        point: Dict[str, Any] = {"ts": str(tick_ts), "equity": {}}
        for runner in runners:
            try:
                point["equity"][runner.instance_id] = float(runner.equity())
            except Exception:  # noqa: BLE001 — one bad runner must not drop the tick
                continue
        self._samples.append(point)

    def _sampled_equity(self, books: List[StrategyBook]) -> pd.DataFrame:
        """Tick samples → one aligned equity frame (rows = ticks, columns =
        strategy names). A runner added mid-stream is NaN before its first
        sample, which the pairwise correlation handles."""
        ids = {b.strategy_id: b.strategy_name or b.strategy_id for b in books}
        rows = [
            {sid: point["equity"].get(sid) for sid in ids}
            for point in list(self._samples)
        ]
        frame = pd.DataFrame(rows, columns=list(ids), dtype=float)
        labels, seen = [], set()
        for sid in frame.columns:
            label = ids[sid]
            if label in seen:  # two runners with one name — keep both, distinctly
                label = f"{label} ({sid[:6]})"
            seen.add(label)
            labels.append(label)
        frame.columns = labels
        return frame

    def _correlation_section(self, inputs: PortfolioInputs, lookback: Optional[int]
                             ) -> Dict[str, Any]:
        """Prefer same-tick samples; fall back to runner equity curves
        (e.g. right after a restart, before enough ticks were sampled)."""
        runner_books = [b for b in inputs.strategies if b.book == "runner"]
        if len(self._samples) > self.config.correlation.min_observations and runner_books:
            report = self.correlation.calculate_from_equity(
                self._sampled_equity(runner_books), lookback
            )
            report["series_source"] = "tick_samples"
        else:
            report = self.correlation.calculate(runner_books, lookback)
            report["series_source"] = "equity_curves"
        return report

    # ------------------------------------------------------------------ #
    # Regime inputs
    # ------------------------------------------------------------------ #

    def _history(self, symbol: str) -> Optional[pd.DataFrame]:
        if self.history_loader is None:
            return None
        cached = self._history_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            frame = self.history_loader(symbol)
        except Exception:  # noqa: BLE001 — a data-source hiccup is not an outage
            logger.info("[monitor] history load failed for %s", symbol, exc_info=True)
            frame = None
        self._history_cache[symbol] = (time.monotonic(), frame)
        return frame

    def _regime_symbol(self, inputs: PortfolioInputs, requested: Optional[str]) -> str:
        if requested:
            return requested.upper()
        bench = self.config.regime.benchmark.upper()
        held = [p.underlying for p in inputs.positions]
        if bench in held or not held:
            return bench
        # The most-exposed underlying the book actually holds.
        weights: Dict[str, float] = {}
        for p in inputs.positions:
            weights[p.underlying] = weights.get(p.underlying, 0.0) + p.notional
        return max(weights, key=weights.get)

    def _regime_section(self, inputs: PortfolioInputs, symbol: Optional[str],
                        allow_fetch: bool) -> Dict[str, Any]:
        sym = self._regime_symbol(inputs, symbol)
        frame = bars_to_frame(inputs.bars.get(sym, []))
        source = "runner_bars"
        if len(frame) < MIN_REGIME_BARS and allow_fetch:
            hist = self._history(sym)
            if hist is not None and len(hist) > len(frame):
                frame = hist
                source = f"history:{hist.attrs.get('source', 'unknown')}"
                vix = self._history(self.config.regime.vol_index_symbol)
                if vix is not None and not vix.empty and "close" in vix:
                    frame = frame.copy()
                    frame["vix"] = vix["close"].reindex(frame.index, method="ffill")
        ivs = [p.iv for p in inputs.positions
               if p.is_option and p.underlying == sym and p.iv
               and p.iv_source in ("contract", "implied")]
        implied = sum(ivs) / len(ivs) if ivs else None
        regime = self.regime.detect(frame, symbol=sym, implied_vol=implied, data_source=source)
        leg_sides: Dict[str, List[str]] = {}
        for p in inputs.positions:
            if p.is_option:
                leg_sides.setdefault(p.strategy_id, []).append(p.side)
        fits = [self.regime.strategy_fit(b, regime, legs_side(leg_sides.get(b.strategy_id, [])))
                for b in inputs.strategies if b.book == "runner"]
        regime["strategy_fit"] = fits
        regime["alerts"] = [a.to_dict() for a in self.regime.check(regime, fits)]
        regime["available_symbols"] = sorted(inputs.bars)
        return regime

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        mode: Optional[str] = None,
        sections: Optional[List[str]] = None,
        symbol: Optional[str] = None,
        lookback: Optional[int] = None,
        allow_fetch: bool = True,
    ) -> Dict[str, Any]:
        wanted = [s for s in (sections or SECTIONS) if s in SECTIONS]
        inputs = self.collector.collect(self.manager, mode)
        out: Dict[str, Any] = {
            "as_of": inputs.as_of,
            "mode": inputs.mode,
            "summary": {
                "positions": len(inputs.positions),
                "option_legs": sum(1 for p in inputs.positions if p.is_option),
                "strategies": len(inputs.strategies),
                "capital": round(inputs.total_capital, 2),
                "equity": round(inputs.total_equity, 2),
                "daily_pnl": round(inputs.daily_pnl, 2),
                "daily_loss_limit": inputs.daily_loss_limit,
            },
        }
        leg_deltas: Dict[str, float] = {}
        if "greeks" in wanted or "concentration" in wanted:
            greeks = self.greeks.calculate(inputs)
            leg_deltas = {leg["position_id"]: leg["delta_units"] or 0.0
                          for leg in greeks["legs"]}
            if "greeks" in wanted:
                out["greeks"] = greeks
        if "concentration" in wanted:
            out["concentration"] = self.concentration.calculate(inputs, leg_deltas)
        if "correlation" in wanted:
            out["correlation"] = self._correlation_section(inputs, lookback)
        if "regime" in wanted:
            out["regime"] = self._regime_section(inputs, symbol, allow_fetch)

        raw: List[Alert] = []
        for name in wanted:
            raw.extend(_alerts_from(out.get(name) or {}))
        # Categories this pass is authoritative for (scenario rides with greeks).
        categories = list(wanted) + (["scenario"] if "greeks" in wanted else [])
        events = self.alerts.reconcile(inputs.mode, sort_alerts(raw), categories=categories)
        self._emit(events)
        out["alerts"] = self.alerts.active(inputs.mode)
        out["alert_counts"] = self.alerts.counts(inputs.mode)
        out["events"] = events
        return out

    def snapshot(self, mode: Optional[str] = None, sections: Optional[List[str]] = None,
                 symbol: Optional[str] = None, lookback: Optional[int] = None
                 ) -> Dict[str, Any]:
        key = (mode, tuple(sections or SECTIONS), symbol, lookback)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self.config.cache_ttl_s:
                return hit[1]
            result = self.evaluate(mode=mode, sections=sections, symbol=symbol,
                                   lookback=lookback)
            self._cache[key] = (time.monotonic(), result)
            if len(self._cache) > 64:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            return result

    def sweep(self) -> Dict[str, Any]:
        """Tick-driven pass over the whole book (all buckets). Never fetches."""
        with self._lock:
            result = self.evaluate(mode=None, allow_fetch=False)
            self.last_sweep = {
                "as_of": result["as_of"],
                "alert_counts": result["alert_counts"],
                "events": len(result["events"]),
            }
            return result

    def _emit(self, events: List[Dict[str, Any]]) -> None:
        for ev in events:
            if ev["severity"] == "info" and ev["event"] != EVENT_RESOLVED:
                continue  # info findings are UI-only; not audit-worthy
            if ev["event"] == EVENT_RESOLVED and ev["severity"] == "info":
                continue
            level = logging.WARNING if ev["severity"] == "critical" else logging.INFO
            logger.log(level, "[monitor] %s %s · %s — %s", ev["event"].upper(),
                       ev["severity"], ev["title"], ev["message"])
            if self._audit is not None:
                try:
                    self._audit(
                        f"MONITOR_{ev['event'].upper()} · {ev['severity'].upper()} · "
                        f"{ev['title']}",
                        scope="monitor",
                        instance_id=None,
                        detail=f"[{ev['scope']}] {ev['message']}",
                    )
                except Exception:  # noqa: BLE001 — audit must never break evaluation
                    logger.exception("[monitor] audit write failed")
