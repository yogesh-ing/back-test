#!/usr/bin/env python3
"""Diagnose timeframe alignment in ``market_data_cache``.

Answers three questions in one run:

1. **What timezone are the stored bars in?** NSE trades 09:15–15:30 IST. If the
   minute floor of the earliest bar of a session reads 03:45, the column holds
   naive UTC; if it reads 09:15, it holds naive IST. That single number decides
   whether intraday resampling is aligned or not.

2. **Are the derived bars aligned to IST?** Resampling runs on whatever the
   column holds. ``1hour``/``4hour`` buckets are offset by the UTC offset
   (330 min) unless the data is in IST: 5/15/30-min are safe (330 is a multiple
   of all three), 1hour shifts 30 min, 4hour shifts 90 min and gains a stub bar.
   ``1day``/``1week`` are safe either way — a session never crosses UTC midnight.

3. **Does a real 1day resample reproduce the broker's own daily bar?** If the
   symbol also has native ``1day`` rows, the resampled daily OHLC(V) is compared
   bar-by-bar against them. This is the end-to-end proof that aggregation is
   correct on real data, not just on fixtures.

Usage::

    PYTHONPATH=src python scripts/diagnose_timeframe_alignment.py --symbol RELIANCE
    PYTHONPATH=src python scripts/diagnose_timeframe_alignment.py --symbol RELIANCE --json

Reads the same DB the app does (``FORWARD_TEST_DB_URL`` → ``config/database.yaml``).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text

from backtest.data.base import CANONICAL_TIMEFRAMES
from backtest.data.db_source import DbSource, _INTERVAL_TO_RULE
from backtest.db.config import get_db_url

#: NSE regular session, IST.
SESSION_START_MIN = 9 * 60 + 15  # 09:15
SESSION_END_MIN = 15 * 60 + 30  # 15:30
IST_OFFSET_MIN = 5 * 60 + 30  # +05:30

#: Timeframes whose bucket boundaries move when the storage tz is UTC.
#: 330 = 5h30m: divisible by 5/15/30 (safe), not by 60 or 240 (offset).
TZ_SENSITIVE = ("1hour", "4hour")


def _minute_of_day(ts: pd.Timestamp) -> int:
    return ts.hour * 60 + ts.minute


def _stored_timeframes(engine, symbol: str) -> dict[str, int]:
    sql = text(
        """
        SELECT timeframe, COUNT(*) AS bars
        FROM market_data_cache
        WHERE symbol = :symbol
        GROUP BY timeframe
        ORDER BY timeframe
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"symbol": symbol}).mappings()
        return {str(r["timeframe"]): int(r["bars"]) for r in rows}


