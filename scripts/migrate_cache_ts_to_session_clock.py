"""One-off repair: put mislabelled market_data_cache rows on the session clock.

The fetch writers bound a naive UTC wall clock into ``market_data_cache.ts``
(timestamptz, server tz Asia/Calcutta), so an NSE 1-min bar came back reading
03:45+05:30 when it was really 09:15 IST. The writers are fixed in
``backtest.data.base.bar_timestamp``; this script repairs the rows already
stored under the broken convention.

Classification is per (symbol, exchange, timeframe, IST calendar day), driven
by the day's first/last wall clock:

* ``early``  — first bar before 09:15 and last bar at or before 10:30: the
  day was written under the naive-UTC convention. Shift every row +5h30.
* ``mixed``  — first bar before 09:15 but bars past 10:30: two conventions
  collide inside one day. NEVER touched; reported for manual review.
* ``honest`` — starts at or after 09:15: left alone.

Safety:
* dry-run (default) prints the plan and a collision check, writes nothing;
* ``--execute`` first snapshots ``bak_cache_ts.<date>`` as (data_id, ts) of
  every row it is about to shift, then updates symbol-by-symbol, committing
  per symbol with before/after row-count verification;
* rollback: ``--rollback`` restores ts for those data_ids from the backup.

Run while the market is closed and no fetch is active. Needs a free Postgres
slot (the Flask server hoards the pool — see project notes).

    PYTHONPATH=src python scripts/migrate_cache_ts_to_session_clock.py            # dry run
    PYTHONPATH=src python scripts/migrate_cache_ts_to_session_clock.py --execute  # repair
    PYTHONPATH=src python scripts/migrate_cache_ts_to_session_clock.py --rollback --bak-table bak_cache_ts_20261005
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

load_dotenv()

SHIFT = "interval '5 hours 30 minutes'"

DAY_KINDS = text(
    """
    WITH d AS (
        SELECT symbol, exchange, timeframe,
               (ts AT TIME ZONE 'Asia/Calcutta')::date  AS ist_day,
               MIN((ts AT TIME ZONE 'Asia/Calcutta')::time) AS first_wall,
               MAX((ts AT TIME ZONE 'Asia/Calcutta')::time) AS last_wall,
               COUNT(*)                                   AS bars
        FROM market_data_cache
        GROUP BY 1, 2, 3, 4
    )
    SELECT CASE
             WHEN first_wall < '09:15' AND last_wall <= '10:30' THEN 'early'
             WHEN first_wall < '09:15'                          THEN 'mixed'
             ELSE 'honest'
           END                       AS kind,
           COUNT(*)                  AS days,
           COALESCE(SUM(bars), 0)    AS rows_
    FROM d GROUP BY 1 ORDER BY 2 DESC
    """
)

# (symbol, day) groups that hold BOTH early-cluster bars and honest-looking
# bars past 10:30 inside the same wall-clock day — the only way a shifted row
# can collide with an existing one.
MIXED_DAYS = text(
    """
    WITH d AS (
        SELECT symbol, exchange, timeframe,
               (ts AT TIME ZONE 'Asia/Calcutta')::date AS ist_day,
               MIN((ts AT TIME ZONE 'Asia/Calcutta')::time) AS first_wall,
               MAX((ts AT TIME ZONE 'Asia/Calcutta')::time) AS last_wall
        FROM market_data_cache
        GROUP BY 1, 2, 3, 4
    )
    SELECT symbol, exchange, timeframe, ist_day, first_wall, last_wall
    FROM d
    WHERE first_wall < '09:15' AND last_wall > '10:30'
    ORDER BY symbol, ist_day
    """
)

def db_url() -> str:
    url = os.environ.get("FORWARD_TEST_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("no DB URL in the environment — copy .env from the main checkout")
    return url


EARLY_CTE = """
    WITH early AS (
        SELECT (ts AT TIME ZONE 'Asia/Calcutta')::date AS ist_day
        FROM market_data_cache
        WHERE symbol = :s
        GROUP BY 1
        HAVING MIN((ts AT TIME ZONE 'Asia/Calcutta')::time) < '09:15'
           AND MAX((ts AT TIME ZONE 'Asia/Calcutta')::time) <= '10:30'
    )
