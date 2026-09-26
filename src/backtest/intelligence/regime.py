"""Market regime detection (VIX bands with hysteresis).

Volatility input, in priority order — always labelled in ``source`` so a
proxy can never pass as the real index:

1. **India VIX prints** — bars for any symbol in ``vix_symbols`` (e.g. a
   runner or feed subscribed to ``INDIAVIX``) or ``POST /api/market/vix``.
   Used while fresher than ``vix_stale_s``.
2. **Realized-vol proxy** — annualised close-to-close volatility of the
   benchmark underlying's bars (``regime_benchmark``, default NIFTY), in the
   same "vol points" unit as VIX. The bar period is inferred from the bars'
   own timestamps (floored at one minute — feeds deliver closed 1-min
   candles), so the proxy is frequency-agnostic.

Neither available → regime ``unknown`` (and no regime alerts).

Bands (defaults): ``low_vol`` < 15 ≤ ``moderate_vol`` < 22 ≤ ``high_vol``.
A flip requires crossing a band edge by ``regime_hysteresis`` points, so a
VIX hovering at 15.0 does not flap between regimes every bar.
"""

from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

LOW, MODERATE, HIGH, UNKNOWN = "low_vol", "moderate_vol", "high_vol", "unknown"
RANK = {LOW: 0, MODERATE: 1, HIGH: 2}
LABELS = {
    LOW: "LOW VOLATILITY",
    MODERATE: "MODERATE VOLATILITY",
    HIGH: "HIGH VOLATILITY",
    UNKNOWN: "UNKNOWN",
}
#: NSE cash session: 09:15–15:30 = 22,500 s; 252 sessions a year.
SESSION_SECONDS = 22_500.0
SESSIONS_PER_YEAR = 252.0
#: 30 days of 5-minute samples.
HISTORY_MAX = 30 * 288


