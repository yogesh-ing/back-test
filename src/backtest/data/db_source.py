"""PostgreSQL data source for backtest engine -- reads from market_data_cache.

Supports on-the-fly resampling: stores 1min data, queries resample to any interval.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from backtest.data.base import (
    CANONICAL_TIMEFRAMES,
    canonical_rank,
    derive_serviceable_timeframes,
    finest_timeframe,
    normalize_candles,
    normalize_timeframe,
)
from backtest.db.config import get_db_url
from backtest.logging_config import get_logger

log = get_logger(__name__)


def _can_serve(stored_tf: str | None, requested: str | None) -> bool:
    """Can ``stored_tf`` bars be resampled into ``requested``?

    Yes when they are the same, or when the request is COARSER — canonical
    rank increases as the timeframe gets coarser, so a request is buildable
    when its rank is at least the stored one's. Unknown names answer True: this
    guard exists to stop a known-impossible request, and rejecting a spelling
    it cannot rank would turn a working run into an error.
    """
    stored_rank = canonical_rank(stored_tf)
    requested_rank = canonical_rank(requested)
    if stored_rank is None or requested_rank is None:
        return True
    return requested_rank >= stored_rank


# Pandas resample rule mapping: canonical timeframe names -> pandas offsets
# (ticket P4.3: ONE vocabulary end to end).
_INTERVAL_TO_RULE = {
    "1min": "1min",
    "5min": "5min",
    "10min": "10min",
    "15min": "15min",
    "30min": "30min",
    "1hour": "1h",
    "4hour": "4h",
    "1day": "1D",
    "1week": "1W",
}

#: Finest-grained first — used to pick the stored timeframe to read from.
#: DERIVED from the canonical vocabulary, never hand-written: the previous
#: literal omitted ``10min``/``30min``/``4hour``, so a symbol stored ONLY at
#: 30min fell through the whole loop, fell back to ``return requested``, and
#: produced a spurious "not found in database" for a timeframe it could have
#: resampled perfectly well.
_SOURCE_TF_PRIORITY = list(CANONICAL_TIMEFRAMES)


def window_bounds(start: str, end: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Resolve a caller's ``start``/``end`` into a **half-open** ``[lo, hi)``.

    ``end`` means "up to and including this date", which is what every caller
    in this repo means by it: the UI's picker is an ``<input type="date">``, the
    API documents ``to_date``, and the Data tab's own coverage query uses
    ``ts::date <= :to_date``. Comparing a timestamp column directly against a
    bare date (``ts BETWEEN :start AND :end``) resolves to midnight and
    therefore drops the ENTIRE final session — one day of a one-month run, and
    every bar of a single-day run. Both are silent: the query succeeds and
    returns a shorter frame.

    So: a date-only ``end`` is widened to the next midnight and compared with
    ``<``. An ``end`` that carries a clock time is treated as an instant and
    kept inclusive (``<=``) — no caller does that today, but a mid-session
    cut-off is a sensible thing to ask for and should not be silently widened
    by a day.

    ``start`` needs no adjustment: ``ts >= '2026-09-02'`` already includes the
    whole first session on both backends.
    """
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end)
    if hi == hi.normalize():  # date-only → include the whole day
        hi = hi + pd.Timedelta(days=1)
        return lo, hi
    return lo, hi + pd.Timedelta(microseconds=1)  # instant → inclusive


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

        # ...and refuse to serve a granularity that cannot be BUILT from it.
        # Resampling runs one way: fine bars aggregate into coarse ones, never
        # the reverse. Without this check a daily-only symbol answered a 1min
        # request by handing back its daily bars labelled as minutes — three
        # daily candles presented as three minutes of trading, which is a
        # silently wrong backtest rather than a visible failure. list_symbols()
        # already applied this rule, so the same symbol could be missing from
        # /api/symbols while being happily "run" here.
        if not _can_serve(source_tf, interval):
            raise ValueError(
                self._describe_stored(
                    symbol,
                    requested=interval,
                    start=start,
                    end=end,
                    engine=engine,
                    cannot_build_from=source_tf,
                )
            )

        # ``ts BETWEEN :start AND :end`` compared timestamps against midnight
        # and silently dropped the whole final day — see window_bounds().
        lo, hi_exclusive = window_bounds(start, end)

        query = text(
            """
            SELECT ts, open, high, low, close, volume
            FROM market_data_cache
            WHERE symbol = :symbol
              AND timeframe = :timeframe
              AND ts >= :start
              AND ts < :end
            ORDER BY ts ASC
        """
        )

        df = pd.read_sql(
            query,
            engine,
            params={
                "symbol": symbol,
                "timeframe": source_tf,
                "start": lo.to_pydatetime(),
                "end": hi_exclusive.to_pydatetime(),
            },
        )

        log.debug("[db] %s tf=%s %s..%s → %d rows", symbol, source_tf, start, end, len(df))
        if df.empty:
            log.warning(
                "[db] no bars for %s (timeframe=%s, %s..%s) — check the symbol exists "
                "in market_data_cache: SELECT DISTINCT timeframe FROM market_data_cache "
                "WHERE symbol='%s'",
                symbol,
                source_tf,
                start,
                end,
                symbol,
            )
            raise ValueError(
                self._describe_stored(
                    symbol, requested=interval, start=start, end=end, engine=engine
                )
            )

        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")

        # Resample if the requested interval differs from what we stored
        if source_tf != interval:
            if interval in _INTERVAL_TO_RULE:
                log.debug("[db] resampling %s: %s → %s", symbol, source_tf, interval)
                df = self._resample(df, interval)
            else:
                log.warning(
                    "[db] interval %r has no resample rule (known: %s) — returning "
                    "stored %s bars unsampled",
                    interval,
                    sorted(_INTERVAL_TO_RULE),
                    source_tf,
                )

        out = normalize_candles(df)
        log.info(
            "[db] %s %s..%s → %d bars @ %s (stored as %s)",
            symbol,
            start,
            end,
            len(out),
            interval,
            source_tf,
        )
        return out

    def _describe_stored(
        self,
        symbol: str,
        requested: str,
        start: str | None = None,
        end: str | None = None,
        engine=None,
        cannot_build_from: str | None = None,
    ) -> str:
        """Why the request failed, and what this symbol DOES hold.

        The window the user asked for and the window the data covers are two
        different facts, and only the second one tells them what to change. A
        bare "no bars" sends them to check whether the symbol exists; "you
        asked for October, this symbol holds 2–14 September" answers it. The
        message this replaced printed the QUERY's bounds back at the user,
        which they already knew because they had just typed them.

        Reports the REQUESTED timeframe, never the stored one it resolved to:
        asking for 1day and being told "no 1min data" describes our resampling
        ladder, not their request. The stored granularities still appear in the
        availability list, where they explain what could be served.

        ``cannot_build_from`` is set when the failure is not about dates at all
        — the symbol holds data, but at a granularity too coarse to make the
        request out of. A date list alone would mislead there ("it says it holds
        1day up to September — so why did it fail?"), so the message also names
        what the symbol CAN serve.

        Deliberately never raises: this runs on the failure path, and a
        reporting helper that throws would replace a useful error with a
        useless one. Any query problem degrades to a generic sentence.
        """
        window = f" between {start} and {end}" if start and end else ""
        try:
            if engine is None:
                engine = self._get_engine()
            rows = pd.read_sql(
                text(
                    """
                    SELECT timeframe, MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                           COUNT(*) AS bars
                    FROM market_data_cache
                    WHERE symbol = :symbol
                    GROUP BY timeframe
                    """
                ),
                engine,
                params={"symbol": symbol},
            )
        except Exception as exc:  # noqa: BLE001 — reporting must not mask the error
            log.debug("[db] could not describe stored data for %s: %s", symbol, exc)
            return f"Symbol '{symbol}' has no {requested} data{window}."

        if rows.empty:
            return (
                f"Symbol '{symbol}' has no {requested} data{window}, and no data "
                f"of any timeframe is stored for it — fetch it from the Data tab first."
            )

        records = rows.to_dict("records")
        parts = []
        # CANONICAL_TIMEFRAMES is finest-first, so a symbol stored at several
        # granularities reads "1min: …; 1day: …" rather than sorting 1day
        # before 1min and burying the fine data the user probably wants.
        for r in sorted(
            records,
            key=lambda r: CANONICAL_TIMEFRAMES.index(r["timeframe"])
            if r["timeframe"] in CANONICAL_TIMEFRAMES
            else len(CANONICAL_TIMEFRAMES),
        ):
            first = pd.to_datetime(r["first_ts"])
            last = pd.to_datetime(r["last_ts"])
            part = f"{r['timeframe']} {first:%d %b %Y} to {last:%d %b %Y} ({int(r['bars']):,} bars)"
            if r["timeframe"] == requested:
                # Distinguishes "the timeframe is missing entirely" from "it is
                # there but the dates are wrong" — different things to do next.
                part += " [the timeframe you asked for]"
            parts.append(part)

        if cannot_build_from:
            # A granularity problem, not a date problem. Say which requests the
            # stored bars CAN answer, or the dates above read as "the data is
            # right there, why did it fail?".
            #
            # Computed with the same bars-cap the picker uses (bars held at the
            # finest stored granularity), so this list agrees with what the
            # dropdown advertises: promising "1week" for three days of daily
            # bars because the RULE allows it would send the user straight into
            # a one-bar run.
            stored = [r["timeframe"] for r in records]
            finest = finest_timeframe(stored)
            finest_bars = next(
                (int(r["bars"]) for r in records if r["timeframe"] == finest), None
            )
            serviceable = derive_serviceable_timeframes(stored, finest_bars=finest_bars)
            can = ", ".join(serviceable) if serviceable else "nothing"
            return (
                f"Symbol '{symbol}' has no {requested} data{window}: its bars are stored at "
                f"{cannot_build_from}, and {cannot_build_from} bars cannot be split into "
                f"{requested} ones. Available — {'; '.join(parts)}. "
                f"Timeframes it can serve: {can}."
            )
        return f"Symbol '{symbol}' has no {requested} data{window}. Available — {'; '.join(parts)}."

    def _find_best_source_tf(self, engine, symbol: str, requested: str) -> str:
        """Find the finest-grained timeframe available for this symbol.

        Priority: 1min > 5min > 15min > 1hour > 1day > 1week
        We want the finest so we can resample UP to any coarser interval.
        """
        # If requesting daily-or-coarser and we have it stored, use it
        # directly (fast, no resample needed)
        if requested in ("1day", "1week"):
            check = text(
                """
                SELECT 1 FROM market_data_cache
                WHERE symbol = :sym AND timeframe = :tf LIMIT 1
            """
            )
            with engine.connect() as conn:
                if conn.execute(check, {"sym": symbol, "tf": requested}).fetchone():
                    return requested

        # For intraday: find finest available
        for tf in _SOURCE_TF_PRIORITY:
            check = text(
                """
                SELECT 1 FROM market_data_cache
                WHERE symbol = :sym AND timeframe = :tf LIMIT 1
            """
            )
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

        resampled = (
            df.resample(rule)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["close"])
        )

        return resampled

    def list_symbols(self, timeframe: Optional[str] = "1day") -> list[str]:
        """
        Returns sorted list of distinct symbols available in DB. Pass a
        timeframe to restrict to it, or ``None`` for every timeframe present.

        A timeframe filter means "which symbols can SERVE this timeframe", not
        "which symbols have rows literally stamped with it". The old literal
        filter reported **zero** symbols for a cache holding 48,750 one-minute
        bars of RELIANCE, because there was no row with ``timeframe='1day'`` —
        the app then logged "0 symbols available" for a symbol it could back-
        test at all nine granularities. Resampling is the whole point of this
        source; the listing has to agree with it.
        """
        engine = self._get_engine()
        wanted = normalize_timeframe(timeframe) if timeframe is not None else None
        if timeframe is not None and wanted is None:
            log.warning(
                "[db] list_symbols: %r is not a known timeframe (known: %s)",
                timeframe,
                ", ".join(CANONICAL_TIMEFRAMES),
            )
            return []

        if timeframe is None:
            query = text(
                """
                SELECT DISTINCT symbol FROM market_data_cache
                ORDER BY symbol ASC
            """
            )
            with engine.connect() as conn:
                rows = [str(row[0]) for row in conn.execute(query).fetchall()]
            log.info("[db] list_symbols(timeframe=None) → %d symbols", len(rows))
            return rows

        # One grouped scan, then derive per symbol — the same rule the coverage
        # endpoint uses, so the picker's list and its timeframe dropdown can
        # never disagree about what a symbol supports.
        query = text(
            """
            SELECT symbol, timeframe, COUNT(*) AS bars
            FROM market_data_cache
            GROUP BY symbol, timeframe
        """
        )
        counts: dict[str, dict[str, int]] = {}
        with engine.connect() as conn:
            for row in conn.execute(query).mappings():
                symbol = str(row["symbol"]).strip().upper()
                if not symbol:
                    continue
                tf = str(row["timeframe"] or "").strip()
                counts.setdefault(symbol, {})[tf] = int(row["bars"] or 0)

        rows = []
        for symbol, by_tf in counts.items():
            finest = finest_timeframe(by_tf)
            serviceable = derive_serviceable_timeframes(
                by_tf,
                finest_bars=by_tf.get(finest) if finest else None,
            )
            if wanted in serviceable:
                rows.append(symbol)
        rows.sort()
        log.info("[db] list_symbols(timeframe=%s) → %d symbols", timeframe, len(rows))
        if not rows:
            log.warning(
                "[db] no symbol in market_data_cache can serve timeframe=%r — check "
                "what was ingested (SELECT symbol, timeframe, COUNT(*) FROM "
                "market_data_cache GROUP BY symbol, timeframe)",
                timeframe,
            )
        return rows

    def last_ingested_at(self, symbol: str, timeframe: Optional[str] = None) -> Optional[str]:
        """
        When this symbol's bars were last written to ``market_data_cache``
        (``YYYY-MM-DD``), or ``None`` when nothing is cached for it.

        Backs the data-provenance stamp on a result (PRD backTest-enhance
        §1.2): "real data" only means something next to "real data as of
        when". ``timeframe`` is optional — a caller that resampled from a
        finer stored bar passes the requested interval and gets the newest
        write across that symbol's granularities.
        """
        query = text(
            """
            SELECT MAX(ingested_at) FROM market_data_cache
            WHERE symbol = :symbol
              AND (:timeframe IS NULL OR timeframe = :timeframe)
            """
        )
        try:
            engine = self._get_engine()
            with engine.connect() as conn:
                row = conn.execute(query, {"symbol": symbol, "timeframe": timeframe}).fetchone()
        except Exception as exc:  # noqa: BLE001 — provenance is best-effort
            log.debug("[db] last_ingested_at(%s) failed: %s", symbol, exc)
            return None
        if not row or row[0] is None:
            return None
        log.debug("[db] last_ingested_at(%s, tf=%s) → %s", symbol, timeframe, row[0])
        return str(row[0])[:10]
