"""Phase 5.0 — 1day equity-bar ingest, 1min->1day rollup & day-by-day verifier.

Populates ``market_data_cache`` (``timeframe = '1day'``) for NSE swing/pool
equities without touching WebSocket streams, live order paths, or strategy
contracts, under three hard production invariants:

1. **TimescaleDB hypertable chunk safety (zero cross-chunk UPDATEs)**:
   ``market_data_cache`` is a TimescaleDB hypertable partitioned on ``ts`` where
   ``ON CONFLICT ... DO UPDATE`` across compressed or boundary chunks fails.
   All writes default strictly to idempotent ``ON CONFLICT (symbol, exchange,
   timeframe, ts) DO NOTHING`` with pre-validated positive OHLCV invariants.
   Re-writing recent uncompressed sessions requires an explicit bounded
   ``replace_window`` (implemented as transactional ``DELETE + INSERT``, never
   ``UPDATE``).

2. **Pilot-first rate & DB pool protection (502-storm guard)**:
   Broker historical fetches enforce a hard pilot ceiling
   (:data:`PILOT_MAX_SYMBOLS` = 10, :data:`PILOT_MAX_CALENDAR_DAYS` = 120)
   unless ``allow_bulk=True`` is explicitly passed. Window fetches apply
   coverage-aware day skipping, adaptive pacing, jittered exponential backoff,
   a consecutive-5xx circuit breaker, and JSON checkpoint persistence so a
   mStock 502 storm halts cleanly without starving the app's DB pool.

3. **Day-by-day verification (never "job done" on silent-None or 502 holes)**:
   Neither empty/``None`` broker responses over expected NSE trading days nor
   thin/partial ``1min`` sessions (< :data:`DEFAULT_MIN_INTRADAY_BARS`) are
   counted as complete. :func:`verify_daily_coverage` audits stored ``1day``
   bars day-by-day against the NSE trading calendar and fails loudly whenever
   any expected trading session is missing.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


import pandas as pd
import requests
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from backtest.data.mstock_live_feed import _resolve_scriptmaster_row
from backtest.timekeeping import EXCHANGE_TZ
from backtest.live.market_status import NSE_HOLIDAYS_2026

logger = logging.getLogger("backtest.data.daily_bar_ingest")

# IST exists in exactly one place (backtest.timekeeping); the aliases below are
# wall-clock handles for display/calendar day, never a second definition.
IST_ZONE = EXCHANGE_TZ

#: Canonical pilot ceilings — multi-year / 200-500 symbol bulk backfills require
#: explicit ``allow_bulk=True`` so demo DB pools and rate budgets stay protected.
PILOT_MAX_SYMBOLS: int = 10
PILOT_MAX_CALENDAR_DAYS: int = 120
DEFAULT_PILOT_SYMBOLS: tuple[str, ...] = (
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
)

#: NSE regular equity session bounds (09:15 <= HH:MM < 15:30 IST -> 375 1min bars).
SESSION_OPEN_HHMM: str = "09:15"
SESSION_CLOSE_HHMM: str = "15:30"
POST_CLOSE_SEAL_HHMM: str = "15:35"
FULL_SESSION_1MIN_BARS: int = 375
#: Default 90% intraday completeness quorum (338/375 minutes), matching
#: ``data_manager.COVERAGE_FRACTION = 0.9`` so half-broken days are never rolled
#: up into deceptive 1day bars.
DEFAULT_MIN_INTRADAY_BARS: int = math.ceil(FULL_SESSION_1MIN_BARS * 0.90)

#: mStock historical endpoint chunk width for ``day`` candles (< 1000-bar cap;
#: 180 calendar days keeps each HTTP payload small and isolates 502 retries).
DEFAULT_DAILY_CHUNK_DAYS: int = 180


class BulkIngestGatedError(ValueError):
    """Raised when a caller requests a bulk historical fetch without ``allow_bulk``."""


# ---------------------------------------------------------------------------
# Canonical 1day session clock helpers
# ---------------------------------------------------------------------------


def parse_iso_date(value: date | datetime | str) -> date:
    """Parse ``YYYY-MM-DD`` (or date/datetime) into a :class:`datetime.date`."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_val = str(value).strip()
    return date.fromisoformat(text_val[:10])


def daily_session_open_utc(session_date: date | str) -> datetime:
    """Canonical ``market_data_cache.ts`` for an NSE ``1day`` bar.

    Anchored to NSE cash session open (``09:15:00+05:30`` == ``03:45:00+00:00``):
    - Matches ``MarketDataCache.ts`` ("Bar OPEN time, aligned to boundary");
    - Falls on calendar date ``d`` in BOTH UTC (``03:45Z``) and IST (``09:15``),
      preventing the ``-1 day`` midnight shift that ``00:00+05:30`` (``18:30Z``
      prior day) causes under UTC ``date_trunc('day', ts)`` and SQLite ``date(ts)``;
    - Produces an identical unique key ``(symbol, exchange, '1day', ts)`` across
      both ``1min -> 1day`` rollups and mStock ``/historical/.../day`` fetches.
    """
    d = parse_iso_date(session_date)
    return datetime(d.year, d.month, d.day, 3, 45, 0, tzinfo=timezone.utc)


