"""
Fetch historical daily OHLCV data for NIFTY 500 (NSE equities) from mStock API
and persist to PostgreSQL / TimescaleDB.

Usage:
    1. Set your TOTP as env var before running:
       set MSTOCK_TOTP=123456
    2. Run:
       PYTHONPATH=src .venv/Scripts/python.exe scripts/fetch_nifty500_historical.py

    Options:
       --symbols RELIANCE,TCS,INFY    Fetch specific symbols only
       --from 2020-01-01               Start date (default: all available)
       --to 2026-08-25                 End date (default: today)
       --dry-run                       Show what would be fetched, don't fetch
       --limit 10                      Max instruments to fetch (for testing)
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from sqlalchemy import create_engine, text

from backtest.live.auth import login, verify_totp, get_auth_code

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_URL = os.getenv(
    "FORWARD_TEST_DB_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/forward_test",
)
MSTOCK_BASE_URL = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")
EXCHANGE = "NSE"
TIMEFRAME = "day"  # default, overridden by --timeframe flag
REQUEST_DELAY = 0.5  # seconds between API calls to avoid rate limits

# Chunk sizes: mStock API returns max 1000 candles per request
MAX_CANDLES = 1000
CHUNK_DAYS = {
    "day": 800,       # ~3.2 years of daily data
    "1min": 2,         # ~2 trading days (375 bars/day)
    "5min": 10,        # ~2 weeks
    "15min": 30,       # ~1.5 months
    "30min": 60,       # ~3 months
    "60min": 120,      # ~6 months
    "1hour": 120,
}

# mStock API uses different interval names than our internal ones
# Our internal: 1min, 5min, 15min, 30min, 60min, day
# mStock API:   minute, 5minute, 15minute, 30minute, 60minute, day
_MSTOCK_INTERVAL_MAP = {
    "1min": "minute",
    "5min": "5minute",
    "15min": "15minute",
    "30min": "30minute",
    "60min": "60minute",
    "1hour": "60minute",
    "day": "day",
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def authenticate() -> str:
    """Login + TOTP -> access token. Uses MSTOCK_TOTP env var or prompts.

    Also accepts MSTOCK_ACCESS_TOKEN to skip login+TOTP entirely.
    """
    # If a token is already provided, skip auth
    existing = os.getenv("MSTOCK_ACCESS_TOKEN", "").strip()
    if existing and len(existing) >= 16:
        print(f"[AUTH] Using existing token from MSTOCK_ACCESS_TOKEN: {existing[:20]}...")
        return existing

    print("[AUTH] Logging in with username/password...")
    login_resp = login()
    if login_resp.get("status") != "success":
        raise RuntimeError(f"Login failed: {login_resp}")
    print("  Login OK")

    print("[AUTH] Waiting for TOTP...")
    code = get_auth_code()
    print(f"  TOTP received (length={len(code)})")

    print("[AUTH] Verifying TOTP...")
    session = verify_totp(code)
    token = session["token"]
    print(f"  Token: {token[:20]}...{token[-10:]}")

    # Cache token for reuse
    with open(".mstock_session_token", "w") as f:
        f.write(token)

    return token


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------
def load_nse_equities(engine, limit: int | None = None, symbols: list[str] | None = None) -> list[dict]:
    """Load equity instruments from the instruments table.

    The mStock scriptmaster stores NSE equities under BSE exchange with
    instrument_type='Equity' (not NSE/EQ). We query both to be safe.
    """
    if symbols:
        placeholders = ", ".join([f":s{i}" for i in range(len(symbols))])
        sql = text(
            f"SELECT tradingsymbol, instrument_token, name, exchange "
            f"FROM instruments "
            f"WHERE ((exchange = 'NSE' AND instrument_type = 'EQ') OR "
            f"       (exchange = 'BSE' AND instrument_type = 'Equity')) "
            f"AND UPPER(tradingsymbol) IN ({placeholders}) "
            f"ORDER BY tradingsymbol"
        )
        params = {f"s{i}": s.upper() for i, s in enumerate(symbols)}
    else:
        sql = text(
            "SELECT tradingsymbol, instrument_token, name, exchange "
            "FROM instruments "
            "WHERE (exchange = 'NSE' AND instrument_type = 'EQ') OR "
            "      (exchange = 'BSE' AND instrument_type = 'Equity') "
            "ORDER BY tradingsymbol"
        )
        params = {}

    if limit and not symbols:
        sql = text(str(sql) + f" LIMIT {int(limit)}")

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


def get_existing_coverage(engine, symbol: str, exchange: str = EXCHANGE, timeframe: str | None = None) -> dict | None:
    """Check what data we already have for a symbol in market_data_cache."""
    tf = timeframe or TIMEFRAME
    sql = text(
        "SELECT min(ts) as earliest, max(ts) as latest, count(*) as cnt "
        "FROM market_data_cache "
        "WHERE symbol = :sym AND exchange = :exch AND timeframe = :tf"
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"sym": symbol, "exch": exchange, "tf": tf}).mappings().first()
    if row and row["cnt"] > 0:
        return {"earliest": row["earliest"], "latest": row["latest"], "count": row["cnt"]}
    return None


# ---------------------------------------------------------------------------
# Fetch from mStock API
# ---------------------------------------------------------------------------
# CHUNK_DAYS is now a dict defined at the top of the file


def _extract_bars(payload) -> list[dict]:
    """Extract bar list from various mStock response shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ["data", "candles", "result", "bars", "historical"]:
            if key in payload:
                value = payload[key]
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    for k2 in ["data", "candles", "result", "bars", "historical"]:
                        if k2 in value and isinstance(value[k2], list):
                            return value[k2]
    return []


