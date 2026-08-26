"""
End-to-end test: mStock Auth -> Fetch ALL Instruments -> PostgreSQL -> Validate.

Flow:
  1. Login with username/password
  2. User provides TOTP from authenticator app
  3. Verify TOTP -> get access token
  4. Fetch complete instrument master from mStock scriptmaster
  5. Create instruments table if it does not exist
  6. Bulk upsert all instruments into PostgreSQL
  7. Run SELECT queries to validate row counts, type breakdown, and sample data

Run:
    MSTOCK_TOTP=XXXXXX PYTHONPATH=src python tests/test_e2e_instruments_to_db.py
"""

from __future__ import annotations

import io
import os
import sys
from datetime import datetime

import pandas as pd
import requests
from sqlalchemy import create_engine, text

from backtest.live.auth import get_auth_code, login, verify_totp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_URL = os.getenv(
    "FORWARD_TEST_DB_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/forward_test",
)
TABLE_NAME = "instruments"

# ---------------------------------------------------------------------------
# Table DDL (created if missing)
# ---------------------------------------------------------------------------
CREATE_TABLE_SQL = text("""
CREATE TABLE IF NOT EXISTS instruments (
    instrument_token   BIGINT PRIMARY KEY,
    exchange_token     BIGINT,
    tradingsymbol      VARCHAR(128) NOT NULL,
    name               VARCHAR(256),
    last_price         NUMERIC(20,4),
    expiry             DATE,
    strike             NUMERIC(20,4),
    tick_size          NUMERIC(12,4),
    lot_size           INTEGER,
    instrument_type    VARCHAR(16),
    segment            VARCHAR(16),
    exchange           VARCHAR(16),
    fetched_at         TIMESTAMP WITH TIME ZONE DEFAULT now()
);
""")

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_instruments_symbol ON instruments (tradingsymbol)",
    "CREATE INDEX IF NOT EXISTS ix_instruments_exchange ON instruments (exchange)",
    "CREATE INDEX IF NOT EXISTS ix_instruments_type ON instruments (instrument_type)",
    "CREATE INDEX IF NOT EXISTS ix_instruments_name ON instruments (name)",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_table(engine):
    """Create instruments table and indexes if they don't exist."""
    with engine.connect() as conn:
        conn.execute(CREATE_TABLE_SQL)
        for idx_sql in CREATE_INDEXES_SQL:
            conn.execute(text(idx_sql))
        conn.commit()
    print("  OK: instruments table ready")


def _bulk_upsert(engine, frame: pd.DataFrame) -> int:
    """Bulk upsert the full instrument master using COPY + temp table for speed."""
    now = datetime.utcnow()

    # Prepare the DataFrame
    df = frame.copy()
    df["fetched_at"] = now
    # Convert NaN -> None for proper NULL insertion
    df = df.where(pd.notnull(df), None)

    # Use COPY for bulk insert (fastest path for PostgreSQL)
    cols = [
        "instrument_token", "exchange_token", "tradingsymbol", "name",
        "last_price", "expiry", "strike", "tick_size", "lot_size",
        "instrument_type", "segment", "exchange", "fetched_at",
    ]

    upsert_sql = text("""
        INSERT INTO instruments
            (instrument_token, exchange_token, tradingsymbol, name,
             last_price, expiry, strike, tick_size, lot_size,
             instrument_type, segment, exchange, fetched_at)
        VALUES
            (:instrument_token, :exchange_token, :tradingsymbol, :name,
             :last_price, :expiry, :strike, :tick_size, :lot_size,
             :instrument_type, :segment, :exchange, :fetched_at)
        ON CONFLICT (instrument_token) DO UPDATE SET
            exchange_token  = EXCLUDED.exchange_token,
            tradingsymbol   = EXCLUDED.tradingsymbol,
            name            = EXCLUDED.name,
            last_price      = EXCLUDED.last_price,
            expiry          = EXCLUDED.expiry,
            strike          = EXCLUDED.strike,
            tick_size       = EXCLUDED.tick_size,
            lot_size        = EXCLUDED.lot_size,
            instrument_type = EXCLUDED.instrument_type,
            segment         = EXCLUDED.segment,
            exchange        = EXCLUDED.exchange,
            fetched_at      = EXCLUDED.fetched_at
    """)

    # Build list of dicts, converting pandas types to native Python
    records = []
    for _, row in df.iterrows():
        rec = {}
        for col in cols:
            val = row[col]
            if pd.isna(val):
                rec[col] = None
            else:
                # Convert numpy types to native Python
                if hasattr(val, "item"):
                    val = val.item()
                rec[col] = val
        records.append(rec)

    # Batch insert (5000 rows per batch to avoid memory issues)
    BATCH_SIZE = 5000
    total_inserted = 0
    with engine.connect() as conn:
        for i in range(0, len(records), BATCH_SIZE):
            batch = records[i : i + BATCH_SIZE]
            conn.execute(upsert_sql, batch)
            total_inserted += len(batch)
            print(f"    Upserted batch {i // BATCH_SIZE + 1}: {len(batch)} rows (total: {total_inserted})")
        conn.commit()

    return total_inserted