def resolve_daily_session_date(raw_ts: Any) -> date:
    """Extract the IST trading calendar ``date`` from any broker or DB timestamp.

    Handles bare dates (``"2026-10-23"``), midnight IST strings from mStock's
    daily endpoint (``"2026-10-23T00:00:00+05:30"`` -> ``2026-10-23``, never
    shifted to ``2026-10-22``), and UTC session-open instants
    (``"2026-10-23T03:45:00Z"`` -> ``2026-10-23``).
    """
    if isinstance(raw_ts, datetime):
        if raw_ts.tzinfo is not None:
            return raw_ts.astimezone(IST_ZONE).date()
        return raw_ts.date()
    if isinstance(raw_ts, date):
        return raw_ts
    text_val = str(raw_ts or "").strip()
    if not text_val:
        raise ValueError("empty timestamp")
    if len(text_val) == 10 and text_val[4] == "-" and text_val[7] == "-":
        return date.fromisoformat(text_val)
    ts = pd.Timestamp(text_val)
    if ts.tzinfo is not None:
        return ts.tz_convert(IST_ZONE).date()
    return ts.date()


# ---------------------------------------------------------------------------
# NSE trading calendar & day-by-day expectation helpers
# ---------------------------------------------------------------------------


def load_nse_holidays(
    engine: Engine | None,
    from_date: date,
    to_date: date,
) -> set[date]:
    """Return weekday NSE trading holidays in ``[from_date, to_date]``.

    Combines ``market_holidays`` DB rows (when the table exists and is reachable)
    with the built-in 2026 NSE equity seed so dev/SQLite profiles still exclude
    known closures such as Gandhi Jayanti (2026-10-02) and Dussehra (2026-10-20).
    """
    holidays: set[date] = set()
    for h_str, seg, _desc in NSE_HOLIDAYS_2026:
        if seg in ("equity", "NSE", "CM"):
            hd = date.fromisoformat(h_str)
            if from_date <= hd <= to_date and hd.weekday() < 5:
                holidays.add(hd)

    if engine is None:
        return holidays

    sql = text(
        "SELECT holiday_date FROM market_holidays "
        "WHERE holiday_date >= :from_date AND holiday_date <= :to_date "
        "AND is_trading_holiday IS NOT FALSE"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sql,
                {"from_date": from_date.isoformat(), "to_date": to_date.isoformat()},
            ).all()
        for (raw_d,) in rows:
            hd = parse_iso_date(raw_d)
            if hd.weekday() < 5:
                holidays.add(hd)
    except Exception as exc:  # noqa: BLE001 — missing table on dev SQLite is harmless
        logger.debug("market_holidays query skipped (%s)", exc)
    return holidays


def expected_nse_trading_days(
    from_date: date | str,
    to_date: date | str,
    *,
    engine: Engine | None = None,
    extra_holidays: Sequence[date | str] = (),
    now_utc: datetime | None = None,
    include_unclosed_today: bool = False,
) -> list[date]:
    """Ordered list of expected sealed NSE cash sessions in ``[from_date, to_date]``.

    Excludes Saturdays/Sundays, NSE holidays, future dates, and (unless
    ``include_unclosed_today=True``) today's session prior to ``15:35 IST``.
    """
    start_d = parse_iso_date(from_date)
    end_d = parse_iso_date(to_date)
    if end_d < start_d:
        return []

    ref_utc = now_utc or datetime.now(timezone.utc)
    if ref_utc.tzinfo is None:
        ref_utc = ref_utc.replace(tzinfo=timezone.utc)
    now_ist = ref_utc.astimezone(IST_ZONE)
    today_ist = now_ist.date()

    effective_end = min(end_d, today_ist)
    if (
        not include_unclosed_today
        and effective_end == today_ist
        and now_ist.strftime("%H:%M") < POST_CLOSE_SEAL_HHMM
    ):
        effective_end = today_ist - timedelta(days=1)
    if effective_end < start_d:
        return []

    holidays = load_nse_holidays(engine, start_d, effective_end)
    for extra in extra_holidays:
        holidays.add(parse_iso_date(extra))

    days: list[date] = []
    cur = start_d
    while cur <= effective_end:
        if cur.weekday() < 5 and cur not in holidays:
            days.append(cur)
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PersistSummary:
    symbol: str
    candidates: int = 0
    inserted: int = 0
    skipped_conflict: int = 0
    rejected_invalid_ohlc: int = 0
    deleted_for_replace: int = 0


@dataclass
class SymbolRollupReport:
    symbol: str
    intraday_rows_scanned: int = 0
    complete_days_found: int = 0
    inserted: int = 0
    skipped_conflict: int = 0
    partial_intraday_days: dict[str, int] = field(default_factory=dict)
    rolled_dates: list[str] = field(default_factory=list)


@dataclass
class RollupRunReport:
    from_date: str
    to_date: str
    dry_run: bool
    min_intraday_bars: int
    total_inserted: int = 0
    total_skipped_conflict: int = 0
    symbols: list[SymbolRollupReport] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SymbolFetchReport:
    symbol: str
    security_token: str = ""
    segment: str = "NSE"
    windows_total: int = 0
    windows_skipped_covered: int = 0
    windows_fetched_ok: int = 0
    inserted: int = 0
    skipped_conflict: int = 0
    rejected_invalid_ohlc: int = 0
    failed_windows: list[dict[str, str]] = field(default_factory=list)
    silent_empty_windows: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def clean(self) -> bool:
        return (
            not self.error
            and not self.failed_windows
            and not self.silent_empty_windows
        )


