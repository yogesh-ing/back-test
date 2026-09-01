"""
End-to-end integration test: mStock Auth -> NIFTY Data -> PostgreSQL -> Validate.

Flow:
  1. Login with username/password
  2. User enters TOTP from authenticator app
  3. Verify TOTP -> get access token
  4. Resolve NIFTY security token from scriptmaster
  5. Fetch 3 months of daily OHLCV bars
  6. Persist all bars into market_data_cache table (PostgreSQL)
  7. Run SELECT queries to validate row counts, date range, and OHLCV sanity

Run:
    PYTHONPATH=src python tests/test_e2e_nifty_auth_to_db.py
"""

from __future__ import annotations

import io
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from sqlalchemy import create_engine, text

from backtest.live.auth import get_auth_code, login, verify_totp
from backtest.live.mstock import MStockClient, _candles_to_frame

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_URL = os.getenv(
    "FORWARD_TEST_DB_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/forward_test",
)
SYMBOL = "NIFTY"
EXCHANGE = "NSE"
TIMEFRAME = "day"
INTERVAL_DAYS = 90  # 3 months

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_nifty_token(client: MStockClient) -> str:
    """Resolve NIFTY index token from the scriptmaster."""
    resp = requests.get(
        f"{client.base_url}/openapi/typea/instruments/scriptmaster",
        headers=client.headers,
        timeout=30,
    )
    resp.raise_for_status()
    frame = pd.read_csv(io.StringIO(resp.text), low_memory=False)

    # Find symbol and token columns
    cols_lower = {str(c).strip().lower(): c for c in frame.columns}
    symbol_col = next(
        (
            cols_lower[k]
            for k in ("tradingsymbol", "symbol", "name", "instrumentname")
            if k in cols_lower
        ),
        None,
    )
    token_col = next(
        (
            cols_lower[k]
            for k in ("instrument_token", "token", "securitytoken", "security_token")
            if k in cols_lower
        ),
        None,
    )
    if not symbol_col or not token_col:
        raise ValueError(f"Missing columns in scriptmaster: {list(frame.columns)[:10]}")

    mask = frame[symbol_col].astype(str).str.upper().str.contains("NIFTY", na=False)
    # Exclude derivatives and ETFs
    text_col = frame[symbol_col].astype(str).str.upper()
    mask = mask & ~text_col.str.contains("FUT|OPT| CE| PE|ETF|BEES", na=False)
    matches = frame[mask]
    if matches.empty:
        raise ValueError("NIFTY not found in scriptmaster")

    token = str(matches.iloc[0][token_col]).strip()
    print(f"  Resolved NIFTY -> token={token}")
    return token


def _persist_to_db(bars: list[dict], symbol: str, exchange: str, timeframe: str) -> int:
    """Insert OHLCV bars into market_data_cache and return row count."""
    engine = create_engine(DB_URL, echo=False)

    insert_sql = text(
        """
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
    """
    )

    rows = []
    for bar in bars:
        if isinstance(bar, dict):
            ts_raw = bar.get("t", bar.get("time", bar.get("timestamp")))
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            rows.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": timeframe,
                    "ts": ts.to_pydatetime(),
                    "open": float(bar.get("o", bar.get("open", 0))),
                    "high": float(bar.get("h", bar.get("high", 0))),
                    "low": float(bar.get("l", bar.get("low", 0))),
                    "close": float(bar.get("c", bar.get("close", 0))),
                    "volume": int(bar.get("v", bar.get("volume", 0))),
                    "source": "mstock",
                }
            )
        elif isinstance(bar, (list, tuple)) and len(bar) >= 6:
            ts_raw = bar[0]
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            rows.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": timeframe,
                    "ts": ts.to_pydatetime(),
                    "open": float(bar[1]),
                    "high": float(bar[2]),
                    "low": float(bar[3]),
                    "close": float(bar[4]),
                    "volume": int(bar[5]),
                    "source": "mstock",
                }
            )

    if not rows:
        raise ValueError("No rows to persist")

    with engine.connect() as conn:
        conn.execute(insert_sql, rows)
        conn.commit()

    engine.dispose()
    return len(rows)


