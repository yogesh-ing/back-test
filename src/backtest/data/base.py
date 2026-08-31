from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]

#: The ONE canonical timeframe vocabulary (ticket P4.3). Every layer — API,
#: config, DB (``market_data_cache.timeframe`` CHECK), UI, feeds — speaks
#: these names. Resolved with the lead as the descriptive set.
CANONICAL_TIMEFRAMES = ("1min", "5min", "15min", "1hour", "4hour", "1day", "1week")

#: Canonical timeframe -> mStock TypeA wire interval. Broker-specific
#: translation only; the rest of the codebase never speaks these names.
#: ``4hour`` has no mStock historical equivalent (intentionally absent).
MSTOCK_INTERVAL_MAP = {
    "1min": "minute",
    "5min": "5minute",
    "15min": "15minute",
    "1hour": "60minute",
    "1day": "day",
    "1week": "week",
}


@runtime_checkable
class DataSource(Protocol):
    def get_candles(self, symbol: str, start: str, end: str, interval: str = "1day") -> pd.DataFrame:
        ...


def normalize_candles(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        raise ValueError("candles frame is required")

    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]

    missing = [c for c in CANDLE_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    if out.empty:
        raise ValueError("candles frame is empty")

    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("candles index must be DatetimeIndex")

    out = out.loc[:, CANDLE_COLUMNS]
    out = out[~out.index.duplicated(keep="last")]
    out = out.sort_index()
    out = out.dropna(subset=["close"])

    if out.empty:
        raise ValueError("candles frame is empty after cleaning")

    for col in CANDLE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="raise")

    return out
