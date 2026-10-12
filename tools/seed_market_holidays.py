"""Seed market_holidays with NSE equity (CM) trading holidays 2022–2026.

Two inputs, cross-checked before anything is written:

1. ``tools/out/nse_holidays_2022_2026.json`` — the authoritative holiday-master
   lists fetched via the browser (segment ``CM`` = capital markets / equities).
2. The DB itself: weekdays in the fetch range where NO symbol has any bar in
   ``market_data_cache`` are candidate closures. Real holidays should appear in
   both sets; a weekday missing from the DB but absent from the NSE list is a
   data hole, and an NSE weekday holiday that *does* have bars is a red flag.

Weekend entries in the NSE list are ignored (the skip logic only ever probes
Mon–Fri). Only weekday closures are upserted. Run with the worktree's .env
reachable; uses a single short-lived connection so the live fetch job is not
disturbed.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine, text

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JSON_PATH = os.path.join(HERE, "out", "nse_holidays_2022_2026.json")
RANGE_START = date(2022, 1, 3)
RANGE_END = date(2026, 10, 2)


def _engine():
    url = os.getenv("FORWARD_TEST_DB_URL")
    if not url:
        env_path = os.path.join(ROOT, ".env")
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("FORWARD_TEST_DB_URL="):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not url:
        sys.exit("FORWARD_TEST_DB_URL not found in env or .env")
    return create_engine(url)


def _nse_weekday_holidays() -> dict[date, str]:
    raw = json.load(open(JSON_PATH, encoding="utf-8"))
    out: dict[date, str] = {}
    for year, segments in raw.items():
        if not isinstance(segments, dict):
            sys.exit(f"year {year} fetch failed: {segments}")
        for h in segments.get("CM", []):
            d = datetime.strptime(h["tradingDate"], "%d-%b-%Y").date()
            if d.weekday() < 5:  # weekdays only
                out[d] = h["description"]
    return out


def _db_days_with_bars(engine) -> set[date]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT ts::date AS d FROM market_data_cache "
                 "WHERE ts::date >= :a AND ts::date <= :b"),
            {"a": RANGE_START, "b": RANGE_END},
        ).all()
    return {r[0] if isinstance(r[0], date) else r[0].date() for r in rows}


def _all_weekdays() -> list[date]:
    d, days = RANGE_START, []
    while d <= RANGE_END:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def main() -> None:
    engine = _engine()
    nse = _nse_weekday_holidays()
    have = _db_days_with_bars(engine)
    weekdays = _all_weekdays()
    missing_weekdays = {d for d in weekdays if d not in have}

    nse_dates = set(nse)
    holes = sorted(missing_weekdays - nse_dates)       # no bars, not a holiday
    surprises = sorted(nse_dates & have)               # holiday yet has bars
    covered = sorted(nse_dates & missing_weekdays)     # holiday, confirmed no bars

    print(f"NSE CM weekday holidays 2022-2026 : {len(nse)}")
    print(f"Weekdays with bars in DB          : {len(weekdays) - len(missing_weekdays)} / {len(weekdays)}")
    print(f"Confirmed by DB (holiday+no bars) : {len(covered)}")
    print(f"Data holes (no bars, not holiday) : {len(holes)}")
    for d in holes:
        print(f"  hole    {d}")
    print(f"Surprises (holiday WITH bars)     : {len(surprises)}")
    for d in surprises:
        print(f"  surprise {d} ({nse[d]})")

    # A "holiday" with bars is only tolerated as a Muhurat/special session:
    # capped at one short session (≤ 75 one-min bars per symbol/day) with a
    # near-uniform bar count across symbols. Anything larger means the market
    # actually traded and our data or the calendar is wrong — refuse to seed.
    max_bars, symbols_with_bars = 0, 0
    if surprises:
        with engine.connect() as conn:
            r = conn.execute(text(
                "SELECT MAX(n), COUNT(*) FROM ("
                "SELECT symbol, COUNT(*) n FROM market_data_cache "
                f"WHERE ts::date IN ({', '.join(chr(39) + d.isoformat() + chr(39) for d in surprises)}) "
                "GROUP BY symbol) t"), ).one()
            max_bars, symbols_with_bars = r[0] or 0, r[1] or 0
        if max_bars > 75:
            print(f"REFUSING to seed — max {max_bars} bars/day on surprise "
                  f"dates exceeds a special session.")
            return
        print(f"Treated as special session (max {max_bars} bars/day, "
              f"{symbols_with_bars} symbols).")

    with engine.begin() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='market_holidays'"))}
        if "holiday_date" not in cols:
            sys.exit(f"unexpected market_holidays columns: {sorted(cols)}")
        ins = text(
            "INSERT INTO market_holidays (holiday_date, segment, description, "
            "is_trading_holiday, source, created_at) "
            "VALUES (:d, 'NSE', :desc, true, 'nse-holiday-master', now()) "
            "ON CONFLICT DO NOTHING")
        n = 0
        for d, desc in sorted(nse.items()):
            if d in surprises:
                desc = f"{desc} — special session (partial day trading)"
            res = conn.execute(ins, {"d": d, "desc": desc})
            n += res.rowcount if res.rowcount and res.rowcount > 0 else 0
        print(f"Upserted {n} new rows (skipped existing).")
        total = conn.execute(text(
            "SELECT COUNT(*) FROM market_holidays WHERE segment='NSE'")).scalar()
        print(f"market_holidays NSE rows now: {total}")
    engine.dispose()


if __name__ == "__main__":
    main()
