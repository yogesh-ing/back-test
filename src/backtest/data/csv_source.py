from __future__ import annotations

import os

import pandas as pd

from .base import normalize_candles


class CsvSource:
    def __init__(self, root: str = "data") -> None:
        self.root = root

    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        path = os.path.join(self.root, f"{symbol}.csv")
        df = pd.read_csv(path)

        if "date" in df.columns:
            df = df.rename(columns={"date": "datetime"})

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.set_index("datetime")

        return normalize_candles(df)
