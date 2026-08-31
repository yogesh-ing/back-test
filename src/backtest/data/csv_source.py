from __future__ import annotations

import os

import pandas as pd

from backtest.logging_config import get_logger

from .base import normalize_candles

log = get_logger(__name__)


class CsvSource:
    """Reads ``{root}/{symbol}.csv`` (daily bars only — ``interval`` is ignored)."""

    #: CSV files are pre-aggregated; we cannot honour intraday requests.
    SUPPORTED_INTERVALS = ("1day",)

    def __init__(self, root: str = "data") -> None:
        self.root = root

    def get_candles(self, symbol: str, start: str, end: str, interval: str = "1day") -> pd.DataFrame:
        path = os.path.join(self.root, f"{symbol}.csv")
        if interval not in self.SUPPORTED_INTERVALS:
            log.warning(
                "[csv] interval %r is not supported — using the file's own bar size as-is "
                "(gap G6: timeframe is cosmetic unless the source is 'db')", interval,
            )
        if not os.path.exists(path):
            log.error("[csv] no such file for %r: %s (looked in root=%r)", symbol, path, self.root)
            raise FileNotFoundError(f"no CSV for {symbol!r} at {path}")
        log.debug("[csv] reading %s", path)
        df = pd.read_csv(path)

        if "date" in df.columns:
            df = df.rename(columns={"date": "datetime"})

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.set_index("datetime")

        out = normalize_candles(df)
        log.debug("[csv] %s → %d bars %s..%s (file covers %s..%s; range filter is the "
                  "caller's job)", symbol, len(out), start, end,
                  out.index[0].date(), out.index[-1].date())
        return out
