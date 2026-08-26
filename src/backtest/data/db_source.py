"""PostgreSQL data source for backtest engine -- reads from market_data_cache."""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from backtest.data.base import normalize_candles


class DbSource:
    """
    Reads OHLCV candles from market_data_cache (PostgreSQL / TimescaleDB).

    Implements the DataSource protocol:
        get_candles(symbol, start, end, interval) -> pd.DataFrame
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
        Queries market_data_cache for symbol+interval in [start, end].
        """
        if interval != "day":
            raise ValueError(
                f"Only 'day' interval available in database. Requested: '{interval}'"
            )

        engine = self._get_engine()
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
                "timeframe": interval,
                "start": start,
                "end": end,
            },
        )

        if df.empty:
            # Check if symbol exists at all for this interval
            check_query = text("""
                SELECT 1 FROM market_data_cache
                WHERE symbol = :symbol AND timeframe = :timeframe
                LIMIT 1
            """)
            with engine.connect() as conn:
                exists = conn.execute(
                    check_query,
                    {"symbol": symbol, "timeframe": interval},
                ).fetchone()
            if exists is None:
                raise ValueError(
                    f"Symbol '{symbol}' not found in database for timeframe '{interval}' "
                    f"between {start} and {end}. "
                    f"Run fetch_nifty500_historical.py to populate."
                )
            # Symbol exists but no rows in date range — return empty? But normalize_candles
            # rejects empty frames. Per PRD: descriptive error.
            raise ValueError(
                f"Symbol '{symbol}' not found in database for timeframe '{interval}' "
                f"between {start} and {end}. "
                f"Run fetch_nifty500_historical.py to populate."
            )

        df = df.rename(columns={"ts": "ts"})
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")
        return normalize_candles(df)

    def list_symbols(self, timeframe: str = "day") -> list[str]:
        """
        Returns sorted list of distinct symbols available in DB
        for the given timeframe.
        """
        if timeframe != "day":
            # Per PRD, only 'day' is supported
            return []

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
