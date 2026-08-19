from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]


@runtime_checkable
class DataSource(Protocol):
    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
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