def _validate_in_db(symbol: str, exchange: str, timeframe: str) -> dict:
    """Run SELECT validation queries and return a summary dict."""
    engine = create_engine(DB_URL, echo=False)
    summary = {}

    with engine.connect() as conn:
        # 1. Row count
        count_row = (
            conn.execute(
                text(
                    "SELECT count(*) as cnt FROM market_data_cache "
                    "WHERE symbol = :symbol AND exchange = :exchange AND timeframe = :timeframe"
                ),
                {"symbol": symbol, "exchange": exchange, "timeframe": timeframe},
            )
            .mappings()
            .first()
        )
        summary["row_count"] = count_row["cnt"]

        # 2. Date range
        range_row = (
            conn.execute(
                text(
                    "SELECT min(ts) as earliest, max(ts) as latest FROM market_data_cache "
                    "WHERE symbol = :symbol AND exchange = :exchange AND timeframe = :timeframe"
                ),
                {"symbol": symbol, "exchange": exchange, "timeframe": timeframe},
            )
            .mappings()
            .first()
        )
        summary["earliest"] = str(range_row["earliest"])
        summary["latest"] = str(range_row["latest"])

        # 3. OHLCV sanity - no zero/negative prices, no high < low
        bad_rows = (
            conn.execute(
                text(
                    "SELECT count(*) as bad FROM market_data_cache "
                    "WHERE symbol = :symbol AND exchange = :exchange AND timeframe = :timeframe "
                    "AND (open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR high < low)"
                ),
                {"symbol": symbol, "exchange": exchange, "timeframe": timeframe},
            )
            .mappings()
            .first()
        )
        summary["bad_ohlcv_rows"] = bad_rows["bad"]

        # 4. Sample rows (first 5)
        sample = (
            conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume "
                    "FROM market_data_cache "
                    "WHERE symbol = :symbol AND exchange = :exchange AND timeframe = :timeframe "
                    "ORDER BY ts ASC LIMIT 5"
                ),
                {"symbol": symbol, "exchange": exchange, "timeframe": timeframe},
            )
            .mappings()
            .all()
        )
        summary["sample_first_5"] = [dict(r) for r in sample]

        # 5. Latest 5 rows
        latest = (
            conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume "
                    "FROM market_data_cache "
                    "WHERE symbol = :symbol AND exchange = :exchange AND timeframe = :timeframe "
                    "ORDER BY ts DESC LIMIT 5"
                ),
                {"symbol": symbol, "exchange": exchange, "timeframe": timeframe},
            )
            .mappings()
            .all()
        )
        summary["sample_last_5"] = [dict(r) for r in latest]

    engine.dispose()
    return summary


# ---------------------------------------------------------------------------
# Main test flow
# ---------------------------------------------------------------------------