@dataclass
class DailyFetchRunReport:
    from_date: str
    to_date: str
    dry_run: bool
    pilot_mode: bool
    circuit_breaker_tripped: bool = False
    http_requests: int = 0
    total_inserted: int = 0
    total_skipped_conflict: int = 0
    checkpoint_path: str | None = None
    symbols: list[SymbolFetchReport] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def all_windows_clean(self) -> bool:
        return (
            not self.circuit_breaker_tripped
            and bool(self.symbols)
            and all(s.clean for s in self.symbols)
        )

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["all_windows_clean"] = self.all_windows_clean
        return out


@dataclass
class SymbolCoverageAudit:
    symbol: str
    expected_sessions: int
    present_sessions: int
    coverage_pct: float
    missing_dates: list[str] = field(default_factory=list)
    duplicate_dates: list[str] = field(default_factory=list)
    invalid_ohlc_dates: list[str] = field(default_factory=list)
    earliest_date: str | None = None
    latest_date: str | None = None
    passed: bool = False


@dataclass
class DailyCoverageAuditReport:
    from_date: str
    to_date: str
    expected_trading_dates: list[str]
    peer_observed_dates: list[str] = field(default_factory=list)
    symbols: list[SymbolCoverageAudit] = field(default_factory=list)
    passed: bool = False

    @property
    def symbols_complete(self) -> int:
        return sum(1 for s in self.symbols if s.passed)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["symbols_complete"] = self.symbols_complete
        out["symbols_total"] = len(self.symbols)
        return out


# ---------------------------------------------------------------------------
# TimescaleDB-safe insert-only writer (ZERO UPDATE statements)
# ---------------------------------------------------------------------------

_INSERT_DO_NOTHING_SQL = text(
    """
    INSERT INTO market_data_cache
        (symbol, exchange, timeframe, ts, open, high, low, close, volume, source, ingested_at)
    VALUES
        (:symbol, :exchange, :timeframe, :ts, :open, :high, :low, :close, :volume,
         :source, :ingested_at)
    ON CONFLICT (symbol, exchange, timeframe, ts) DO NOTHING
    """
)

_DELETE_WINDOW_SQL = text(
    """
    DELETE FROM market_data_cache
    WHERE symbol = :symbol
      AND exchange = :exchange
      AND timeframe = '1day'
      AND ts >= :lo_ts
      AND ts <= :hi_ts
    """
)


def persist_daily_bars(
    engine: Engine,
    symbol: str,
    bars: Sequence[Mapping[str, Any]],
    *,
    exchange: str = "NSE",
    source: str = "mstock",
    dry_run: bool = False,
    replace_window: tuple[date, date] | None = None,
    batch_size: int = 250,
) -> PersistSummary:
    """Persist ``1day`` OHLCV rows into ``market_data_cache`` without ``UPDATE``.

    Deduplicates candidate rows per IST session date in memory (keeping the
    highest-volume bar if a feed returned duplicate dates), validates positive
    OHLC invariants, and executes ``ON CONFLICT DO NOTHING`` in bounded batches.
    """
    norm_sym = str(symbol).strip().upper()
    norm_exch = str(exchange or "NSE").strip().upper()
    summary = PersistSummary(symbol=norm_sym, candidates=len(bars))
    if not bars:
        return summary

    ingested_now = datetime.now(timezone.utc)
    by_session: dict[date, dict[str, Any]] = {}

    for raw in bars:
        raw_ts = (
            raw.get("session_date")
            or raw.get("ts")
            or raw.get("t")
            or raw.get("time")
            or raw.get("timestamp")
        )
        if raw_ts is None:
            summary.rejected_invalid_ohlc += 1
            continue
        try:
            sess_d = resolve_daily_session_date(raw_ts)
            o = float(raw.get("open", raw.get("o", 0.0)))
            h = float(raw.get("high", raw.get("h", 0.0)))
            low_p = float(raw.get("low", raw.get("l", 0.0)))
            c = float(raw.get("close", raw.get("c", 0.0)))
            v = float(raw.get("volume", raw.get("v", 0.0)) or 0.0)
        except (TypeError, ValueError):
            summary.rejected_invalid_ohlc += 1
            continue

        finite_ohlc = (
            math.isfinite(o)
            and math.isfinite(h)
            and math.isfinite(low_p)
            and math.isfinite(c)
        )
        if (
            not finite_ohlc
            or o <= 0.0
            or h <= 0.0
            or low_p <= 0.0
            or c <= 0.0
            or v < 0.0
            or h < low_p
            or h < o
            or h < c
            or low_p > o
            or low_p > c
        ):
            summary.rejected_invalid_ohlc += 1
            continue

        row_dict = {
            "symbol": norm_sym,
            "exchange": norm_exch,
            "timeframe": "1day",
            "ts": daily_session_open_utc(sess_d),
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(low_p, 4),
            "close": round(c, 4),
            "volume": round(v, 2),
            "source": str(raw.get("source") or source),
            "ingested_at": ingested_now,
        }
        existing = by_session.get(sess_d)
        if existing is None or row_dict["volume"] >= existing["volume"]:
            by_session[sess_d] = row_dict

    ordered_rows = [by_session[d] for d in sorted(by_session)]
    if not ordered_rows:
        return summary

    if dry_run:
        summary.inserted = len(ordered_rows)
        return summary

    with engine.begin() as conn:
        if replace_window is not None:
            w_lo, w_hi = replace_window
            del_res = conn.execute(
                _DELETE_WINDOW_SQL,
                {
                    "symbol": norm_sym,
                    "exchange": norm_exch,
                    "lo_ts": daily_session_open_utc(w_lo),
                    "hi_ts": daily_session_open_utc(w_hi),
                },
            )
            summary.deleted_for_replace = max(0, int(del_res.rowcount or 0))

        inserted_total = 0
        for i in range(0, len(ordered_rows), batch_size):
            chunk = ordered_rows[i : i + batch_size]
            res = conn.execute(_INSERT_DO_NOTHING_SQL, chunk)
            if res.rowcount is not None and res.rowcount >= 0:
                inserted_total += int(res.rowcount)
            else:
                inserted_total += len(chunk)

    summary.inserted = inserted_total
    summary.skipped_conflict = max(0, len(ordered_rows) - inserted_total)
    return summary