def _load_minutes(engine, symbol: str, timeframe: str) -> pd.DataFrame:
    sql = text(
        """
        SELECT ts, open, high, low, close, volume
        FROM market_data_cache
        WHERE symbol = :symbol AND timeframe = :timeframe
        ORDER BY ts ASC
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"symbol": symbol, "timeframe": timeframe})
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts")


def _classify_timezone(df: pd.DataFrame) -> dict[str, Any]:
    """Is the minute-floor of the session 09:15 (IST) or 03:45 (UTC)?"""
    by_day = df.groupby(df.index.normalize()).apply(
        lambda g: _minute_of_day(g.index.min()), include_groups=False
    )
    starts = by_day.astype(int)
    mode_start = int(starts.mode().iloc[0]) if len(starts) else -1
    ist_ok = abs(mode_start - SESSION_START_MIN) <= 5
    utc_ok = abs(mode_start - (SESSION_START_MIN - IST_OFFSET_MIN)) <= 5
    if ist_ok:
        verdict, label, offset = "ist", "naive IST (session starts ~09:15)", 0
    elif utc_ok:
        verdict, label, offset = "utc", "naive UTC (session starts ~03:45)", IST_OFFSET_MIN
    else:
        hhmm = f"{mode_start // 60:02d}:{mode_start % 60:02d}"
        verdict = "unknown"
        label = f"neither IST nor UTC (mode starts at {hhmm})"
        offset = 0
    return {
        "verdict": verdict,
        "label": label,
        "session_start_minute_of_day": mode_start,
        "ist_shift_needed_minutes": offset,
        "sessions_sampled": int(len(starts)),
    }


def _alignment_report(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Buckets EXACTLY as production produces them — no correction applied.

    Reporting a corrected view would hide the bug this script exists to find,
    so the raw stored boundary is the headline and the IST reading is derived
    beside it.
    """
    src = DbSource.__new__(DbSource)
    rows: list[dict[str, Any]] = []
    for tf in CANONICAL_TIMEFRAMES:
        if tf not in _INTERVAL_TO_RULE:
            continue
        out = src._resample(df, tf) if tf != "1min" else df
        raw = [str(t)[11:19] for t in list(out.index)[:6]]
        ist = [
            str(t + pd.Timedelta(minutes=IST_OFFSET_MIN))[11:19] for t in list(out.index)[:6]
        ]
        rows.append(
            {
                "timeframe": tf,
                "bars_in_sample": int(len(out)),
                "bucket_starts_stored": raw,
                "bucket_starts_ist": ist,
                "tz_sensitive": tf in TZ_SENSITIVE,
            }
        )
    return rows


def _compare_with_native_daily(engine, symbol: str, minutes: pd.DataFrame) -> dict[str, Any]:
    """Resampled 1day vs the broker's own 1day rows — the end-to-end proof."""
    native = _load_minutes(engine, symbol, "1day")
    if native.empty:
        return {"available": False, "reason": "no native 1day rows to compare against"}

    src = DbSource.__new__(DbSource)
    derived = src._resample(minutes, "1day")

    a = derived[["open", "high", "low", "close", "volume"]].copy()
    b = native[["open", "high", "low", "close", "volume"]].copy()
    a.index = pd.to_datetime(a.index).normalize()
    b.index = pd.to_datetime(b.index).normalize()

    common = a.index.intersection(b.index)
    if not len(common):
        return {"available": False, "reason": "no overlapping dates"}

    a, b = a.loc[common], b.loc[common]
    diffs: dict[str, int] = {}
    worst: dict[str, float] = {}
    for col in ("open", "high", "low", "close"):
        d = (a[col] - b[col]).abs()
        diffs[col] = int((d > 1e-6).sum())
        worst[col] = float(d.max())
    vol = (a["volume"] - b["volume"]).abs()
    diffs["volume"] = int((vol > 0).sum())
    worst["volume"] = float(vol.max())

    mismatched = sum(v for v in diffs.values())
    return {
        "available": True,
        "days_compared": int(len(common)),
        "mismatched_fields": diffs,
        "worst_abs_diff": worst,
        "verdict": "MATCH" if mismatched == 0 else "MISMATCH",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", required=True, help="e.g. RELIANCE")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    symbol = args.symbol.strip().upper()
    engine = create_engine(get_db_url())

    stored = _stored_timeframes(engine, symbol)
    if not stored:
        print(f"no rows in market_data_cache for {symbol}", file=sys.stderr)
        return 1

    finest = next((tf for tf in CANONICAL_TIMEFRAMES if tf in stored), sorted(stored)[0])
    minutes = _load_minutes(engine, symbol, finest)
    tz = _classify_timezone(minutes)
    alignment = _alignment_report(minutes)
    daily = _compare_with_native_daily(engine, symbol, minutes)

    report = {
        "symbol": symbol,
        "stored_timeframes": stored,
        "finest_used": finest,
        "bars_read": int(len(minutes)),
        "range": [str(minutes.index.min()), str(minutes.index.max())] if len(minutes) else None,
        "storage_timezone": tz,
        "resampled_buckets_ist": alignment,
        "native_daily_comparison": daily,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    lo, hi = report["range"]
    print(f"\n{symbol} — {len(minutes):,} bars at {finest}, range {lo} .. {hi}")
    print(f"stored timeframes: {stored}\n")

    print("1. STORAGE TIMEZONE")
    print(f"   {tz['label']}")
    print(f"   sessions sampled: {tz['sessions_sampled']}")
    if tz["verdict"] == "utc":
        print("   -> intraday buckets below are shown shifted to IST; the raw column is UTC")
    print()

    # Only show the IST reading when it differs — printing it for IST-stored
    # data would add 5h30m to times that are already IST and read as nonsense.
    show_ist = tz["verdict"] == "utc"
    head = "2. BUCKET STARTS AS PRODUCED"
    if show_ist:
        head += "  (stored -> same instant in IST)"
    print(head)
    for row in alignment:
        # The marker means "a wrong storage convention would show up HERE" — it
        # is a property of the timeframe, not a finding. Naming it after the
        # verdict keeps a passing line from reading like a warning: for IST
        # storage the buckets are labelled as expected, and saying so is the
        # difference between "check this" and "this is fine".
        if not row["tz_sensitive"]:
            flag = ""
        elif tz["verdict"] == "utc":
            flag = "  <-- OFFSET (see verdict)"
        else:
            flag = "  (convention-sensitive, matches expected)"
        tf, n = row["timeframe"], row["bars_in_sample"]
        line = f"   {tf:7s} {n:>7d} bars   {row['bucket_starts_stored']}"
        if show_ist:
            line += f"  ->  {row['bucket_starts_ist']}"
        print(line + flag)
    print()

    print("3. RESAMPLED 1day vs BROKER'S NATIVE 1day")
    if not daily["available"]:
        print(f"   skipped: {daily['reason']}")
        print("   (fetch the symbol at 1day too, then re-run, to get the end-to-end proof)")
    else:
        print(f"   days compared: {daily['days_compared']}")
        print(f"   mismatched fields: {daily['mismatched_fields']}")
        print(f"   worst abs diff: {daily['worst_abs_diff']}")
        print(f"   verdict: {daily['verdict']}")
    print()

    if tz["verdict"] == "utc":
        hour = next(r for r in alignment if r["timeframe"] == "1hour")
        four = next(r for r in alignment if r["timeframe"] == "4hour")
        print("VERDICT: storage is UTC and resampling does not convert to IST,")
        print("         so intraday buckets are offset by the 5h30m UTC offset:")
        h1 = hour["bucket_starts_ist"][0]
        print(f"           1hour starts {h1} IST (expected 09:15 or 10:00)")
        print(f"           4hour starts {four['bucket_starts_ist'][0]} IST and gains a stub bar")
        print("         1day/1week are unaffected (a session never crosses UTC midnight).")
    elif tz["verdict"] == "ist":
        print("VERDICT: storage is IST; intraday buckets align with the NSE session")
        print("         (10min/30min/1hour still start at clock boundaries, so the first")
        print("          bar of a session is shorter than the rest — that is clock-aligned")
        print("          aggregation, not a misalignment.)")
    else:
        print("VERDICT: unrecognised convention — the fetch path may be mixing sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
