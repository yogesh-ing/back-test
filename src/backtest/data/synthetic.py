from __future__ import annotations

from hashlib import sha256

import numpy as np
import pandas as pd

from backtest.logging_config import get_logger

from .base import CANDLE_COLUMNS, normalize_candles

log = get_logger(__name__)


class SyntheticSource:
    #: Synthetic bars are always business-day spaced; other intervals are ignored.
    SUPPORTED_INTERVALS = ("day", "B")

    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        start_dt = pd.Timestamp(start)
        end_dt = pd.Timestamp(end)
        idx = pd.date_range(start=start_dt, end=end_dt, freq="B")

        if interval not in self.SUPPORTED_INTERVALS:
            log.warning(
                "[synthetic] interval %r is not supported — returning daily business bars "
                "(gap G6: timeframe is cosmetic unless the source is 'db')", interval,
            )
        if len(idx) <= 50:
            log.warning("[synthetic] %s %s..%s → only %d bars (need > 50)",
                        symbol, start, end, len(idx))
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

        log.debug("[synthetic] %s %s..%s → %d bars (%.2f → %.2f)", symbol, start, end,
                  len(frame), frame["close"].iloc[0], frame["close"].iloc[-1])
        return normalize_candles(frame)