# ---------------------------------------------------------------------------
# Path A — Local Postgres 1min -> 1day Rollup (zero broker HTTP traffic)
# ---------------------------------------------------------------------------


def rollup_1min_to_1day(
    engine: Engine,
    symbols: Sequence[str],
    from_date: date | str,
    to_date: date | str,
    *,
    min_intraday_bars: int = DEFAULT_MIN_INTRADAY_BARS,
    dry_run: bool = False,
    replace_recent_window: tuple[date, date] | None = None,
    now_utc: datetime | None = None,
    include_unclosed_today: bool = False,
    special_session_dates: Sequence[date | str] = (),
) -> RollupRunReport:
    """Aggregate complete ``1min`` NSE cash sessions in ``market_data_cache`` into ``1day``.

    * Filters strictly to ``09:15 <= IST HH:MM < 15:30`` so off-hours stray ticks
      cannot pollute session open/high/low/close.
    * Enforces ``min_intraday_bars`` (default ``338`` of ``375`` minutes, or
      ``>= 40`` on explicit ``special_session_dates`` such as Diwali Muhurat) so
      truncated/gappy ``1min`` days are skipped and surfaced in
      ``partial_intraday_days`` rather than written as corrupt ``1day`` bars.
    * Uses ``persist_daily_bars`` (``ON CONFLICT DO NOTHING`` — zero ``UPDATE``).
    """
    t0 = time.monotonic()
    start_d = parse_iso_date(from_date)
    end_d = parse_iso_date(to_date)
    ref_utc = now_utc or datetime.now(timezone.utc)
    if ref_utc.tzinfo is None:
        ref_utc = ref_utc.replace(tzinfo=timezone.utc)
    now_ist = ref_utc.astimezone(IST_ZONE)
    today_ist = now_ist.date()

    effective_end = min(end_d, today_ist)
    if (
        not include_unclosed_today
        and effective_end == today_ist
        and now_ist.strftime("%H:%M") < POST_CLOSE_SEAL_HHMM
    ):
        effective_end = today_ist - timedelta(days=1)

    cleaned_syms = [
        str(s).strip().upper() for s in dict.fromkeys(symbols) if str(s).strip()
    ]
    report = RollupRunReport(
        from_date=start_d.isoformat(),
        to_date=end_d.isoformat(),
        dry_run=dry_run,
        min_intraday_bars=int(min_intraday_bars),
    )
    if not cleaned_syms or effective_end < start_d:
        report.elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        return report

    special_set = {parse_iso_date(d) for d in special_session_dates}
    # Start 00:00 IST on start_d == (start_d - 1d) 18:30 UTC; end 23:59:59 IST on effective_end.
    lo_utc = datetime(start_d.year, start_d.month, start_d.day, 0, 0, tzinfo=IST_ZONE).astimezone(
        timezone.utc
    )
    hi_utc = (
        datetime(
            effective_end.year,
            effective_end.month,
            effective_end.day,
            0,
            0,
            tzinfo=IST_ZONE,
        )
        + timedelta(days=1)
    ).astimezone(timezone.utc)

    is_sqlite = getattr(getattr(engine, "dialect", None), "name", "") == "sqlite"
    lo_bind: Any = lo_utc.isoformat() if is_sqlite else lo_utc
    hi_bind: Any = hi_utc.isoformat() if is_sqlite else hi_utc

    select_1min_sql = text(
        """
        SELECT ts, exchange, open, high, low, close, volume
        FROM market_data_cache
        WHERE symbol = :symbol
          AND timeframe = '1min'
          AND ts >= :lo_ts
          AND ts < :hi_ts
        ORDER BY ts ASC
        """
    )

    for sym in cleaned_syms:
        sym_rep = SymbolRollupReport(symbol=sym)
        with engine.connect() as conn:
            df = pd.read_sql(
                select_1min_sql,
                conn,
                params={"symbol": sym, "lo_ts": lo_bind, "hi_ts": hi_bind},
            )
        if df.empty:
            report.symbols.append(sym_rep)
            continue

        sym_rep.intraday_rows_scanned = int(len(df))
        ts_parsed = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST_ZONE)
        df = df.assign(ist_ts=ts_parsed)
        hhmm = df["ist_ts"].dt.strftime("%H:%M")
        df = df[(hhmm >= SESSION_OPEN_HHMM) & (hhmm < SESSION_CLOSE_HHMM)].copy()
        if df.empty:
            report.symbols.append(sym_rep)
            continue

        # Prefer NSE over BSE when duplicate minute rows exist, then higher volume.
        df["exch_rank"] = (df["exchange"].astype(str).str.upper() == "NSE").astype(int)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0.0)
        df = (
            df.sort_values(["ist_ts", "exch_rank", "volume"])
            .drop_duplicates(subset=["ist_ts"], keep="last")
            .sort_values("ist_ts")
        )
        df["sess_date"] = df["ist_ts"].dt.date

        candidate_bars: list[dict[str, Any]] = []
        for sess_d, grp in df.groupby("sess_date", sort=True):
            if sess_d < start_d or sess_d > effective_end:
                continue
            n_bars = int(len(grp))
            req_bars = 40 if sess_d in special_set else int(min_intraday_bars)
            if n_bars < req_bars:
                sym_rep.partial_intraday_days[sess_d.isoformat()] = n_bars
                continue
            sym_rep.complete_days_found += 1
            sym_rep.rolled_dates.append(sess_d.isoformat())
            candidate_bars.append(
                {
                    "session_date": sess_d,
                    "open": float(grp["open"].iloc[0]),
                    "high": float(grp["high"].max()),
                    "low": float(grp["low"].min()),
                    "close": float(grp["close"].iloc[-1]),
                    "volume": float(grp["volume"].sum()),
                    "source": "rollup_1min",
                }
            )

        if candidate_bars:
            persisted = persist_daily_bars(
                engine,
                sym,
                candidate_bars,
                exchange="NSE",
                source="rollup_1min",
                dry_run=dry_run,
                replace_window=replace_recent_window,
            )
            sym_rep.inserted = persisted.inserted
            sym_rep.skipped_conflict = persisted.skipped_conflict
            report.total_inserted += persisted.inserted
            report.total_skipped_conflict += persisted.skipped_conflict

        report.symbols.append(sym_rep)

    report.elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
    return report


