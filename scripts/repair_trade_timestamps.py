"""One-off repair: put mislabelled ``trades`` timestamps on one clock.

WHAT WENT WRONG (2026-10-27, measured on this deployment's Postgres)
--------------------------------------------------------------------
The trade writers stamped ``datetime.utcnow()``, stripped the offset on the way
in, and handed the naive value to ``TIMESTAMPTZ`` columns under an
``Asia/Calcutta`` session timezone. Postgres read every naive value as IST, so
each one landed 5h30 early. Where the *other* side of the row came from a bar
or settlement clock (already IST wall time) the two halves ended up 5h30 apart:
133 option rows recorded a 330-minute hold for trades that lasted a minute, and
1755 of 1831 equity entries displayed pre-open.

The writers are fixed in :mod:`backtest.timekeeping` and its call sites. This
script repairs the rows already stored under the broken convention.

CLASSIFIER (the two rules come from that same measurement)
----------------------------------------------------------
``created_at`` is a server default — the one honest clock on the row — and a
pre-fix exit stamped in UTC always sits ~5h30 (or more, for batched flushes)
before it. So:

* ``entry`` — exit is a whole minute AND ``created_at - exit_time`` < 2 min:
  the exit came from a bar/settlement clock and is already correct, so only the
  UTC-stamped entry shifts. 133 rows, all option runners.
* ``both``  — everything else: both halves were stamped from the UTC wall
  clock, so both shift and the recorded duration is preserved. 2026 rows, all
  equity runners.
* ``skip``  — rows created at or after ``--before``: the writers are fixed
  there, so those rows are honest and must never be shifted.

SAFETY
------
* dry-run is the default — it prints the plan, a preview and the residual check,
  and writes nothing;
* ``--execute`` snapshots the whole table into ``bak_trades_ts_<stamp>`` first
  and prints the rollback command;
* ``--rollback --bak-table <name>`` restores entry/exit/hold from that snapshot;
* a second ``--execute`` would double-shift, so it refuses when today's snapshot
  already exists unless ``--force``.

Run with the web app stopped (or at least with no runner persisting), so no
pre-fix writer adds rows mid-migration.

    PYTHONPATH=src python scripts/repair_trade_timestamps.py
    PYTHONPATH=src python scripts/repair_trade_timestamps.py --execute \
        --before "2026-10-28 09:00"
    PYTHONPATH=src python scripts/repair_trade_timestamps.py --rollback \
        --bak-table bak_trades_ts_20261028
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from backtest.timekeeping import EXCHANGE_TZ, ensure_utc  # noqa: E402

load_dotenv(ROOT / ".env")

#: A naive UTC value read as IST displays 5h30 before its true instant.
SHIFT = "interval '5 hours 30 minutes'"
#: The NSE session plus the settlement tail — a repaired stamp must land here.
SESSION_START = "09:10"
SESSION_END = "15:45"

#: Classify every pre-cutover row. ``entry`` = the exit is already honest.
PLAN_SQL = """
    SELECT t.trade_id,
           t.symbol,
           t.strategy_name,
           t.created_at,
           t.entry_time,
           t.exit_time,
           t.holding_period_minutes AS hold,
           CASE
             WHEN EXTRACT(SECOND FROM t.exit_time) = 0
              AND EXTRACT(MICROSECONDS FROM t.exit_time) = 0
              AND t.created_at - t.exit_time BETWEEN interval '0 minutes'
                                                 AND interval '2 minutes'
             THEN 'entry'
             ELSE 'both'
           END AS fix
    FROM trades t
    WHERE t.created_at < :before
      AND t.entry_time IS NOT NULL
      AND t.exit_time IS NOT NULL
"""

#: A shifted entry must not overtake an exit it is meant to precede.
GUARD = """
    SELECT count(*)
    FROM trades t JOIN repair r USING (trade_id)
    WHERE r.fix = 'entry' AND t.exit_time < t.entry_time + {SHIFT}
