"""OI anomaly and liquidity (bid-ask spread) detection from chain snapshots.

Input: option-chain rows — ``{"strike", "option_type", "oi", "bid", "ask",
"trading_symbol"?}`` — pushed by the chain snapshot recorder (live chains)
or ``POST /api/market/chain-activity``. Synthetic chains carry no OI and a
fixed spread, so on synthetic data this monitor honestly reports "no OI
data" instead of inventing anomalies.

* **OI anomaly** — a strike's |ΔOI| between snapshots exceeds
  ``oi_spike_multiplier`` × its rolling mean |ΔOI| (after
  ``oi_min_history`` changes of history).
* **Liquidity dry-up** — a contract's spread exceeds ``spread_multiplier`` ×
  its rolling average spread.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

HISTORY = 20
MAX_EVENTS = 200


def _f(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v


class MarketActivityMonitor:
    def __init__(
        self,
        oi_multiplier: float = 3.0,
        oi_min_history: int = 3,
        spread_multiplier: float = 2.0,
    ) -> None:
        self.oi_multiplier = float(oi_multiplier)
        self.oi_min_history = int(oi_min_history)
        self.spread_multiplier = float(spread_multiplier)
        self._lock = threading.Lock()
        self._last_oi: Dict[tuple, float] = {}
        self._oi_changes: Dict[tuple, Deque[float]] = {}
        self._spreads: Dict[tuple, Deque[float]] = {}
        self._anomalies: Deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._liquidity: Deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._snapshots: Dict[str, Dict[str, Any]] = {}

    def ingest(
        self,
        underlying: str,
        rows: Iterable[Dict[str, Any]],
        ts: Any = None,
        source: str = "chain",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Feed one chain snapshot. Returns ``(oi_anomalies, liquidity_events)``."""
        u = str(underlying).upper()
        stamp = ts.isoformat() if isinstance(ts, datetime) else (
            str(ts) if ts else datetime.now(timezone.utc).isoformat()
        )
        anomalies: List[Dict[str, Any]] = []
        dryups: List[Dict[str, Any]] = []
        rows = list(rows)
        with_oi = 0
        with self._lock:
            for row in rows:
                strike = _f(row.get("strike"))
                opt = str(row.get("option_type") or "").upper()
                if strike is None or opt not in ("CE", "PE"):
                    continue
                key = (u, strike, opt)
                label = row.get("trading_symbol") or f"{u} {strike:g}{opt}"
                oi = _f(row.get("oi", row.get("open_interest")))
                if oi is not None and oi > 0:
                    with_oi += 1
                    prev = self._last_oi.get(key)
                    self._last_oi[key] = oi
                    if prev is not None:
                        change = oi - prev
                        hist = self._oi_changes.setdefault(key, deque(maxlen=HISTORY))
                        if len(hist) >= self.oi_min_history:
                            avg = sum(abs(c) for c in hist) / len(hist)
                            if avg > 0 and abs(change) > self.oi_multiplier * avg:
                                event = {
                                    "underlying": u,
                                    "strike": strike,
                                    "option_type": opt,
                                    "symbol": label,
                                    "oi": oi,
                                    "oi_change": change,
                                    "avg_change": round(avg, 2),
                                    "multiplier": round(abs(change) / avg, 2),
                                    "timestamp": stamp,
                                    "source": source,
                                }
                                anomalies.append(event)
                                self._anomalies.append(event)
                        hist.append(change)
                bid, ask = _f(row.get("bid")), _f(row.get("ask"))
                if bid is not None and ask is not None and bid > 0 and ask >= bid:
                    spread = ask - bid
                    hist_s = self._spreads.setdefault(key, deque(maxlen=HISTORY))
                    if len(hist_s) >= 3:
                        avg_s = sum(hist_s) / len(hist_s)
                        if avg_s > 0 and spread > self.spread_multiplier * avg_s:
                            event = {
                                "underlying": u,
                                "strike": strike,
                                "option_type": opt,
                                "symbol": label,
                                "spread": round(spread, 2),
                                "avg_spread": round(avg_s, 2),
                                "multiplier": round(spread / avg_s, 2),
                                "timestamp": stamp,
                                "source": source,
                            }
                            dryups.append(event)
                            self._liquidity.append(event)
                    hist_s.append(spread)
            self._snapshots[u] = {
                "last_snapshot": stamp,
                "rows": len(rows),
                "rows_with_oi": with_oi,
                "source": source,
            }
        return anomalies, dryups

    def activity(self, symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        sym = str(symbol).upper() if symbol else None
        with self._lock:
            anomalies = [a for a in self._anomalies if sym is None or a["underlying"] == sym]
            liquidity = [e for e in self._liquidity if sym is None or e["underlying"] == sym]
            snapshots = (
                {sym: self._snapshots.get(sym)} if sym else dict(self._snapshots)
            )
        has_oi = any((s or {}).get("rows_with_oi") for s in snapshots.values())
        note = None
        if not snapshots or all(v is None for v in snapshots.values()):
            note = "No option-chain snapshots received yet (live chains feed this monitor)."
        elif not has_oi:
            note = "Chain snapshots carry no open interest (synthetic chains have none)."
        return {
            "symbol": sym,
            "anomalies": list(reversed(anomalies))[:limit],
            "liquidity": list(reversed(liquidity))[:limit],
            "snapshots": snapshots,
            "has_oi_data": has_oi,
            "note": note,
            "thresholds": {
                "oi_multiplier": self.oi_multiplier,
                "oi_min_history": self.oi_min_history,
                "spread_multiplier": self.spread_multiplier,
            },
        }