def _fetch_chunk(api_key: str, token: str, url: str, from_date: str, to_date: str) -> list[dict]:
    """Fetch one date-range chunk from the mStock historical endpoint."""
    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"token {api_key}:{token}",
    }
    params = {"from": from_date, "to": to_date}
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    return _extract_bars(resp.json())


def fetch_bars(token: str, security_token: str, from_date: str, to_date: str, segment: str = EXCHANGE, timeframe: str | None = None) -> list[dict]:
    """Fetch OHLCV bars from mStock, chunking date ranges to stay under 1000 candle limit."""
    tf = timeframe or TIMEFRAME
    mstock_tf = _MSTOCK_INTERVAL_MAP.get(tf, tf)  # Map to mStock API interval name
    api_key = os.getenv('MSTOCK_API_KEY', '')
    url = f"{MSTOCK_BASE_URL}/openapi/typea/instruments/historical/{segment}/{security_token}/{mstock_tf}"

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")

    # Dynamic chunk size based on timeframe
    chunk_days = CHUNK_DAYS.get(tf, 800)

    all_bars = []
    chunk_start = start

    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
        bars = _fetch_chunk(
            api_key, token, url,
            chunk_start.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )
        all_bars.extend(bars)
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(0.2)  # rate limit between chunks

    return all_bars