def _utc(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if ts:
        try:
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class MarketRegimeDetector:
    def __init__(
        self,
        low_max: float = 15.0,
        high_min: float = 22.0,
        hysteresis: float = 0.5,
        sample_s: float = 300.0,
        benchmark: str = "NIFTY",
        realized_window: int = 60,
        vix_symbols: Optional[List[str]] = None,
        vix_stale_s: float = 900.0,
    ) -> None:
        self.low_max = float(low_max)
        self.high_min = float(high_min)
        self.hysteresis = float(hysteresis)
        self.sample_s = float(sample_s)
        self.benchmark = str(benchmark or "").upper()
        self.realized_window = int(realized_window)
        self.vix_symbols = {s.upper() for s in (vix_symbols or ["INDIAVIX", "INDIA VIX", "VIX"])}
        self.vix_stale_s = float(vix_stale_s)
        self._lock = threading.RLock()
        self._vix: Optional[Tuple[float, datetime, str]] = None  # value, observed_at, source
        self._closes: Deque[Tuple[datetime, float]] = deque(maxlen=self.realized_window + 1)
        self._regime: str = UNKNOWN
        self._regime_since: Optional[datetime] = None
        self._previous_regime: Optional[str] = None
        self._last_transition: Optional[Dict[str, Any]] = None
        self._history: Deque[Dict[str, Any]] = deque(maxlen=HISTORY_MAX)
        self._last_sample_at: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # Inputs
    # ------------------------------------------------------------------ #

    def ingest_vix(self, value: float, ts: Any = None, source: str = "manual") -> None:
        v = float(value)
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"VIX must be a positive number, got {value!r}")
        with self._lock:
            # observed_at is wall-clock receipt time: staleness is about how
            # long ago WE heard it, not the print's own (possibly replayed) ts.
            self._vix = (v, datetime.now(timezone.utc), source)

    def observe_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        sym = str(symbol).upper()
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            return
        if sym in self.vix_symbols:
            self.ingest_vix(close, bar.get("ts"), source=f"feed:{sym}")
            return
        if sym == self.benchmark and close > 0:
            with self._lock:
                self._closes.append((_utc(bar.get("ts")), close))

    # ------------------------------------------------------------------ #
    # Derived values
    # ------------------------------------------------------------------ #

    def realized_vol(self) -> Optional[float]:
        """Annualised realized vol of the benchmark, in vol points (×100)."""
        with self._lock:
            points = list(self._closes)
        if len(points) < max(10, self.realized_window // 3):
            return None
        rets = []
        gaps = []
        for (t0, p0), (t1, p1) in zip(points, points[1:]):
            if p0 > 0 and p1 > 0:
                rets.append(math.log(p1 / p0))
                gaps.append(max(0.0, (t1 - t0).total_seconds()))
        if len(rets) < 2:
            return None
        period = max(60.0, statistics.median(gaps) if gaps else 60.0)
        bars_per_year = SESSIONS_PER_YEAR * SESSION_SECONDS / period
        return round(statistics.pstdev(rets) * math.sqrt(bars_per_year) * 100.0, 2)

    def _input(self, now: datetime) -> Tuple[Optional[float], str, Optional[float]]:
        realized = self.realized_vol()
        with self._lock:
            vix = self._vix
        if vix is not None and (now - vix[1]).total_seconds() <= self.vix_stale_s:
            return vix[0], vix[2], realized
        if realized is not None:
            return realized, f"realized_vol_proxy:{self.benchmark}", realized
        return None, "none", realized

    def classify(self, value: Optional[float], previous: Optional[str] = None) -> str:
        if value is None:
            return UNKNOWN

        def raw(v: float) -> str:
            if v < self.low_max:
                return LOW
            if v >= self.high_min:
                return HIGH
            return MODERATE

        candidate = raw(value)
        if previous not in RANK or candidate == previous:
            return candidate
        # Hysteresis: the move must survive shifting the value back by h.
        shifted = value - self.hysteresis if RANK[candidate] > RANK[previous] else (
            value + self.hysteresis
        )
        confirmed = raw(shifted)
        return previous if confirmed == previous else confirmed

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def evaluate(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Update the regime. Returns a transition dict when it flipped."""
        now = now or datetime.now(timezone.utc)
        value, source, realized = self._input(now)
        transition = None
        with self._lock:
            new = self.classify(value, self._regime)
            if new != self._regime:
                old = self._regime
                if old != UNKNOWN and new != UNKNOWN:
                    transition = {
                        "old_regime": old,
                        "new_regime": new,
                        "vix": value,
                        "source": source,
                        "ts": now.isoformat(),
                    }
                    self._last_transition = transition
                self._previous_regime = old
                self._regime = new
                self._regime_since = now
            due = (
                self._last_sample_at is None
                or (now - self._last_sample_at).total_seconds() >= self.sample_s
            )
            if value is not None and (due or transition is not None):
                self._history.append(
                    {
                        "ts": now.isoformat(),
                        "value": round(value, 2),
                        "regime": self._regime,
                        "source": source,
                        "realized_vol": realized,
                        "previous_regime": self._previous_regime,
                        "regime_changed": transition is not None,
                    }
                )
                self._last_sample_at = now
        return transition

    def last_sample(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._history[-1]) if self._history else None

    def snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        value, source, realized = self._input(now)
        with self._lock:
            history = list(self._history)
            regime = self._regime
            since = self._regime_since
            transition = self._last_transition
        day_ago = now - timedelta(hours=24)
        prior = None
        for row in history:
            if _utc(row["ts"]) <= day_ago:
                prior = row
            else:
                break
        if prior is None and history:
            prior = history[0] if _utc(history[0]["ts"]) < now - timedelta(minutes=1) else None
        prev_value = prior["value"] if prior else None
        change_pct = (
            round((value - prev_value) / prev_value * 100.0, 2)
            if value is not None and prev_value
            else None
        )
        transitioning = bool(
            transition and _utc(transition["ts"]) >= now - timedelta(hours=1)
        )
        return {
            "regime": regime,
            "label": LABELS.get(regime, regime),
            "vix": round(value, 2) if value is not None else None,
            "source": source,
            "is_proxy": source.startswith("realized_vol_proxy"),
            "realized_vol": realized,
            "previous_value": prev_value,
            "previous_value_ts": prior["ts"] if prior else None,
            "vix_change_pct": change_pct,
            "transitioning": transitioning,
            "last_transition": transition,
            "regime_since": since.isoformat() if since else None,
            "bands": {
                "low_max": self.low_max,
                "high_min": self.high_min,
                "hysteresis": self.hysteresis,
            },
            "history": history[-2000:],
            "timestamp": now.isoformat(),
        }


def regime_fit(vix_range: Optional[Tuple[float, float]], vix: Optional[float]) -> Dict[str, Any]:
    """How a strategy's declared VIX range fits the current VIX."""
    if not vix_range:
        return {"status": "any", "label": "Works in any regime", "ok": True}
    lo, hi = float(vix_range[0]), float(vix_range[1])
    text = f"Optimized for VIX {lo:g}-{hi:g}"
    if vix is None:
        return {"status": "unknown", "label": text, "ok": None, "range": [lo, hi]}
    ok = lo <= vix <= hi
    return {
        "status": "favorable" if ok else "unfavorable",
        "label": text,
        "ok": ok,
        "range": [lo, hi],
    }