# ---------------------------------------------------------------------------
# Day-by-day completeness verifier (Constraint 2 — never "job done" on holes)
# ---------------------------------------------------------------------------


def _query_stored_daily_rows(
    engine: Engine,
    symbols: Sequence[str],
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    lo_utc = datetime(
        from_date.year, from_date.month, from_date.day, 0, 0, tzinfo=IST_ZONE
    ).astimezone(timezone.utc)
    hi_utc = (
        datetime(to_date.year, to_date.month, to_date.day, 0, 0, tzinfo=IST_ZONE)
        + timedelta(days=1)
    ).astimezone(timezone.utc)
    is_sqlite = getattr(getattr(engine, "dialect", None), "name", "") == "sqlite"
    lo_bind: Any = lo_utc.isoformat() if is_sqlite else lo_utc
    hi_bind: Any = hi_utc.isoformat() if is_sqlite else hi_utc

    sql = text(
        """
        SELECT symbol, exchange, ts, open, high, low, close, volume
        FROM market_data_cache
        WHERE timeframe = '1day'
          AND ts >= :lo_ts
          AND ts < :hi_ts
          AND symbol IN :symbols
        ORDER BY symbol ASC, ts ASC
        """
    ).bindparams(bindparam("symbols", expanding=True))

    with engine.connect() as conn:
        return pd.read_sql(
            sql,
            conn,
            params={
                "lo_ts": lo_bind,
                "hi_ts": hi_bind,
                "symbols": list(symbols),
            },
        )


def verify_daily_coverage(
    engine: Engine,
    symbols: Sequence[str],
    from_date: date | str,
    to_date: date | str,
    *,
    extra_holidays: Sequence[date | str] = (),
    now_utc: datetime | None = None,
    include_unclosed_today: bool = False,
) -> DailyCoverageAuditReport:
    """Verify ``market_data_cache`` (``timeframe='1day'``) day-by-day per symbol.

    Fails (``passed=False``) if ANY requested symbol is missing ANY expected
    NSE trading session in ``[from_date, to_date]`` or holds invalid OHLC values.
    """
    start_d = parse_iso_date(from_date)
    end_d = parse_iso_date(to_date)
    cleaned_syms = [
        str(s).strip().upper() for s in dict.fromkeys(symbols) if str(s).strip()
    ]
    expected_days = expected_nse_trading_days(
        start_d,
        end_d,
        engine=engine,
        extra_holidays=extra_holidays,
        now_utc=now_utc,
        include_unclosed_today=include_unclosed_today,
    )
    expected_iso = [d.isoformat() for d in expected_days]
    expected_set = set(expected_iso)

    df = _query_stored_daily_rows(engine, cleaned_syms, start_d, end_d)
    by_sym: dict[str, list[tuple[str, str, float, float, float, float]]] = {
        s: [] for s in cleaned_syms
    }
    peer_dates: set[str] = set()

    if not df.empty:
        for row in df.itertuples(index=False):
            sym_key = str(row.symbol).strip().upper()
            sess_iso = resolve_daily_session_date(row.ts).isoformat()
            peer_dates.add(sess_iso)
            if sym_key in by_sym:
                by_sym[sym_key].append(
                    (
                        sess_iso,
                        str(row.exchange or "NSE").upper(),
                        float(row.open),
                        float(row.high),
                        float(row.low),
                        float(row.close),
                    )
                )

    audits: list[SymbolCoverageAudit] = []
    for sym in cleaned_syms:
        records = by_sym.get(sym, [])
        date_exchanges: dict[str, set[str]] = {}
        invalid_dates: set[str] = set()
        for d_iso, exch, o, h, low_p, c in records:
            date_exchanges.setdefault(d_iso, set()).add(exch)
            bad_ohlc = (
                o <= 0
                or h <= 0
                or low_p <= 0
                or c <= 0
                or h < low_p
                or h < o
                or h < c
                or low_p > o
                or low_p > c
            )
            if bad_ohlc:
                invalid_dates.add(d_iso)

        # Count duplicates within the same exchange or across exchanges on the same date
        date_counts: dict[str, int] = {}
        for d_iso, *_ in records:
            date_counts[d_iso] = date_counts.get(d_iso, 0) + 1

        present_expected = sorted(d for d in expected_set if d in date_counts)
        missing = sorted(d for d in expected_set if d not in date_counts)
        dupes = sorted(d for d, cnt in date_counts.items() if cnt > 1)
        all_dates = sorted(date_counts)
        exp_n = len(expected_iso)
        pres_n = len(present_expected)
        cov_pct = 100.0 if exp_n == 0 else round((pres_n / exp_n) * 100.0, 2)
        sym_passed = (len(missing) == 0) and (len(invalid_dates) == 0)

        audits.append(
            SymbolCoverageAudit(
                symbol=sym,
                expected_sessions=exp_n,
                present_sessions=pres_n,
                coverage_pct=cov_pct,
                missing_dates=missing,
                duplicate_dates=dupes,
                invalid_ohlc_dates=sorted(invalid_dates),
                earliest_date=all_dates[0] if all_dates else None,
                latest_date=all_dates[-1] if all_dates else None,
                passed=sym_passed,
            )
        )

    overall_passed = bool(audits) and all(a.passed for a in audits)
    return DailyCoverageAuditReport(
        from_date=start_d.isoformat(),
        to_date=end_d.isoformat(),
        expected_trading_dates=expected_iso,
        peer_observed_dates=sorted(peer_dates),
        symbols=audits,
        passed=overall_passed,
    )


# ---------------------------------------------------------------------------
# Path B — Pilot-gated mStock historical 1day ingest (502-resilient)
# ---------------------------------------------------------------------------


def _build_date_windows(
    start_d: date, end_d: date, chunk_days: int
) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cur = start_d
    step = max(1, int(chunk_days))
    while cur <= end_d:
        w_end = min(cur + timedelta(days=step - 1), end_d)
        windows.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    return windows


def _extract_mstock_bars(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").strip().lower()
        if status in ("error", "false", "failed", "failure"):
            msg = payload.get("message") or payload.get("error") or status
            raise RuntimeError(f"mStock error payload: {msg}")
        for key in ("data", "candles", "result", "bars", "historical"):
            if key in payload:
                val = payload[key]
                if isinstance(val, list):
                    return val
                if isinstance(val, dict):
                    for k2 in ("candles", "data", "result", "bars", "historical"):
                        if k2 in val and isinstance(val[k2], list):
                            return val[k2]
    return []


def _normalize_raw_bar(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        raw_ts = raw.get("t", raw.get("time", raw.get("timestamp", raw.get("ts"))))
        if raw_ts is None:
            return None
        return {
            "ts": raw_ts,
            "open": raw.get("o", raw.get("open")),
            "high": raw.get("h", raw.get("high")),
            "low": raw.get("l", raw.get("low")),
            "close": raw.get("c", raw.get("close")),
            "volume": raw.get("v", raw.get("volume", 0)),
        }
    if isinstance(raw, (list, tuple)) and len(raw) >= 6:
        return {
            "ts": raw[0],
            "open": raw[1],
            "high": raw[2],
            "low": raw[3],
            "close": raw[4],
            "volume": raw[5],
        }
    return None


def resolve_equity_token_and_segment(
    engine: Engine | None,
    symbol: str,
    *,
    base_url: str,
    api_key: str,
    access_token: str,
) -> tuple[str, str]:
    """Resolve ``(security_token, segment)`` via ``ScriptMasterCache`` or ``instruments``."""
    sym_up = str(symbol).strip().upper()
    if api_key and access_token:
        try:
            row = _resolve_scriptmaster_row(base_url, api_key, access_token, sym_up)
            tok = str(
                row.get("instrument_token")
                or row.get("token")
                or row.get("securitytoken")
                or ""
            ).strip()
            exch = str(row.get("exchange") or "NSE").strip().upper()
            if tok:
                return tok, ("BSE" if exch == "BSE" else "NSE")
        except Exception as exc:  # noqa: BLE001 — fall through to DB catalogue
            logger.debug("ScriptMasterCache lookup failed for %s: %s", sym_up, exc)

    if engine is not None:
        sql = text(
            "SELECT instrument_token, exchange "
            "FROM instruments "
            "WHERE UPPER(tradingsymbol) = :sym "
            "  AND ((exchange = 'NSE' AND instrument_type = 'EQ') "
            "       OR (exchange = 'BSE' AND instrument_type = 'Equity')) "
            "ORDER BY CASE WHEN exchange = 'NSE' THEN 0 ELSE 1 END "
            "LIMIT 1"
        )
        try:
            with engine.connect() as conn:
                rec = conn.execute(sql, {"sym": sym_up}).first()
            if rec and rec[0]:
                exch = str(rec[1] or "NSE").strip().upper()
                return str(rec[0]).strip(), ("BSE" if exch == "BSE" else "NSE")
        except Exception as exc:  # noqa: BLE001
            logger.debug("instruments table lookup failed for %s: %s", sym_up, exc)

    raise ValueError(f"Could not resolve mStock security token for {sym_up}")


def ingest_mstock_daily(
    engine: Engine,
    symbols: Sequence[str],
    from_date: date | str,
    to_date: date | str,
    *,
    api_key: str | None = None,
    access_token: str | None = None,
    base_url: str | None = None,
    allow_bulk: bool = False,
    dry_run: bool = False,
    chunk_days: int = DEFAULT_DAILY_CHUNK_DAYS,
    base_delay_s: float = 0.6,
    wide_delay_s: float = 2.5,
    max_retries_per_window: int = 3,
    max_consecutive_5xx: int = 4,
    checkpoint_path: str | Path | None = None,
    http_get: Callable[..., Any] | None = None,
    token_resolver: Callable[[str], tuple[str, str]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_utc: datetime | None = None,
) -> DailyFetchRunReport:
    """Fetch missing ``1day`` NSE equity bars from mStock with pilot & 502 guards.

    * **Pilot gate**: refuses ``> PILOT_MAX_SYMBOLS`` (10) or
      ``> PILOT_MAX_CALENDAR_DAYS`` (120) unless ``allow_bulk=True``.
    * **Coverage skip**: queries existing ``1day`` dates in ``market_data_cache``
      once up front and skips any chunk window with zero missing trading days.
    * **502 storm circuit breaker**: backs off on ``429``/``5xx``/timeout and
      trips cleanly after ``max_consecutive_5xx`` consecutive failed windows,
      persisting ``checkpoint_path`` so progress is never lost or misreported.
    * **Silent-None detection**: an HTTP 200 carrying ``None`` or ``[]`` across
      a window that expects ``>= 1`` NSE trading session is flagged in
      ``silent_empty_windows`` (never treated as clean!).
    """
    t0 = time.monotonic()
    start_d = parse_iso_date(from_date)
    end_d = parse_iso_date(to_date)
    if end_d < start_d:
        raise ValueError(f"to_date ({end_d}) precedes from_date ({start_d})")

    cleaned_syms = [
        str(s).strip().upper() for s in dict.fromkeys(symbols) if str(s).strip()
    ]
    if not cleaned_syms:
        raise ValueError("at least one symbol is required")

    span_days = (end_d - start_d).days + 1
    if not allow_bulk:
        if len(cleaned_syms) > PILOT_MAX_SYMBOLS:
            raise BulkIngestGatedError(
                f"Pilot gate refused {len(cleaned_syms)} symbols (max {PILOT_MAX_SYMBOLS} "
                "in pilot mode). Pass allow_bulk=True / --allow-bulk ONLY after Monday's "
                "live soak clears."
            )
        if span_days > PILOT_MAX_CALENDAR_DAYS:
            raise BulkIngestGatedError(
                f"Pilot gate refused {span_days}-day window ({start_d}..{end_d}, max "
                f"{PILOT_MAX_CALENDAR_DAYS} calendar days in pilot mode). Pass "
                "allow_bulk=True / --allow-bulk ONLY after Monday's live soak clears."
            )

    resolved_base = (
        base_url or os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade")
    ).rstrip("/")
    resolved_key = (
        api_key if api_key is not None else os.getenv("MSTOCK_API_KEY", "")
    ).strip()
    resolved_tok = (
        access_token
        if access_token is not None
        else os.getenv("MSTOCK_ACCESS_TOKEN", "")
    ).strip()
    getter = http_get or requests.get

    expected_all = set(
        expected_nse_trading_days(
            start_d, end_d, engine=engine, now_utc=now_utc
        )
    )
    existing_df = _query_stored_daily_rows(engine, cleaned_syms, start_d, end_d)
    covered_by_sym: dict[str, set[date]] = {s: set() for s in cleaned_syms}
    if not existing_df.empty:
        for row in existing_df.itertuples(index=False):
            sk = str(row.symbol).strip().upper()
            if sk in covered_by_sym:
                covered_by_sym[sk].add(resolve_daily_session_date(row.ts))

    all_windows = _build_date_windows(start_d, end_d, chunk_days)
    run_report = DailyFetchRunReport(
        from_date=start_d.isoformat(),
        to_date=end_d.isoformat(),
        dry_run=dry_run,
        pilot_mode=not allow_bulk,
        checkpoint_path=str(checkpoint_path) if checkpoint_path else None,
    )

    consec_gateway_fails = 0
    current_delay = max(0.0, float(base_delay_s))

    for sym in cleaned_syms:
        sym_rep = SymbolFetchReport(symbol=sym, windows_total=len(all_windows))
        if run_report.circuit_breaker_tripped:
            sym_rep.error = "skipped: circuit breaker tripped on earlier 5xx storm"
            run_report.symbols.append(sym_rep)
            continue

        covered = covered_by_sym.get(sym, set())
        pending_windows: list[tuple[date, date, list[date]]] = []
        for w_start, w_end in all_windows:
            exp_in_win = sorted(
                d for d in expected_all if w_start <= d <= w_end and d not in covered
            )
            if not exp_in_win:
                sym_rep.windows_skipped_covered += 1
            else:
                pending_windows.append((w_start, w_end, exp_in_win))

        if not pending_windows:
            run_report.symbols.append(sym_rep)
            continue

        if dry_run:
            run_report.symbols.append(sym_rep)
            continue

        try:
            if token_resolver is not None:
                sec_token, segment = token_resolver(sym)
            else:
                if not resolved_key or not resolved_tok:
                    raise ValueError(
                        "MSTOCK_API_KEY and MSTOCK_ACCESS_TOKEN are required for live HTTP fetch"
                    )
                sec_token, segment = resolve_equity_token_and_segment(
                    engine,
                    sym,
                    base_url=resolved_base,
                    api_key=resolved_key,
                    access_token=resolved_tok,
                )
            sym_rep.security_token = sec_token
            sym_rep.segment = segment
        except Exception as exc:  # noqa: BLE001
            sym_rep.error = str(exc)
            run_report.symbols.append(sym_rep)
            continue

        headers = {
            "X-Mirae-Version": "1",
            "Authorization": f"token {resolved_key}:{resolved_tok}",
        }
        url = (
            f"{resolved_base}/openapi/typea/instruments/historical/"
            f"{segment}/{sec_token}/day"
        )

        collected_bars: list[dict[str, Any]] = []
        for w_start, w_end, exp_in_win in pending_windows:
            if consec_gateway_fails >= max_consecutive_5xx:
                run_report.circuit_breaker_tripped = True
                sym_rep.failed_windows.append(
                    {
                        "from": w_start.isoformat(),
                        "to": w_end.isoformat(),
                        "reason": "circuit_breaker_tripped",
                    }
                )
                break

            # mStock `to` is END-EXCLUSIVE (verified live 2026-10-06).
            params = {
                "from": w_start.isoformat(),
                "to": (w_end + timedelta(days=1)).isoformat(),
            }
            last_err: Exception | None = None
            raw_bars: list[Any] | None = None

            for attempt in range(max(1, int(max_retries_per_window))):
                run_report.http_requests += 1
                try:
                    resp = getter(url, headers=headers, params=params, timeout=30)
                    status_code = int(getattr(resp, "status_code", 200) or 200)
                    if status_code == 429 or status_code >= 500:
                        raise RuntimeError(f"HTTP {status_code}")
                    if status_code >= 400:
                        last_err = RuntimeError(f"HTTP {status_code}")
                        break  # 4xx (non-429) is definite — do not retry
                    payload = resp.json()
                    if payload is None:
                        raw_bars = []
                    else:
                        raw_bars = _extract_mstock_bars(payload)
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    backoff = min(8.0, (0.5 * (2**attempt)) + random.uniform(0.0, 0.15))
                    sleep_fn(backoff)

            if last_err is not None:
                consec_gateway_fails += 1
                current_delay = max(current_delay, float(wide_delay_s))
                sym_rep.failed_windows.append(
                    {
                        "from": w_start.isoformat(),
                        "to": w_end.isoformat(),
                        "reason": str(last_err),
                    }
                )
                if consec_gateway_fails >= max_consecutive_5xx:
                    run_report.circuit_breaker_tripped = True
                    logger.error(
                        "[daily-ingest] Circuit breaker tripped after %d consecutive "
                        "gateway failures (last=%s)",
                        consec_gateway_fails,
                        last_err,
                    )
                    break
                sleep_fn(current_delay)
                continue

            consec_gateway_fails = 0
            current_delay = max(0.0, float(base_delay_s))
            normalized_win: list[dict[str, Any]] = []
            for item in raw_bars or []:
                norm_b = _normalize_raw_bar(item)
                if norm_b is not None:
                    normalized_win.append(norm_b)

            if not normalized_win and exp_in_win:
                # Anti-"silent-None" invariant: 0 bars returned for a window with
                # expected NSE trading days is NEVER counted as clean!
                sym_rep.silent_empty_windows.append(
                    {
                        "from": w_start.isoformat(),
                        "to": w_end.isoformat(),
                        "expected_days": len(exp_in_win),
                        "sample_missing": [d.isoformat() for d in exp_in_win[:5]],
                    }
                )
            else:
                sym_rep.windows_fetched_ok += 1
                collected_bars.extend(normalized_win)

            if current_delay > 0:
                sleep_fn(current_delay)

        if collected_bars:
            # Persist under canonical exchange='NSE' so DbSource/swing queries see
            # one consistent exchange key regardless of BSE/NSE scriptmaster quirk.
            persisted = persist_daily_bars(
                engine,
                sym,
                collected_bars,
                exchange="NSE",
                source="mstock",
                dry_run=False,
            )
            sym_rep.inserted = persisted.inserted
            sym_rep.skipped_conflict = persisted.skipped_conflict
            sym_rep.rejected_invalid_ohlc = persisted.rejected_invalid_ohlc
            run_report.total_inserted += persisted.inserted
            run_report.total_skipped_conflict += persisted.skipped_conflict

        run_report.symbols.append(sym_rep)
        if checkpoint_path is not None:
            _write_checkpoint(Path(checkpoint_path), run_report)

    run_report.elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
    if checkpoint_path is not None:
        _write_checkpoint(Path(checkpoint_path), run_report)
    return run_report


def _write_checkpoint(path: Path, report: DailyFetchRunReport) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — checkpoint write must never crash run
        logger.warning("[daily-ingest] failed to write checkpoint %s: %s", path, exc)
