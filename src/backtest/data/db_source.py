"""PostgreSQL data source for backtest engine -- reads from market_data_cache.

Supports on-the-fly resampling: stores 1min data, queries resample to any interval.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from backtest.data.base import normalize_candles
from backtest.db.config import get_db_url
from backtest.logging_config import get_logger

log = get_logger(__name__)

# Pandas resample rule mapping: canonical timeframe names -> pandas offsets
# (ticket P4.3: ONE vocabulary end to end).
_INTERVAL_TO_RULE = {
    "1min": "1min",
    "5min": "5min",
    "15min": "15min",
    "1hour": "1h",
    "4hour": "4h",
    "1day": "1D",
    "1week": "1W",
}

#: Finest-grained first — used to pick the stored timeframe to read from.
_SOURCE_TF_PRIORITY = ["1min", "5min", "15min", "1hour", "1day", "1week"]


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
        db_url: SQLAlchemy connection string. When omitted, resolved via the
                single DB-URL authority (:func:`backtest.db.config.get_db_url`).
        """
        # Single DB-URL authority (ticket P4.3): explicit arg >
        # FORWARD_TEST_DB_URL env > config/database.yaml profile.
        self.db_url = get_db_url(db_url)
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            masked = self.db_url.rsplit("@", 1)[-1] if "@" in self.db_url else self.db_url
            log.debug("[db] creating engine for %s", masked)
            self._engine = create_engine(self.db_url)
        return self._engine

    def get_candles(
        self,
        symbol: str,
        start: str,
        end: str,
        interval: str = "1day",
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

        log.debug("[db] %s tf=%s %s..%s → %d rows", symbol, source_tf, start, end, len(df))
        if df.empty:
            log.warning("[db] no bars for %s (timeframe=%s, %s..%s) — check the symbol exists "
                        "in market_data_cache: SELECT DISTINCT timeframe FROM market_data_cache "
                        "WHERE symbol='%s'", symbol, source_tf, start, end, symbol)
            raise ValueError(
                f"Symbol '{symbol}' not found in database for timeframe '{source_tf}' "
                f"between {start} and {end}. "
                f"Run fetch_nifty500_historical.py --timeframe 1min to populate."
            )

        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")

        # Resample if the requested interval differs from what we stored
        if source_tf != interval:
            if interval in _INTERVAL_TO_RULE:
                log.debug("[db] resampling %s: %s → %s", symbol, source_tf, interval)
                df = self._resample(df, interval)
            else:
                log.warning("[db] interval %r has no resample rule (known: %s) — returning "
                            "stored %s bars unsampled", interval, sorted(_INTERVAL_TO_RULE),
                            source_tf)

        out = normalize_candles(df)
        log.info("[db] %s %s..%s → %d bars @ %s (stored as %s)", symbol, start, end, len(out),
                 interval, source_tf)
        return out

    def _find_best_source_tf(self, engine, symbol: str, requested: str) -> str:
        """Find the finest-grained timeframe available for this symbol.

        Priority: 1min > 5min > 15min > 1hour > 1day > 1week
        We want the finest so we can resample UP to any coarser interval.
        """
        # If requesting daily-or-coarser and we have it stored, use it
        # directly (fast, no resample needed)
        if requested in ("1day", "1week"):
            check = text("""
                SELECT 1 FROM market_data_cache
                WHERE symbol = :sym AND timeframe = :tf LIMIT 1
            """)
            with engine.connect() as conn:
                if conn.execute(check, {"sym": symbol, "tf": requested}).fetchone():
                    return requested

        # For intraday: find finest available
        for tf in _SOURCE_TF_PRIORITY:
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

    def list_symbols(self, timeframe: str = "1day") -> list[str]:
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
        log.info("[db] list_symbols(timeframe=%s) → %d symbols", timeframe, len(rows))
        if not rows:
            log.warning("[db] market_data_cache has no rows for timeframe=%r — check what was "
                        "ingested (SELECT DISTINCT timeframe FROM market_data_cache)", timeframe)
        return rows
