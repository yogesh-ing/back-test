"""Strategy P&L correlation.

Runner equity curves are recorded on wall-clock timestamps and decimated
when they fill, so two runners' curves never line up point-for-point. This
calculator keeps its **own** aligned samples instead: at every portfolio tick
it records each runner's equity under one shared sample index, so the
per-sample P&L changes of any two runners are directly comparable.

Correlation is Pearson on per-sample equity changes over the most recent
``window`` samples. A pair needs ``min_samples`` overlapping changes, and a
series with zero variance (a flat runner) yields ``None`` — "unknown", not 0.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np


class CorrelationCalculator:
    def __init__(
        self,
        window: int = 240,
        min_samples: int = 30,
        warning: float = 0.80,
        cache_s: float = 300.0,
    ) -> None:
        self.window = int(window)
        self.min_samples = int(min_samples)
        self.warning = float(warning)
        self.cache_s = float(cache_s)
        self._samples: Dict[str, Deque[Tuple[int, float]]] = {}
        self._index = 0
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_key: Optional[tuple] = None
        self._cache_ts = 0.0

    # ------------------------------------------------------------------ #
    # Sampling
    # ------------------------------------------------------------------ #

    def record(self, equities: Dict[str, float]) -> int:
        """Record one aligned sample of every runner's equity."""
        with self._lock:
            self._index += 1
            for sid, eq in equities.items():
                try:
                    value = float(eq)
                except (TypeError, ValueError):
                    continue
                buf = self._samples.setdefault(sid, deque(maxlen=self.window + 1))
                buf.append((self._index, value))
            return self._index

    def forget(self, keep: List[str]) -> None:
        keep_set = set(keep)
        with self._lock:
            for sid in list(self._samples):
                if sid not in keep_set:
                    del self._samples[sid]

    def sample_count(self, sid: str) -> int:
        with self._lock:
            return len(self._samples.get(sid, ()))

    # ------------------------------------------------------------------ #
    # Matrix
    # ------------------------------------------------------------------ #

    def _series(self, members: List[str]) -> Dict[int, float]:
        """Summed equity per sample index across ``members`` (only indexes
        every member has — a group is a portfolio of its runners)."""
        maps = [dict(self._samples.get(m, ())) for m in members]
        if not maps or any(not m for m in maps):
            return {}
        common = set(maps[0]).intersection(*maps[1:]) if len(maps) > 1 else set(maps[0])
        return {i: sum(m[i] for m in maps) for i in common}

    @staticmethod
    def _pearson(
        a: Dict[int, float], b: Dict[int, float], min_n: int
    ) -> Tuple[Optional[float], int]:
        common = sorted(set(a) & set(b))
        if len(common) < 2:
            return None, 0
        xa = np.diff(np.array([a[i] for i in common], dtype=float))
        xb = np.diff(np.array([b[i] for i in common], dtype=float))
        n = int(len(xa))
        if n < min_n:
            return None, n
        if float(np.std(xa)) < 1e-12 or float(np.std(xb)) < 1e-12:
            return None, n
        value = float(np.corrcoef(xa, xb)[0, 1])
        if not np.isfinite(value):
            return None, n
        return round(value, 4), n

    def matrix(
        self,
        groups: Dict[str, Dict[str, Any]],
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Correlation matrix across ``groups``.

        ``groups`` maps a group id → ``{"label": str, "members": [runner ids]}``
        (one member per runner for the default per-runner view; several for
        ``group_by=strategy``).
        """
        key = tuple(sorted((gid, tuple(sorted(g["members"]))) for gid, g in groups.items()))
        now = time.monotonic()
        if (
            use_cache
            and self._cache is not None
            and self._cache_key == key
            and now - self._cache_ts < self.cache_s
        ):
            return {**self._cache, "cached": True}
        with self._lock:
            ids = list(groups)
            series = {gid: self._series(list(groups[gid]["members"])) for gid in ids}
        labels = [groups[g]["label"] for g in ids]
        n = len(ids)
        values: List[List[Optional[float]]] = [[None] * n for _ in range(n)]
        samples: List[List[int]] = [[0] * n for _ in range(n)]
        alerts: List[Dict[str, Any]] = []
        for i in range(n):
            values[i][i] = 1.0 if len(series[ids[i]]) > self.min_samples else None
            samples[i][i] = max(0, len(series[ids[i]]) - 1)
            for j in range(i + 1, n):
                corr, count = self._pearson(series[ids[i]], series[ids[j]], self.min_samples)
                values[i][j] = values[j][i] = corr
                samples[i][j] = samples[j][i] = count
                if corr is not None and corr > self.warning:
                    alerts.append(
                        {
                            "type": "high_correlation",
                            "strategy_a": labels[i],
                            "strategy_b": labels[j],
                            "id_a": ids[i],
                            "id_b": ids[j],
                            "correlation": corr,
                            "samples": count,
                            "threshold": self.warning,
                        }
                    )
        result = {
            # PRD shape: first row = labels, then the numeric rows.
            "matrix": [labels] + values,
            "labels": labels,
            "ids": ids,
            "values": values,
            "samples": samples,
            "alerts": alerts,
            "threshold": self.warning,
            "min_samples": self.min_samples,
            "window": self.window,
            "computed_at": time.time(),
            "cached": False,
            "method": "pearson on per-tick equity changes (aligned samples)",
        }
        self._cache, self._cache_key, self._cache_ts = result, key, now
        return result