""".format(SHIFT=SHIFT)

#: The repair itself. ``both`` shifts the exit too, so the duration is intact.
APPLY_SQL = f"""
    UPDATE trades t
    SET entry_time = t.entry_time + {SHIFT},
        exit_time  = CASE WHEN r.fix = 'both' THEN t.exit_time + {SHIFT}
                          ELSE t.exit_time END,
        holding_period_minutes = floor(EXTRACT(EPOCH FROM (
            (CASE WHEN r.fix = 'both' THEN t.exit_time + {SHIFT} ELSE t.exit_time END)
            - (t.entry_time + {SHIFT}))) / 60)::int
    FROM repair r
    WHERE r.trade_id = t.trade_id
      AND (CASE WHEN r.fix = 'both' THEN t.exit_time + {SHIFT} ELSE t.exit_time END)
          >= t.entry_time + {SHIFT}
"""

PREVIEW_SQL = f"""
    WITH p AS (
        SELECT r.symbol, r.strategy_name, r.fix, r.hold,
               (r.entry_time + {SHIFT})                                AS entry_new,
               (CASE WHEN r.fix = 'both' THEN r.exit_time + {SHIFT}
                     ELSE r.exit_time END)                             AS exit_new,
               r.entry_time, r.exit_time
        FROM repair r
        ORDER BY r.exit_time DESC
        LIMIT 8
    )
    SELECT symbol, strategy_name, fix, hold AS hold_was,
           floor(EXTRACT(EPOCH FROM (exit_new - entry_new)) / 60)::int AS hold_new,
           to_char(entry_time AT TIME ZONE 'Asia/Calcutta', 'MM-DD HH24:MI') AS entry_was,
           to_char(entry_new  AT TIME ZONE 'Asia/Calcutta', 'MM-DD HH24:MI') AS entry_new,
           to_char(exit_time  AT TIME ZONE 'Asia/Calcutta', 'MM-DD HH24:MI') AS exit_was,
           to_char(exit_new   AT TIME ZONE 'Asia/Calcutta', 'MM-DD HH24:MI') AS exit_new
    FROM p