# ---------------------------------------------------------------------------
# Persist to DB
# ---------------------------------------------------------------------------
def persist_bars(engine, bars: list[dict], symbol: str, exchange: str, timeframe: str) -> int:
    """Upsert OHLCV bars into market_data_cache. Returns rows inserted/updated."""
    if not bars:
        return 0

    sql = text("""
        INSERT INTO market_data_cache
            (symbol, exchange, timeframe, ts, open, high, low, close, volume, source, ingested_at)
        VALUES
            (:symbol, :exchange, :timeframe, :ts, :open, :high, :low, :close, :volume, :source, now())
        ON CONFLICT (symbol, exchange, timeframe, ts) DO UPDATE
            SET open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                ingested_at = now()
    """)

    rows = []
    skipped_bad = 0
    for bar in bars:
        if isinstance(bar, dict):
            ts_raw = bar.get("t", bar.get("time", bar.get("timestamp")))
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            o = float(bar.get("o", bar.get("open", 0)))
            h = float(bar.get("h", bar.get("high", 0)))
            l = float(bar.get("l", bar.get("low", 0)))
            c = float(bar.get("c", bar.get("close", 0)))
            v = int(bar.get("v", bar.get("volume", 0)))
        elif isinstance(bar, (list, tuple)) and len(bar) >= 6:
            ts_raw = bar[0]
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            o = float(bar[1])
            h = float(bar[2])
            l = float(bar[3])
            c = float(bar[4])
            v = int(bar[5])
        else:
            continue

        # Skip rows that violate OHLC sanity (some API rows have bad data)
        # Check constraint: high >= all, low <= all, all > 0
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            skipped_bad += 1
            continue
        if h < l or h < o or h < c or l > o or l > c:
            skipped_bad += 1
            continue

        rows.append({
            "symbol": symbol,
            "exchange": exchange,
            "timeframe": timeframe,
            "ts": ts.to_pydatetime(),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
            "source": "mstock",
        })
    if skipped_bad:
        print(f" [filtered {skipped_bad} bad OHLC rows]", end="")

    if not rows:
        return 0

    # Batch insert in chunks of 500
    chunk_size = 500
    total = 0
    with engine.connect() as conn:
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            conn.execute(sql, chunk)
            total += len(chunk)
        conn.commit()

    return total


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------
def print_summary(engine):
    """Print a summary of all data in market_data_cache."""
    sql = text("""
        SELECT symbol, count(*) as bars,
               min(ts) as earliest, max(ts) as latest
        FROM market_data_cache
        WHERE exchange IN ('NSE', 'BSE') AND timeframe = 'day'
        GROUP BY symbol
        ORDER BY symbol
    """)

    # Get total count
    total_sql = text("SELECT count(DISTINCT symbol) as symbols, count(*) as total_bars FROM market_data_cache WHERE exchange IN ('NSE', 'BSE') AND timeframe = 'day'")
    with engine.connect() as conn:
        totals = conn.execute(total_sql).mappings().first()
        rows = conn.execute(sql).mappings().all()

    print(f"\n{'='*70}")
    print(f"  DB SUMMARY: {totals['symbols']} symbols, {totals['total_bars']} total bars")
    print(f"{'='*70}")
    print(f"  {'Symbol':<15} {'Bars':>6}  {'From':<12} {'To':<12}")
    print(f"  {'-'*55}")
    for r in rows:
        print(f"  {r['symbol']:<15} {r['bars']:>6}  {str(r['earliest'])[:10]:<12} {str(r['latest'])[:10]:<12}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Fetch NIFTY 500 historical data from mStock")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated list of trading symbols (e.g. RELIANCE,TCS,INFY)")
    parser.add_argument("--csv", type=str, default=None,
                        help="Path to CSV file with a 'Symbol' column (e.g. stock-list/nse_ind_nifty200list.csv)")
    parser.add_argument("--from", dest="from_date", type=str, default="2020-01-01",
                        help="Start date YYYY-MM-DD (default: 2020-01-01)")
    parser.add_argument("--to", dest="to_date", type=str, default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max instruments to fetch (for testing)")
    parser.add_argument("--timeframe", type=str, default="day",
                        choices=["day", "1min", "5min", "15min", "30min", "60min", "1hour"],
                        help="Timeframe to fetch (default: day)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without fetching")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip symbols that already have data in DB")
    args = parser.parse_args()

    # Override global TIMEFRAME from CLI
    global TIMEFRAME
    TIMEFRAME = args.timeframe

    to_date = args.to_date or date.today().isoformat()
    from_date = args.from_date
    symbols_list = None
    if args.csv:
        csv_df = pd.read_csv(args.csv)
        sym_col = None
        for candidate in ["Symbol", "symbol", "SYMBOL", "Tradingsymbol", "tradingsymbol"]:
            if candidate in csv_df.columns:
                sym_col = candidate
                break
        if sym_col is None:
            print(f"ERROR: CSV has no 'Symbol' column. Found: {list(csv_df.columns)}")
            sys.exit(1)
        symbols_list = [str(s).strip().upper() for s in csv_df[sym_col].tolist()]
        print(f"  Loaded {len(symbols_list)} symbols from {args.csv}")
    elif args.symbols:
        symbols_list = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    print("=" * 70)
    print("  NIFTY 500 Historical Data Fetcher (mStock -> PostgreSQL)")
    print("=" * 70)
    print(f"  Date range:  {from_date} -> {to_date}")
    print(f"  Exchange:    NSE + BSE")
    print(f"  Timeframe:   {TIMEFRAME}")
    if symbols_list:
        print(f"  Symbols:     {symbols_list}")
    if args.limit:
        print(f"  Limit:       {args.limit} instruments")
    print()

    # Step 2: Load instruments from DB (no auth needed for dry-run)
    engine = create_engine(DB_URL, echo=False)
    instruments = load_nse_equities(engine, limit=args.limit, symbols=symbols_list)
    print(f"\n[DATA] Loaded {len(instruments)} instruments from DB")

    if not instruments:
        print("  No instruments found. Check your filters.")
        sys.exit(1)

    if args.dry_run:
        print("\n[DRY RUN] Would fetch data for:")
        for inst in instruments:
            print(f"  {inst['tradingsymbol']:<15} token={inst['instrument_token']}  {inst['name']}")
        sys.exit(0)

    # Step 1: Authenticate (only needed for live fetch)
    token = authenticate()

    # Step 3: Fetch and persist for each instrument
    stats = {"success": 0, "skipped": 0, "failed": 0, "total_bars": 0}
    failed_symbols = []

    for i, inst in enumerate(instruments, 1):
        symbol = inst["tradingsymbol"]
        sec_token = str(inst["instrument_token"])
        inst_exchange = inst.get("exchange", EXCHANGE)

        # Check existing coverage
        if args.skip_existing:
            coverage = get_existing_coverage(engine, symbol, inst_exchange)
            if coverage and coverage["count"] > 100:
                print(f"  [{i}/{len(instruments)}] {symbol}: SKIP ({coverage['count']} bars already)")
                stats["skipped"] += 1
                continue

        print(f"  [{i}/{len(instruments)}] {symbol} ({inst_exchange} token={sec_token})...", end=" ", flush=True)

        try:
            bars = fetch_bars(token, sec_token, from_date, to_date, segment=inst_exchange, timeframe=TIMEFRAME)
            if not bars:
                print("no data returned")
                stats["skipped"] += 1
                continue

            inserted = persist_bars(engine, bars, symbol, inst_exchange, TIMEFRAME)
            stats["success"] += 1
            stats["total_bars"] += inserted
            print(f"{len(bars)} bars -> {inserted} persisted")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:200] if e.response is not None else ""
            print(f"HTTP {status} error: {body}")
            failed_symbols.append((symbol, f"{status}: {body[:80]}"))
            stats["failed"] += 1
        except Exception as e:
            print(f"ERROR: {e}")
            failed_symbols.append((symbol, str(e)[:80]))
            stats["failed"] += 1

        # Rate limiting
        time.sleep(REQUEST_DELAY)

    # Step 4: Summary
    print(f"\n{'='*70}")
    print(f"  DONE")
    print(f"{'='*70}")
    print(f"  Success:    {stats['success']}")
    print(f"  Skipped:    {stats['skipped']}")
    print(f"  Failed:     {stats['failed']}")
    print(f"  Total bars: {stats['total_bars']}")

    if failed_symbols:
        print(f"\n  Failed symbols:")
        for sym, err in failed_symbols:
            print(f"    {sym}: {err}")

    # DB summary
    print_summary(engine)
    engine.dispose()


if __name__ == "__main__":
    main()