"""

CACHE_COLS = ("data_id, symbol, exchange, timeframe, ts, open, high, low, "
              "close, volume, bid, ask, source, ingested_at")


def shift_symbol(conn, symbol: str, bak: str) -> dict:
    """Shift one symbol's early-classified days. Returns counts.

    ``market_data_cache`` is a TimescaleDB hypertable partitioned on ``ts``,
    so an UPDATE across chunk boundaries is rejected — rows are moved with a
    delete + re-insert inside one per-symbol transaction.
    """
    conn.execute(text("SAVEPOINT shift"))
    conn.execute(
        text("DROP TABLE IF EXISTS shift_rows"),
    )
    conn.execute(
        text(
            "CREATE TEMP TABLE shift_rows ON COMMIT DROP AS "
            + EARLY_CTE
            + f"""SELECT m.{CACHE_COLS.replace(', ', ', m.')}
                  FROM market_data_cache m
                  JOIN early e ON e.ist_day = (m.ts AT TIME ZONE 'Asia/Calcutta')::date
                  WHERE m.symbol = :s"""
        ),
        {"s": symbol},
    )
    rows = conn.execute(text("SELECT count(*) FROM shift_rows")).scalar()
    if not rows:
        conn.execute(text("RELEASE SAVEPOINT shift"))
        return {"symbol": symbol, "rows": 0}

    conn.execute(text(f"INSERT INTO {bak} ({CACHE_COLS}) SELECT {CACHE_COLS} FROM shift_rows"))
    conn.execute(
        text(
            "DELETE FROM market_data_cache m USING shift_rows s "
            "WHERE s.data_id = m.data_id"
        )
    )
    conn.execute(
        text(
            f"INSERT INTO market_data_cache ({CACHE_COLS}) "
            f"SELECT data_id, symbol, exchange, timeframe, ts + {SHIFT}, "
            "open, high, low, close, volume, bid, ask, source, ingested_at "
            "FROM shift_rows"
        )
    )
    still = conn.execute(
        text(
            """SELECT count(*) FROM market_data_cache
               WHERE symbol = :s
                 AND (ts AT TIME ZONE 'Asia/Calcutta')::time < '09:15'"""
        ),
        {"s": symbol},
    ).scalar()
    if still:
        # Should be impossible: early days were the only pre-09:15 rows of this symbol.
        raise RuntimeError(f"{symbol}: {still} pre-open rows left after shift; aborting")
    return {"symbol": symbol, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually write")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--bak-table", help="backup table for --rollback (bak_cache_ts_YYYYMMDD)")
    ap.add_argument("--symbol", help="restrict to one symbol (operator escape hatch)")
    args = ap.parse_args()

    engine = create_engine(db_url())
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = '10min'"))

        if args.rollback:
            if not args.bak_table or not args.bak_table.replace("_", "a").isalnum():
                sys.exit("--rollback requires a valid --bak-table")
            # Undo = drop the shifted copies, re-insert the originals. Hypertable,
            # so no UPDATE of ts here either.
            conn.execute(
                text(
                    f"DELETE FROM market_data_cache m USING {args.bak_table} b "
                    f"WHERE b.data_id = m.data_id "
                    f"AND m.ts = b.ts + {SHIFT} AND m.symbol = b.symbol"
                )
            )
            conn.execute(
                text(
                    f"INSERT INTO market_data_cache ({CACHE_COLS}) "
                    f"SELECT {CACHE_COLS} FROM {args.bak_table} b "
                    "WHERE NOT EXISTS (SELECT 1 FROM market_data_cache m "
                    "WHERE m.data_id = b.data_id AND m.ts = b.ts)"
                )
            )
            conn.commit()
            print("rollback complete from", args.bak_table)
            return 0

        print("=== day classification (whole table) ===")
        for r in conn.execute(DAY_KINDS):
            print(f"  {r.kind:<8} days={r.days:<8} rows={r.rows_}")
        mixed = conn.execute(MIXED_DAYS).fetchall()
        if mixed:
            print(f"=== MIXED days ({len(mixed)}) — SKIPPED, manual review ===")
            for r in mixed:
                print(" ", r.symbol, r.ist_day, r.first_wall, "→", r.last_wall)

        symbols = [
            r.symbol
            for r in conn.execute(
                text(
                    """
                    SELECT symbol FROM market_data_cache
                    GROUP BY symbol, (ts AT TIME ZONE 'Asia/Calcutta')::date
                    HAVING MIN((ts AT TIME ZONE 'Asia/Calcutta')::time) < '09:15'
                       AND MAX((ts AT TIME ZONE 'Asia/Calcutta')::time) <= '10:30'
                    """
                )
            ).fetchall()
        ]
        symbols = sorted(set(symbols))
        if args.symbol:
            symbols = [s for s in symbols if s == args.symbol]
        total = conn.execute(
            text(
                """
                SELECT count(*) FROM market_data_cache m
                WHERE (m.ts AT TIME ZONE 'Asia/Calcutta')::time < '09:15'
                """
            )
        ).scalar()
        print(f"=== would shift ~{total} pre-09:15 rows across {len(symbols)} symbols ===")
        if not args.execute:
            print("dry run — nothing written. Re-run with --execute.")
            return 0

        bak = "bak_cache_ts_" + conn.execute(text("SELECT to_char(now(), 'YYYYMMDD_HH24MISS')")).scalar()
        conn.execute(
            text(f"CREATE TABLE {bak} AS SELECT * FROM market_data_cache WHERE false")
        )
        conn.execute(text(f"ALTER TABLE {bak} ADD PRIMARY KEY (data_id, ts)"))
        print(f"backup table: {bak}")

        shifted_total = 0
        for i, sym in enumerate(symbols, 1):
            res = shift_symbol(conn, sym, bak)
            if res["rows"]:
                conn.commit()
                shifted_total += res["rows"]
            print(f"  [{i}/{len(symbols)}] {sym}: shifted {res.get('rows', 0)}", flush=True)

        # Post-check: no pre-09:15 wall clocks left except the skipped MIXED days.
        left = conn.execute(
            text(
                """
                SELECT count(*) FROM market_data_cache
                WHERE (ts AT TIME ZONE 'Asia/Calcutta')::time < '09:15'
                """
            )
        ).scalar()
        print(f"done. shifted {shifted_total} rows; {left} pre-09:15 rows remain (expect only MIXED).")
        print(f"rollback: python scripts/migrate_cache_ts_to_session_clock.py --rollback --bak-table {bak}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