def _validate(engine, total_fetched: int) -> dict:
    """Run validation queries and return summary."""
    summary = {}
    with engine.connect() as conn:
        # 1. Total count
        row = conn.execute(text(f"SELECT count(*) as cnt FROM {TABLE_NAME}")).mappings().first()
        summary["total_rows"] = row["cnt"]

        # 2. By exchange
        rows = conn.execute(text(
            f"SELECT exchange, count(*) as cnt FROM {TABLE_NAME} GROUP BY exchange ORDER BY cnt DESC"
        )).mappings().all()
        summary["by_exchange"] = [dict(r) for r in rows]

        # 3. By instrument_type (top 10)
        rows = conn.execute(text(
            f"SELECT instrument_type, count(*) as cnt FROM {TABLE_NAME} "
            f"GROUP BY instrument_type ORDER BY cnt DESC LIMIT 10"
        )).mappings().all()
        summary["by_type_top10"] = [dict(r) for r in rows]

        # 4. By segment (top 10)
        rows = conn.execute(text(
            f"SELECT segment, count(*) as cnt FROM {TABLE_NAME} "
            f"GROUP BY segment ORDER BY cnt DESC LIMIT 10"
        )).mappings().all()
        summary["by_segment_top10"] = [dict(r) for r in rows]

        # 5. Sample: first 5 equity instruments
        rows = conn.execute(text(
            f"SELECT instrument_token, tradingsymbol, name, exchange, instrument_type, tick_size, lot_size "
            f"FROM {TABLE_NAME} WHERE instrument_type = 'EQ' ORDER BY tradingsymbol LIMIT 5"
        )).mappings().all()
        sample_eq = [dict(r) for r in rows]
        summary["sample_equity_5"] = sample_eq

        # 6. Check for NIFTY
        rows = conn.execute(text(
            f"SELECT instrument_token, tradingsymbol, name, exchange, segment "
            f"FROM {TABLE_NAME} WHERE tradingsymbol ILIKE '%NIFTY%' "
            f"AND instrument_type = 'EQ' ORDER BY tradingsymbol LIMIT 10"
        )).mappings().all()
        summary["nifty_matches"] = [dict(r) for r in rows]

        # 7. Distinct instrument types count
        row = conn.execute(text(
            f"SELECT count(DISTINCT instrument_type) as cnt FROM {TABLE_NAME}"
        )).mappings().first()
        summary["distinct_types"] = row["cnt"]

        # 8. Distinct exchanges count
        row = conn.execute(text(
            f"SELECT count(DISTINCT exchange) as cnt FROM {TABLE_NAME}"
        )).mappings().first()
        summary["distinct_exchanges"] = row["cnt"]

        # 9. fetched_at freshness
        row = conn.execute(text(
            f"SELECT max(fetched_at) as last_fetch FROM {TABLE_NAME}"
        )).mappings().first()
        summary["last_fetch"] = str(row["last_fetch"])

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("  E2E: mStock Auth -> Fetch ALL Instruments -> PostgreSQL -> Validate")
    print("=" * 70)

    # -- Step 1: Login -------------------------------------------------------
    print("\n[1/6] Logging in with username/password...")
    login_resp = login()
    print(f"  Login: {login_resp.get('status')}")
    assert login_resp.get("status") == "success", f"Login failed: {login_resp}"
    print("  OK: Login successful")

    # -- Step 2: TOTP --------------------------------------------------------
    print("\n[2/6] Waiting for TOTP code...")
    code = get_auth_code()
    print(f"  TOTP received (length={len(code)})")

    # -- Step 3: Verify TOTP -------------------------------------------------
    print("\n[3/6] Verifying TOTP...")
    session = verify_totp(code)
    access_token = session["token"]
    print(f"  Token: {access_token[:20]}...{access_token[-10:]}")
    print("  OK: TOTP verified")

    # -- Step 4: Fetch instrument master -------------------------------------
    print("\n[4/6] Fetching ALL instruments from mStock scriptmaster...")
    base_url = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")
    api_key = os.getenv("MSTOCK_API_KEY", "").strip()
    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"token {api_key}:{access_token}",
    }

    resp = requests.get(
        f"{base_url}/openapi/typea/instruments/scriptmaster",
        headers=headers,
        timeout=60,
    )
    resp.raise_for_status()
    frame = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    total_fetched = len(frame)
    print(f"  Fetched {total_fetched:,} instruments")
    print(f"  Columns: {list(frame.columns)}")
    print(f"  Exchanges: {frame['exchange'].value_counts().to_dict()}")
    print(f"  Types: {frame['instrument_type'].value_counts().head(10).to_dict()}")

    # -- Step 5: Persist to PostgreSQL ---------------------------------------
    print(f"\n[5/6] Persisting {total_fetched:,} instruments to PostgreSQL...")
    engine = create_engine(DB_URL, echo=False)
    _ensure_table(engine)
    inserted = _bulk_upsert(engine, frame)
    print(f"  OK: Upserted {inserted:,} rows")

    # -- Step 6: Validate ----------------------------------------------------
    print(f"\n[6/6] Validating data in PostgreSQL...")
    summary = _validate(engine, total_fetched)
    engine.dispose()

    print(f"\n{'=' * 70}")
    print("  VALIDATION RESULTS")
    print(f"{'=' * 70}")
    print(f"\n  Total rows in DB:      {summary['total_rows']:,}")
    print(f"  Fetched from API:      {total_fetched:,}")
    print(f"  Last fetch:            {summary['last_fetch']}")
    print(f"  Distinct exchanges:    {summary['distinct_exchanges']}")
    print(f"  Distinct types:        {summary['distinct_types']}")

    print(f"\n  By Exchange:")
    for r in summary["by_exchange"]:
        print(f"    {r['exchange']:>8}: {r['cnt']:>8,}")

    print(f"\n  By Instrument Type (top 10):")
    for r in summary["by_type_top10"]:
        print(f"    {r['instrument_type']:>12}: {r['cnt']:>8,}")

    print(f"\n  By Segment (top 10):")
    for r in summary["by_segment_top10"]:
        print(f"    {r['segment']:>12}: {r['cnt']:>8,}")

    print(f"\n  Sample Equity Instruments:")
    for r in summary["sample_equity_5"]:
        print(f"    {r['instrument_token']:>8} | {r['tradingsymbol']:<20} | {r['name'][:40]:<40} | tick={r['tick_size']} lot={r['lot_size']}")

    print(f"\n  NIFTY Matches (equity):")
    for r in summary["nifty_matches"]:
        print(f"    {r['instrument_token']:>8} | {r['tradingsymbol']:<30} | {r['name'][:50]:<50} | {r['exchange']}/{r['segment']}")

    # -- Assertions ----------------------------------------------------------
    errors = []
    if summary["total_rows"] < total_fetched:
        errors.append(f"DB has {summary['total_rows']:,} rows but API returned {total_fetched:,}")
    if summary["total_rows"] != inserted:
        errors.append(f"Upserted {inserted:,} but DB has {summary['total_rows']:,}")
    if summary["distinct_exchanges"] < 1:
        errors.append("No exchanges found")

    print(f"\n{'-' * 70}")
    if errors:
        print("  [FAIL] VALIDATION FAILED:")
        for e in errors:
            print(f"     - {e}")
        sys.exit(1)
    else:
        print("  [PASS] ALL VALIDATIONS PASSED")
        print(f"     {summary['total_rows']:,} instruments persisted and verified in PostgreSQL")
    print(f"{'-' * 70}")


if __name__ == "__main__":
    main()
