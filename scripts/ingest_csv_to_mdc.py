#!/usr/bin/env python3
"""Ingest a broker/export CSV of OHLCV bars into ``market_data_cache``.

The Data tab fetches through the broker's HTTP API. This script is the offline
equivalent: it takes a CSV you already have and writes it to the same table
with the same upsert semantics, so ``DbSource`` (and therefore every backtest,
compare and forward run) sees it exactly as if the fetch had succeeded.

It is deliberately tolerant about shape, because broker exports disagree:

* delimiter — comma / semicolon / tab / whitespace
* header names — ``ts|time|timestamp|date|datetime`` and
  ``open|o``, ``high|h``, ``low|l``, ``close|c``, ``volume|vol|v``
* a separate ``date`` + ``time`` column pair
* ISO timestamps, ``YYYY-MM-DD HH:MM:SS``, and epoch seconds/millis
* an optional symbol column (used when ``--symbol`` is not given)

It is deliberately STRICT about values — the same checks
``data_manager._persist_bars`` applies, so a bad bar is reported rather than
stored: positive prices, OHLC consistency (``high >= max(o,c)``,
``low <= min(o,c)``).

Usage::

    PYTHONPATH=src python scripts/ingest_csv_to_mdc.py \\
        --csv ~/reliance_1min_sept2026.csv --symbol RELIANCE --timeframe 1min

    # see what it would do, touch nothing
    PYTHONPATH=src python scripts/ingest_csv_to_mdc.py --csv ... --symbol RELIANCE --dry-run

Timezone: bars are stored with naive timestamps, exactly as given. The column
convention (IST vs UTC) is *detected and reported*, never guessed at silently —
see ``scripts/diagnose_timeframe_alignment.py --symbol X`` for the full
alignment picture.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from backtest.data.base import CANONICAL_TIMEFRAMES, normalize_timeframe
from backtest.db.config import get_db_url

SESSION_START_MIN = 9 * 60 + 15
IST_OFFSET_MIN = 5 * 60 + 30

#: A column that IS the whole timestamp. Deliberately excludes a bare "time"
#: or "date": those are half of a pair, and treating "time" as a full stamp
#: stamps every bar on the day the script ran (found 2026-10-05 on the first
#: real date+time export — silent corruption, no error).
_FULL_TS_ALIASES = ("ts", "timestamp", "datetime", "date_time", "bar_time", "bartime")
_DATE_ALIASES = ("date", "trade_date", "day")
_TIME_ALIASES = ("time", "bar_time", "clock", "timestamp")
_OHLCV_ALIASES = {
    "open": ("open", "o", "open_price"),
    "high": ("high", "h", "high_price"),
    "low": ("low", "l", "low_price"),
    "close": ("close", "c", "close_price", "ltp"),
    "volume": ("volume", "vol", "v", "qty", "quantity", "volume_traded"),
}
_SYMBOL_ALIASES = ("symbol", "tradingsymbol", "ticker", "scrip", "instrument")


def _find(cols: list[str], aliases: tuple[str, ...]) -> str | None:
    """Exact alias match only. Used for OHLCV, where loose matching is unsafe
    (a substring rule would let ``o`` match ``volume`` or ``close``)."""
    lowered = {c.strip().lower(): c for c in cols}
    for a in aliases:
        if a in lowered:
            return lowered[a]
    return None


def _find_timestamp_col(cols: list[str]) -> str | None:
    """Find a FULL-timestamp column, tolerating suffixed broker names.

    Real exports name this column by convention, not by our vocabulary:
    ``ts_ist`` (the first real file), ``bar_ts``, ``datetime_ist``. Exact
    matching missed all of them, so a perfectly good file was rejected.

    Only timestamp-ish hints are matched loosely, and a bare ``date``/``time``
    is excluded — those are halves of a pair and are handled separately.
    """
    hints = ("timestamp", "datetime", "date_time", "bartime", "bar_time")
    lowered = {c.strip().lower(): c for c in cols}
    for a in _FULL_TS_ALIASES:
        if a in lowered:
            return lowered[a]
    for name, original in lowered.items():
        if name in ("date", "time", "day"):
            continue
        if any(h in name for h in hints):
            return original
        # "ts" / "ts_ist" / "bar_ts" — token-based, so "lots" never matches
        tokens = name.replace("-", "_").split("_")
        if "ts" in tokens:
            return original
    return None


def _parse_timestamps(df: pd.DataFrame, cols: list[str]) -> tuple[pd.Series, str]:
    """Return (naive timestamps, how they were built).

    Resolution order matters. A ``date`` + ``time`` pair must be combined
    BEFORE either half is considered on its own, or a date+time export
    silently collapses onto one day.
    """
    date_col = _find(cols, _DATE_ALIASES)
    time_col = _find(cols, _TIME_ALIASES)
    full_col = _find_timestamp_col(cols)

    # 1. a genuine full-timestamp column
    if full_col is not None:
        raw = df[full_col]
        if pd.api.types.is_numeric_dtype(raw):
            # epoch seconds (~1e9) or milliseconds (~1e12)
            biggest = float(pd.to_numeric(raw, errors="coerce").abs().max() or 0)
            unit = "ms" if biggest > 1e11 else "s"
            parsed = pd.to_datetime(raw, unit=unit, errors="coerce")
            return parsed, f"epoch {unit} column '{full_col}'"
        parsed = pd.to_datetime(raw, errors="coerce", format="mixed")
        if parsed.dt.tz is not None:
            parsed = parsed.dt.tz_convert("UTC").dt.tz_localize(None)
        return parsed, f"column '{full_col}'"

    # 2. date + time as a pair (the common broker export)
    if date_col and time_col:
        joined = (
            df[date_col].astype(str).str.strip() + " " + df[time_col].astype(str).str.strip()
        )
        parsed = pd.to_datetime(joined, errors="coerce", format="mixed")
        return parsed, f"'{date_col}' + '{time_col}'"

    # 3. a lone time column — usable only for a single-session export
    if time_col is not None:
        parsed = pd.to_datetime(df[time_col], errors="coerce", format="mixed")
        if parsed.notna().any() and parsed.dt.normalize().nunique() == 1:
            return parsed, f"column '{time_col}' (single session)"

    if date_col is not None:
        parsed = pd.to_datetime(df[date_col], errors="coerce", format="mixed")
        return parsed, f"column '{date_col}' (midnight stamps)"

    raise SystemExit(
        "could not find a timestamp column. Looked for a full stamp "
        f"{list(_FULL_TS_ALIASES)}, a {list(_DATE_ALIASES)} + {list(_TIME_ALIASES)} pair, "
        f"or either half alone. Columns seen: {cols}"
    )


def _runs(days: list) -> list[list]:
    """Collapse consecutive dates into runs — ``04, 07, 10`` stays three lines,
    but an unbroken ``22, 23, 24`` hole reads as one range instead of three."""
    out: list[list] = []
    for day in days:
        if out and (day - out[-1][-1]).days == 1:
            out[-1].append(day)
        else:
            out.append([day])
    return out


def _read_any(path: Path) -> pd.DataFrame:
    """Read with delimiter sniffing; fall back across the common separators."""
    for sep in (None, ",", ";", "\t", r"\s+"):
        try:
            df = pd.read_csv(path, sep=sep, engine="python" if sep == r"\s+" else "c")
            if df.shape[1] >= 5:
                return df
        except Exception:  # noqa: BLE001 — try the next separator
            continue
    raise SystemExit(f"could not parse {path} as CSV")


def _detect_timezone(ts: pd.Series) -> tuple[str, int, str]:
    """Classify the minute-floor of the session: 09:15 => IST, 03:45 => UTC."""
    mins = ts.dt.hour * 60 + ts.dt.minute
    by_day = mins.groupby(ts.dt.normalize()).min()
    if by_day.empty:
        return "unknown", 0, "no parsable timestamps"
    mode = int(by_day.mode().iloc[0])
    hhmm = f"{mode // 60:02d}:{mode % 60:02d}"
    if abs(mode - SESSION_START_MIN) <= 5:
        return "ist", 0, f"naive IST (session starts {hhmm})"
    if abs(mode - (SESSION_START_MIN - IST_OFFSET_MIN)) <= 5:
        hhmm = f"{mode // 60:02d}:{mode % 60:02d}"
        return "utc", IST_OFFSET_MIN, f"naive UTC (session starts {hhmm})"
    hhmm = f"{mode // 60:02d}:{mode % 60:02d}"
    return "unknown", 0, f"neither IST nor UTC (session starts {hhmm})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--symbol", help="symbol to store under (else taken from a symbol column)")
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--timeframe", default="1min", help="canonical name, e.g. 1min / 5min / 1day")
    ap.add_argument(
        "--source", default="csv-import", help="provenance stamped in the source column"
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tf = normalize_timeframe(args.timeframe)
    if tf is None:
        raise SystemExit(
            f"unknown timeframe {args.timeframe!r}; expected one of "
            f"{', '.join(CANONICAL_TIMEFRAMES)}"
        )

    if not args.csv.exists():
        raise SystemExit(f"no such file: {args.csv}")

    df = _read_any(args.csv)
    cols = [str(c) for c in df.columns]
    print(f"read {len(df):,} rows, columns: {cols}")

    symbol = args.symbol
    if not symbol:
        sym_col = _find(cols, _SYMBOL_ALIASES)
        if not sym_col:
            raise SystemExit("no --symbol given and no symbol column found")
        symbol = str(df[sym_col].dropna().iloc[0]).strip().upper()
    symbol = symbol.strip().upper()

    ts, how = _parse_timestamps(df, cols)
    bad_ts = int(ts.isna().sum())

    out = pd.DataFrame({"ts": ts})
    missing = []
    for field, aliases in _OHLCV_ALIASES.items():
        col = _find(cols, aliases)
        if col is None:
            if field == "volume":
                out["volume"] = 0
                continue
            missing.append(field)
            continue
        out[field] = pd.to_numeric(df[col], errors="coerce")
    if missing:
        raise SystemExit(f"missing required price column(s): {missing}. Columns seen: {cols}")

    before = len(out)
    out = out.dropna(subset=["ts", "open", "high", "low", "close"])
    dropped_ts = before - len(out)

    # Same value checks _persist_bars applies — a bad bar is reported, not stored.
    nonpos = (out[["open", "high", "low", "close"]] <= 0).any(axis=1)
    ohlc_bad = (
        (out["high"] < out["low"])
        | (out["high"] < out[["open", "close"]].max(axis=1))
        | (out["low"] > out[["open", "close"]].min(axis=1))
    )
    # Duplicates must be DROPPED, not merely counted: the table's
    # UNIQUE(symbol, exchange, timeframe, ts) rejects the whole batch
    # otherwise. "Last wins" matches the ON CONFLICT upsert semantics.
    dupe_mask = out["ts"].duplicated(keep="last")

    rejected = int(nonpos.sum() + ohlc_bad.sum())
    out = out[~(nonpos | ohlc_bad)].copy()
    out = out.drop_duplicates(subset=["ts"], keep="last")

    tz_verdict, ist_shift, tz_label = _detect_timezone(out["ts"])
    mins = out["ts"].dt.hour * 60 + out["ts"].dt.minute
    per_session = mins.groupby(out["ts"].dt.normalize()).size()

    print()
    print(f"symbol            {symbol}   timeframe {tf}   exchange {args.exchange}")
    print(f"timestamps from   {how}")
    print(f"rows parsed       {len(out):,}")
    if bad_ts or dropped_ts:
        print(f"  dropped         {bad_ts} unparsable ts, {dropped_ts} with null OHLC")
    if rejected:
        print(f"  rejected        {rejected} bars failing positivity/OHLC checks")
    if int(dupe_mask.sum()):
        print(f"  duplicates      {int(dupe_mask.sum())} repeated timestamps (last kept)")
    print(f"range             {out['ts'].min()}  ..  {out['ts'].max()}")
    med = int(per_session.median())
    print(f"sessions          {len(per_session)}  (median {med} bars/session)")
    print(f"storage timezone  {tz_label}")
    # Printed next to the verdict it belongs to, not after the gap block below.
    if tz_verdict == "utc":
        print("                  -> 1hour/4hour resampling will be offset. Run")
        print("                     scripts/diagnose_timeframe_alignment.py after ingesting.")
    elif tz_verdict == "unknown":
        print("                  -> not an NSE session shape; check the export.")

    # Weekday holes. A broker export that drops whole sessions still ingests
    # cleanly — every bar it does contain is valid — and the backtest then runs
    # on a half-empty month without a word. On the real RELIANCE file this
    # reported 7 missing sessions, of which only one (14 Sep, Ganesh Chaturthi)
    # is an NSE holiday: the other six are data the fetch never returned.
    # The trading calendar is not available here, so every weekday in the range
    # is listed and the operator judges — the point is that it is not silent.
    span = pd.date_range(out["ts"].min().normalize(), out["ts"].max().normalize(), freq="B")
    have = set(out["ts"].dt.normalize())
    missing = [d for d in span if d not in have]
    if missing:
        print(f"weekday gaps      {len(missing)} of {len(span)} weekdays in range hold no bars:")
        for group in _runs([d.date() for d in missing]):
            span_txt = f"{group[0]}" if len(group) == 1 else f"{group[0]}..{group[-1]}"
            print(f"                    {span_txt} ({len(group)} day"
                  f"{'s' if len(group) > 1 else ''})")
        print("                  -> check these against the NSE holiday list. A gap that")
        print("                     is NOT a holiday is missing data, and a backtest will")
        print("                     happily run on it.")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    if not len(out):
        raise SystemExit("no valid bars to write")

    engine = create_engine(get_db_url())
    rows = [
        {
            "symbol": symbol,
            "exchange": args.exchange,
            "timeframe": tf,
            "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume or 0),
            "source": args.source,
        }
        for r in out.itertuples()
    ]

    # Postgres is the deployment target and has ON CONFLICT; sqlite (dev, CI)
    # is handled by the same statement through SQLAlchemy when the dialect
    # supports it, otherwise by a delete-then-insert of the same key set.
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(
                text(
                    """
                    INSERT INTO market_data_cache
                        (symbol, exchange, timeframe, ts, open, high, low, close, volume, source)
                    VALUES
                        (:symbol, :exchange, :timeframe, :ts, :open, :high, :low, :close,
                         :volume, :source)
                    ON CONFLICT (symbol, exchange, timeframe, ts) DO UPDATE
                        SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                            close = EXCLUDED.close, volume = EXCLUDED.volume,
                            source = EXCLUDED.source
                    """
                ),
                rows,
            )
        else:
            conn.execute(
                text(
                    "DELETE FROM market_data_cache WHERE symbol = :symbol "
                    "AND timeframe = :timeframe"
                ),
                {"symbol": symbol, "timeframe": tf},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO market_data_cache
                        (symbol, exchange, timeframe, ts, open, high, low, close, volume, source)
                    VALUES
                        (:symbol, :exchange, :timeframe, :ts, :open, :high, :low, :close,
                         :volume, :source)
                    """
                ),
                rows,
            )

    sql = text("SELECT COUNT(*) FROM market_data_cache WHERE symbol = :s AND timeframe = :t")
    with engine.connect() as conn:
        n = conn.execute(sql, {"s": symbol, "t": tf}).scalar()
    print(f"\nwrote {len(rows):,} rows — {symbol}/{tf} now holds {n:,} bars in market_data_cache")
    print("\nnext:  PYTHONPATH=src python scripts/diagnose_timeframe_alignment.py "
          f"--symbol {symbol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
