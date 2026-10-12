#!/usr/bin/env python3
"""Scan daily bars for suspected unadjusted corporate actions (review §3.3).

Reads ``{root}/{SYMBOL}.csv`` files (the CSV data root) and flags every
1-day close-to-close move beyond ``daily_bar.split_suspect_return_pct``
(default ±40% — see ``config/data_quality.yaml``). Each suspect is annotated
with whether the corporate-action calendar already explains it and what
split factor the move implies.

Manual-confirm loop:
  1. run this scan;
  2. for every UNEXPLAINED suspect, confirm the corporate action from NSE
     announcements and add a row to ``data/corporate_actions.csv``
     (columns: symbol,ex_date,kind,adjustment_factor[,amount,note]);
  3. flip ``daily_bar.corporate_actions.enabled: true`` — from then on
     ``mode='backtest'`` runs back-adjust those bars at read time.

Usage:
    python scripts/scan_split_suspects.py [--root data] [--threshold 40]
                                          [--calendar data/corporate_actions.csv]
                                          [--strict] [--csv OUT]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from backtest.data.corporate_actions import (  # noqa: E402
    DEFAULT_SPLIT_SUSPECT_RETURN_PCT,
    CorporateActionCalendar,
)
from backtest.data.csv_source import CsvSource  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default="data", help="CSV data root (default: data)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_SPLIT_SUSPECT_RETURN_PCT,
        help="suspect |1-day return| %% (default: 40)",
    )
    parser.add_argument(
        "--calendar",
        default=None,
        help="known-actions CSV to annotate suspects (default: policy's actions_csv)",
    )
    parser.add_argument("--csv", default=None, help="write the suspect report as CSV")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if any UNEXPLAINED suspect is found (CI/cron gate)",
    )
    args = parser.parse_args()

    calendar = CorporateActionCalendar()
    calendar_path = args.calendar or "data/corporate_actions.csv"
    if os.path.exists(calendar_path):
        from backtest.data.corporate_actions import calendar_from_csv

        calendar = calendar_from_csv(calendar_path)
        print(f"[scan] known actions: {len(calendar)} from {calendar_path}")
    else:
        print(f"[scan] no actions file at {calendar_path} — every suspect is unexplained")

    root = Path(args.root)
    files = sorted(root.glob("*.csv"))
    if not files:
        print(f"[scan] no CSVs under {root}/ — nothing to scan")
        return 0

    source = CsvSource(root=str(root))
    total_suspects = 0
    unexplained = 0
    rows = []
    for path in files:
        symbol = path.stem.upper()
        try:
            df = source.get_candles(symbol, "", "", interval="1day")
        except Exception as exc:  # noqa: BLE001 — report and keep scanning
            print(f"[scan] {symbol}: unreadable ({exc})")
            continue
        for rec in calendar.suspects(df, symbol, args.threshold):
            total_suspects += 1
            if not rec["known_action"]:
                unexplained += 1
            flag = "KNOWN" if rec["known_action"] else "UNEXPLAINED"
            nearby = f" ({rec['nearby_action']})" if rec["nearby_action"] else ""
            print(
                f"  {flag:11s} {symbol:12s} {rec['ts'].date()}  "
                f"{rec['prev_close']:10.2f} → {rec['close']:10.2f}  "
                f"{rec['return_pct']:+7.1f}%  implied factor {rec['implied_factor']:.4g}"
                f"{nearby}"
            )
            rows.append(
                {
                    "symbol": symbol,
                    "ts": rec["ts"].date().isoformat(),
                    "prev_close": rec["prev_close"],
                    "close": rec["close"],
                    "return_pct": round(rec["return_pct"], 2),
                    "implied_factor": rec["implied_factor"],
                    "known_action": rec["known_action"],
                    "nearby_action": rec["nearby_action"],
                }
            )

    print(
        f"\n[scan] {len(files)} symbol(s), {total_suspects} suspect(s), "
        f"{unexplained} UNEXPLAINED — add confirmed actions to {calendar_path}"
    )
    if args.csv and rows:
        pd.DataFrame(rows).to_csv(args.csv, index=False)
        print(f"[scan] report → {args.csv}")

    if args.strict and unexplained:
        print("[scan] STRICT: unexplained suspects remain — failing gate", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
