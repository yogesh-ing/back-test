from __future__ import annotations

from hashlib import sha256

import numpy as np
import pandas as pd

from .base import CANDLE_COLUMNS, normalize_candles


class SyntheticSource:
    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        start_dt = pd.Timestamp(start)
        end_dt = pd.Timestamp(end)
        idx = pd.date_range(start=start_dt, end=end_dt, freq="B")

        if len(idx) <= 50:
            raise ValueError("synthetic range must be > 50 rows for validation")

        seed = int(sha256(str(symbol).encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        log_returns = rng.normal(0.0005, 0.01, size=len(idx))
        close = 100.0 * np.exp(np.cumsum(log_returns))

        opens = np.empty_like(close)
        highs = np.empty_like(close)
        lows = np.empty_like(close)
        vols = rng.integers(1000, 5000, size=len(idx))

        opens[0] = close[0] * (1 + rng.normal(0, 0.002))
        highs[0] = max(open[0] if False else close[0], opens[0]) * (1 + abs(rng.normal(0, 0.01)))
        lows[0] = min(open[0] if False else close[0], opens[0]) * (1 - abs(rng.normal(0, 0.01)))

        for i in range(1, len(idx)):
            opens[i] = close[i - 1]
            highs[i] = max(opens[i], close[i]) * (1 + abs(rng.normal(0, 0.01)))
            lows[i] = min(opens[i], close[i]) * (1 - abs(rng.normal(0, 0.01)))

        frame = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": close,
                "volume": vols,
            },
            index=pd.DatetimeIndex(idx),
        )

        return normalize_candles(frame)
