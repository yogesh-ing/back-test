"""PostgreSQL data source for backtest engine -- reads from market_data_cache.

Supports on-the-fly resampling: stores 1min data, queries resample to any interval.
"""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from backtest.data.base import normalize_candles

# Pandas resample rule mapping: our interval names -> pandas offset aliases
_INTERVAL_TO_RULE = {
    "1min": "1min",
    "5min": "5min",
    "15min": "15min",
    "30min": "30min",
    "60min": "1h",
    "1hour": "1h",
    "4hour": "4h",
    "day": "1D",
    "week": "1W",
    "month": "1ME",
}


class DbSource:
    """
    Reads OHLCV candles from market_data_cache (PostgreSQL / TimescaleDB).

    Strategy: always fetch the finest-grained data available (prefer 1min),
    then resample UP to the requested interval using pandas. This means:
    - Store only 1min + day data
    - Support any interval (5min, 15min, 1H, 4H, etc.) via resampling
    - No need to store every possible timeframe separately
    """

    def __init__(self, db_url: Optional[str] = None):
        """
        db_url: SQLAlchemy connection string.
                Falls back to DATABASE_URL env var.
                Falls back to default local Postgres.
        """
        self.db_url = db_url or os.getenv("DATABASE_URL") or os.getenv(
            "FORWARD_TEST_DB_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/forward_test"
        )
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            self._engine = create_engine(self.db_url)
        return self._engine

    def get_candles(
        self,
        symbol: str,
        start: str,
        end: str,
        interval: str = "day",
    ) -> pd.DataFrame:
        """
        Queries market_data_cache for symbol, then resamples to requested interval.

        1. Find finest available timeframe for this symbol (1min preferred)
        2. Query raw bars from DB
        3. Resample to requested interval using pandas
        """
        engine = self._get_engine()

        # Find the best source timeframe (finest available)
        source_tf = self._find_best_source_tf(engine, symbol, interval)

        query = text("""
            SELECT ts, open, high, low, close, volume
            FROM market_data_cache
            WHERE symbol = :symbol
              AND timeframe = :timeframe
              AND ts BETWEEN :start AND :end
            ORDER BY ts ASC
        """)

        df = pd.read_sql(
            query,
            engine,
            params={
                "symbol": symbol,
                "timeframe": source_tf,
                "start": start,
                "end": end,
            },
        )

        if df.empty:
            raise ValueError(
                f"Symbol '{symbol}' not found in database for timeframe '{source_tf}' "
                f"between {start} and {end}. "
                f"Run fetch_nifty500_historical.py --timeframe 1min to populate."
            )

        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")

        # Resample if the requested interval differs from what we stored
        if source_tf != interval and interval in _INTERVAL_TO_RULE:
            df = self._resample(df, interval)

        return normalize_candles(df)

    def _find_best_source_tf(self, engine, symbol: str, requested: str) -> str:
        """Find the finest-grained timeframe available for this symbol.

        Priority: 1min > 5min > 15min > 30min > 60min > day
        We want the finest so we can resample UP to any coarser interval.
        """
        # If requesting day and we have day data, use it directly (fast, no resample needed)
        if requested == "day":
            check = text("""
                SELECT 1 FROM market_data_cache
                WHERE symbol = :sym AND timeframe = 'day' LIMIT 1
            """)
            with engine.connect() as conn:
                if conn.execute(check, {"sym": symbol}).fetchone():
                    return "day"

        # For intraday: find finest available
        for tf in ["1min", "5min", "15min", "30min", "60min", "1hour"]:
            check = text("""
                SELECT 1 FROM market_data_cache
                WHERE symbol = :sym AND timeframe = :tf LIMIT 1
            """)
            with engine.connect() as conn:
                if conn.execute(check, {"sym": symbol, "tf": tf}).fetchone():
                    return tf

        # Fallback: try whatever was requested
        return requested

    def _resample(self, df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """Resample OHLCV data to a coarser interval.

        Rules:
        - open: first value in the window
        - high: max in the window
        - low: min in the window
        - close: last value in the window
        - volume: sum across the window
        """
        rule = _INTERVAL_TO_RULE.get(interval)
        if not rule:
            raise ValueError(
                f"Unsupported interval '{interval}'. "
                f"Supported: {list(_INTERVAL_TO_RULE.keys())}"
            )

        resampled = df.resample(rule).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["close"])

        return resampled

    def list_symbols(self, timeframe: str = "day") -> list[str]:
        """
        Returns sorted list of distinct symbols available in DB
        for the given timeframe.
        """
        engine = self._get_engine()
        query = text("""
            SELECT DISTINCT symbol FROM market_data_cache
            WHERE timeframe = :timeframe
            ORDER BY symbol ASC
        """)
        with engine.connect() as conn:
            result = conn.execute(query, {"timeframe": timeframe})
            rows = [row[0] for row in result.fetchall()]
        return rows