"""

TRADES_COLS = ("trade_id, symbol, strategy_name, entry_time, exit_time, "
               "holding_period_minutes, created_at")


def db_url() -> str:
    url = os.environ.get("FORWARD_TEST_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("no DB URL in the environment — copy .env from the main checkout")
    return url


def build_plan(conn, before_iso: str) -> None:
    conn.execute(text("DROP TABLE IF EXISTS repair"))
    conn.execute(
        text(f"CREATE TEMP TABLE repair ON COMMIT DROP AS {PLAN_SQL}"),
        {"before": before_iso},
    )


def print_plan(conn, before_iso: str) -> int:
    counts = conn.execute(text("""
        SELECT fix, count(*) AS n FROM repair GROUP BY fix
    """)).all()
    by_fix = dict(counts)
    total = conn.execute(text("SELECT count(*) FROM trades")).scalar()
    touched = sum(by_fix.values())
    print(f"cutoff       : rows created before {before_iso}")
    print(f"plan         : {touched} of {total} row(s)  "
          f"(shift entry only: {by_fix.get('entry', 0)}, "
          f"shift both: {by_fix.get('both', 0)})")
    print(f"untouched    : {total - touched} row(s) at/after the cutoff")
    blocked = conn.execute(text(GUARD)).scalar()
    if blocked:
        print(f"skipped      : {blocked} row(s) where the shifted entry would pass its "
              "exit — left alone, review them")
    print("\npreview (IST wall clock):")
    for r in conn.execute(text(PREVIEW_SQL)).mappings():
        print(f"  {r['fix']:<6} {r['strategy_name']:<24} {r['symbol']:<22} "
              f"{r['entry_was']}->{r['entry_new']}  {r['exit_was']}->{r['exit_new']}  "
              f"hold {r['hold_was']}->{r['hold_new']} min")
    return touched


def apply_repair(conn, bak: str) -> None:
    conn.execute(text(f"CREATE TABLE {bak} AS "
                      f"SELECT {TRADES_COLS}, now() AS snapshotted_at FROM trades"))
    n = conn.execute(text(f"SELECT count(*) FROM {bak}")).scalar()
    print(f"snapshot     : {bak} ({n} rows)")
    updated = conn.execute(text(APPLY_SQL)).rowcount
    conn.commit()
    print(f"updated      : {updated} row(s)")
    print(f"rollback     : PYTHONPATH=src python {Path(__file__).name} "
          f"--rollback --bak-table {bak}")


def rollback(conn, bak: str) -> None:
    n = conn.execute(
        text(f"""
            UPDATE trades t
            SET entry_time = b.entry_time,
                exit_time  = b.exit_time,
                holding_period_minutes = b.holding_period_minutes
            FROM {bak} b WHERE b.trade_id = t.trade_id
        """)
    ).rowcount
    conn.commit()
    print(f"restored {n} row(s) from {bak}")


def verify(conn, label: str = "after repair") -> None:
    print(f"\n=== trades, as a trader reads them (IST) — {label} ===")
    for r in conn.execute(text(f"""
        SELECT count(*) AS rows_,
               count(*) FILTER (WHERE (entry_time AT TIME ZONE 'Asia/Calcutta')::time
                                    NOT BETWEEN '{SESSION_START}' AND '{SESSION_END}')
                     AS entry_outside,
               count(*) FILTER (WHERE (exit_time AT TIME ZONE 'Asia/Calcutta')::time
                                    NOT BETWEEN '{SESSION_START}' AND '{SESSION_END}')
                     AS exit_outside,
               count(*) FILTER (WHERE exit_time < entry_time) AS reversed,
               count(*) FILTER (WHERE holding_period_minutes BETWEEN 325 AND 335)
                     AS hold_330,
               max(holding_period_minutes) AS hold_max
        FROM trades
    """)).mappings():
        print(f"trades       : {r['rows_']}  entry_outside={r['entry_outside']} "
              f"exit_outside={r['exit_outside']} reversed={r['reversed']} "
              f"hold_330={r['hold_330']} hold_max={r['hold_max']}")
    print("newest five rows:")
    for r in conn.execute(text("""
        SELECT symbol, strategy_name,
               to_char(entry_time AT TIME ZONE 'Asia/Calcutta',
                       'YYYY-MM-DD HH24:MI:SS') AS entry_ist,
               to_char(exit_time AT TIME ZONE 'Asia/Calcutta',
                       'YYYY-MM-DD HH24:MI:SS') AS exit_ist,
               holding_period_minutes AS hold
        FROM trades ORDER BY exit_time DESC LIMIT 5
    """)).mappings():
        print(f"  {r['strategy_name']:<24} {r['symbol']:<22} "
              f"{r['entry_ist']} -> {r['exit_ist']}  {r['hold']} min")


def resolve_before(raw: str | None) -> str:
    """``--before`` is exchange wall time; the column is an instant."""
    if raw is None:
        return ensure_utc(datetime.now(EXCHANGE_TZ)).isoformat()
    naive = datetime.fromisoformat(raw)
    return ensure_utc(naive.replace(tzinfo=EXCHANGE_TZ)).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true",
                    help="write the repair (default is dry-run)")
    ap.add_argument("--before",
                    help="cutover as IST wall time (YYYY-MM-DD HH:MM) — only rows "
                         "created before it are touched. Required with --execute; "
                         "pass the moment the fixed writers went live.")
    ap.add_argument("--bak-table",
                    help="snapshot table name (default bak_trades_ts_<today>)")
    ap.add_argument("--rollback", action="store_true",
                    help="restore from --bak-table instead of repairing")
    ap.add_argument("--force", action="store_true",
                    help="allow --execute when today's snapshot already exists")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d")
    bak = args.bak_table or f"bak_trades_ts_{stamp}"
    engine = create_engine(db_url())

    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}

        if args.rollback:
            if not args.bak_table:
                sys.exit("--rollback needs --bak-table <name>")
            if args.bak_table not in tables:
                sys.exit(f"{args.bak_table} is not a table in this database")
            rollback(conn, args.bak_table)
            verify(conn, "after rollback")
            return

        if args.execute and not args.before:
            sys.exit("--execute requires --before <IST cutover> so post-fix rows "
                     "are never shifted twice")
        if args.execute and bak in tables and not args.force:
            sys.exit(f"{bak} already exists — reuse it with --rollback, or pass "
                     "--force after checking it")

        before_iso = resolve_before(args.before)
        build_plan(conn, before_iso)
        touched = print_plan(conn, before_iso)
        if not touched:
            print("nothing to repair")
            return
        if not args.execute:
            print("\ndry run — nothing written. Re-run with --execute to apply.")
            verify(conn, "current state, unrepaired")
            return
        apply_repair(conn, bak)
        verify(conn)


if __name__ == "__main__":
    main()