def main():
    print("=" * 70)
    print("  E2E TEST: mStock Auth -> NIFTY Data -> PostgreSQL -> Validate")
    print("=" * 70)

    # ── Step 1: Login ─────────────────────────────────────────────────────
    print("\n[1/6] Logging in with username/password...")
    login_resp = login()
    print(f"  Login response: {login_resp}")
    assert login_resp.get("status") == "success", f"Login failed: {login_resp}"
    print("  OK: Login successful")

    # ── Step 2: TOTP ──────────────────────────────────────────────────────
    print("\n[2/6] Waiting for TOTP code...")
    code = get_auth_code()
    print(f"  TOTP code received (length={len(code)})")

    # ── Step 3: Verify TOTP ───────────────────────────────────────────────
    print("\n[3/6] Verifying TOTP...")
    session = verify_totp(code)
    access_token = session["token"]
    print(f"  Access token: {access_token[:20]}...{access_token[-10:]}")
    print("  OK: TOTP verified")

    # ── Step 4: Resolve NIFTY token ───────────────────────────────────────
    print(f"\n[4/6] Resolving {SYMBOL} security token from scriptmaster...")
    client = MStockClient(token=access_token)
    nifty_token = _resolve_nifty_token(client)

    # ── Step 5: Fetch 3 months of data ────────────────────────────────────
    to_date = date.today()
    from_date = to_date - timedelta(days=INTERVAL_DAYS)
    print(f"\n[5/6] Fetching {SYMBOL} daily bars from {from_date} to {to_date}...")
    # Use the token directly instead of client.get_bars() which does its
    # own lookup and may fail for index symbols like NIFTY.
    resp = requests.get(
        f"{client.base_url}/openapi/typea/instruments/historical/{EXCHANGE}/{nifty_token}/{TIMEFRAME}",
        headers=client.headers,
        params={"from": str(from_date), "to": str(to_date)},
        timeout=30,
    )
    resp.raise_for_status()
    bars = client._extract_bars(resp.json())
    if not bars:
        raise ValueError(f"No bars returned. Response: {resp.text[:500]}")
    print(f"  Received {len(bars)} bars")

    candles = _candles_to_frame(bars)
    print(f"  Parsed into DataFrame: {len(candles)} rows, columns={list(candles.columns)}")
    print(f"  Date range:             {candles.index.min()} -> {candles.index.max()}")
    print(f"  Price range: {candles['close'].min():.2f} -> {candles['close'].max():.2f}")

    # ── Step 6: Persist to PostgreSQL ─────────────────────────────────────
    print(f"\n[6/6] Persisting {len(bars)} bars to PostgreSQL (market_data_cache)...")
    inserted = _persist_to_db(bars, SYMBOL, EXCHANGE, TIMEFRAME)
    print(f"  OK: Inserted/upserted {inserted} rows")

    # ── Validation ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  VALIDATION")
    print("=" * 70)
    summary = _validate_in_db(SYMBOL, EXCHANGE, TIMEFRAME)

    print(f"\n  Row count:              {summary['row_count']}")
    print(f"  Date range:             {summary['earliest']} -> {summary['latest']}")
    print(f"  Bad OHLCV rows:         {summary['bad_ohlcv_rows']}")

    print("\n  First 5 rows:")
    for row in summary["sample_first_5"]:
        print(
            f"    {row['ts']}  O={row['open']:>10.2f}  H={row['high']:>10.2f}  "
            f"L={row['low']:>10.2f}  C={row['close']:>10.2f}  V={row['volume']:>12}"
        )

    print("\n  Last 5 rows:")
    for row in summary["sample_last_5"]:
        print(
            f"    {row['ts']}  O={row['open']:>10.2f}  H={row['high']:>10.2f}  "
            f"L={row['low']:>10.2f}  C={row['close']:>10.2f}  V={row['volume']:>12}"
        )

    # ── Assertions ────────────────────────────────────────────────────────
    errors = []
    if summary["row_count"] < 1:
        errors.append(f"Expected at least 1 row, got {summary['row_count']}")
    if summary["bad_ohlcv_rows"] > 0:
        errors.append(f"Found {summary['bad_ohlcv_rows']} rows with invalid OHLCV data")
    if summary["row_count"] != inserted:
        errors.append(f"Inserted {inserted} but DB has {summary['row_count']}")

    print("\n" + "-" * 70)
    if errors:
        print("  [FAIL] VALIDATION FAILED:")
        for e in errors:
            print(f"     - {e}")
        sys.exit(1)
    else:
        print("  [PASS] ALL VALIDATIONS PASSED")
        print(f"     {summary['row_count']} bars persisted and verified in PostgreSQL")
    print("-" * 70)


if __name__ == "__main__":
    main()
