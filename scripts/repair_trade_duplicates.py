"""One-off at-rest repair for B1: the duplicate rows ``trades`` already carries.

WHAT WENT WRONG (2026-10-30, measured on this deployment's Postgres)
--------------------------------------------------------------------
``LiveTradePersister`` remembered what it had written in an in-memory set, so
every restart restored a runner's closed-trades cache and flushed all of it
again with a fresh ``trade_id`` and ``created_at = today``:

    6,476 rows for 2,878 real round trips, -₹6,76,178.83 booked against a true
    -₹4,52,543.09 — ₹2,23,635.74 of phantom loss, worst group 16 copies.

The writer is fixed at source (revision 022 + ``trade_dedupe_key``), which stops
new copies. This script repairs the rows that are already stored.

WHAT IT DOES
------------
* Groups ``trades`` on the identity columns — the same tuple the guard uses —
  and keeps ONE row per group: the earliest ``created_at``, tie-broken by
  ``trade_id`` so the choice is reproducible rather than "whichever Postgres
  returned first". The survivor's money is left exactly as it was booked: this
  is a de-duplication, not a re-pricing, and B2 owns the fee disagreement.
* Deletes the rest, then backfills ``dedupe_key`` on every surviving row that
  has none — so the unique index becomes a real constraint over the whole
  history instead of only over future writes.

SAFETY
------
* dry-run is the default: prints the groups, the phantom rupees, and a preview;
  writes nothing;
* ``--execute`` snapshots the whole table into ``bak_trades_dupes_<stamp>``
  first and prints the rollback command;
* ``--rollback --bak-table <name>`` puts the deleted rows back and does not
  touch rows written since — a repair that ran after a week of trading must not
  become a restore to the snapshot date;
* refuses when the ``dedupe_key`` column is absent (apply 022 first), and
  refuses an ``--execute`` when today's snapshot already exists unless
  ``--force``, because a second pass over repaired rows is a no-op by design but
  the snapshot would be overwritten-proof rather than pre-repair.

Run with the web app stopped, so no persister adds rows mid-repair.

    PYTHONPATH=src python scripts/repair_trade_duplicates.py
    PYTHONPATH=src python scripts/repair_trade_duplicates.py --execute
    PYTHONPATH=src python scripts/repair_trade_duplicates.py --rollback \
        --bak-table bak_trades_dupes_20261031_1530
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, delete, inspect, text  # noqa: E402

from backtest.forward.trade_persistence import (  # noqa: E402
    DEDUPE_COLUMNS,
    trade_dedupe_key,
)
from backtest.timekeeping import from_stored_utc  # noqa: E402

load_dotenv(ROOT / ".env")

#: The survivor rule, spelled out so the dry-run can be argued with: the copy
#: that was written first is the one that stays.
_KEEP_ORDER = "earliest created_at, tie-broken by trade_id"
_COLUMNS = ", ".join(DEDUPE_COLUMNS)


def db_url() -> str:
    url = os.environ.get("FORWARD_TEST_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("no DB URL in the environment — copy .env from the main checkout")
    return url


def _as_values(row: dict) -> dict:
    """A stored row → the identity mapping :func:`trade_dedupe_key` expects.

    ``entry_time``/``exit_time`` come back naive from SQLite and aware from
    Postgres; both are this application's own stamps, so they go in through
    :func:`from_stored_utc` rather than being guessed at.
    """
    values = dict(row)
    for column in ("entry_time", "exit_time"):
        values[column] = from_stored_utc(values[column])
    return values


def _survivor_order(row: dict) -> tuple:
    """Sort key for ``created_at``, safe across both engines' readings.

    Postgres hands back an aware datetime and SQLite a string-shaped naive one,
    so comparing them against each other (or against a naive minimum for a NULL)
    raises. ISO text compares chronologically and puts a NULL first, which is the
    same rule as :data:`_KEEP_ORDER`.
    """
    return (str(row["created_at"] or ""), str(row["trade_id"]))


def load_groups(engine) -> tuple[list[dict], int]:
    """Every ``trades`` row bucketed by identity. Returns (groups, total rows)."""
    with engine.connect() as conn:
        rows = [
            dict(r._mapping)
            for r in conn.execute(
                text(
                    f"SELECT {_COLUMNS}, trade_id, created_at, gross_pnl, net_pnl FROM trades"
                )
            )
        ]
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[trade_dedupe_key(_as_values(row))].append(row)
    groups = [sorted(members, key=_survivor_order) for members in buckets.values()]
    return groups, len(rows)


def report(groups: list[dict], total: int) -> None:
    dupes = [g for g in groups if len(g) > 1]
    doomed = [row for group in dupes for row in group[1:]]
    money = lambda rows, col: sum(Decimal(str(r[col] or 0)) for r in rows)  # noqa: E731
    stats = {
        "rows": total,
        "distinct": len(groups),
        "duplicate_groups": len(dupes),
        "to_delete": len(doomed),
        "worst_group": max((len(g) for g in dupes), default=0),
        "phantom_gross": money(doomed, "gross_pnl"),
        "phantom_net": money(doomed, "net_pnl"),
        "kept_gross": money([g[0] for g in groups], "gross_pnl"),
        "kept_net": money([g[0] for g in groups], "net_pnl"),
    }
    print(f"  rows in trades        {stats['rows']:,}")
    print(f"  distinct round trips  {stats['distinct']:,}")
    print(f"  groups with copies    {stats['duplicate_groups']:,} (worst {stats['worst_group']}x)")
    print(f"  rows to delete        {stats['to_delete']:,}")
    print(f"  phantom P&L removed   gross ₹{stats['phantom_gross']:,.2f} "
          f"/ net ₹{stats['phantom_net']:,.2f}")
    print(f"  ledger after repair   gross ₹{stats['kept_gross']:,.2f} "
          f"/ net ₹{stats['kept_net']:,.2f}")
    for group in sorted(dupes, key=len, reverse=True)[:5]:
        first = group[0]
        print(
            f"    keep {first['trade_id'][:8]} ({first['created_at']}) "
            f"drop {len(group) - 1}: {first['symbol']} "
            f"{str(first['exit_time'])[:19]} ₹{Decimal(str(first['net_pnl'] or 0)):,.2f}"
        )
    return stats


def execute(engine, groups: list[dict], stamp: str, force: bool) -> None:
    from backtest.db.models import Trade as TradeRow

    bak = f"bak_trades_dupes_{stamp}"
    doomed = [row["trade_id"] for group in groups if len(group) > 1 for row in group[1:]]
    if not doomed:
        print("\n  no duplicate round trips — nothing to do.")
        return
    with engine.begin() as conn:
        if bak in inspect(conn).get_table_names() and not force:
            sys.exit(
                f"{bak} already exists — its rows are post-repair, not pre-repair. "
                "Pass --force to re-snapshot over it."
            )
        conn.execute(text(f"CREATE TABLE {bak} AS SELECT * FROM trades"))
        deleted = 0
        for chunk in (doomed[i : i + 500] for i in range(0, len(doomed), 500)):
            deleted += conn.execute(
                delete(TradeRow).where(TradeRow.trade_id.in_(chunk))
            ).rowcount
        keyed = 0
        for group in groups:
            survivor = group[0]
            keyed += conn.execute(
                text(
                    "UPDATE trades SET dedupe_key = :k "
                    "WHERE trade_id = :tid AND dedupe_key IS NULL"
                ),
                {"k": trade_dedupe_key(_as_values(survivor)), "tid": survivor["trade_id"]},
            ).rowcount
        remaining = conn.execute(text("SELECT COUNT(*) FROM trades")).scalar_one()
        print(f"\n  snapshot   {bak}")
        print(f"  deleted    {deleted:,} duplicate rows (planned {len(doomed):,})")
        print(f"  re-keyed   {keyed:,} survivors")
        print(f"  trades now {remaining:,} rows")
        print(
            f"\n  rollback:  PYTHONPATH=src python scripts/repair_trade_duplicates.py "
            f"--rollback --bak-table {bak}"
        )
        if deleted != len(doomed):
            print("  NOTE: planned and deleted differ — a writer added a row mid-run.")


def rollback(engine, bak: str) -> None:
    with engine.begin() as conn:
        names = inspect(conn).get_table_names()
        if bak not in names:
            sys.exit(f"{bak} is not a table in this database")
        restored = conn.execute(
            text(f"INSERT INTO trades SELECT * FROM {bak} "
                 f"WHERE trade_id NOT IN (SELECT trade_id FROM trades)")
        ).rowcount
        print(f"  restored {restored:,} rows from {bak} (rows written since are untouched)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true", help="actually write (default: dry-run)")
    parser.add_argument("--rollback", action="store_true", help="undo --execute from a snapshot")
    parser.add_argument("--bak-table", help="snapshot name for --rollback / --force check")
    parser.add_argument("--force", action="store_true", help="re-snapshot over an existing backup")
    args = parser.parse_args()

    engine = create_engine(db_url())
    cols = {c["name"] for c in inspect(engine).get_columns("trades")}
    if "dedupe_key" not in cols:
        sys.exit(
            "trades.dedupe_key is missing — apply migration 022 first "
            "(alembic upgrade head, or db/migrations/022_trades_dedupe_key.sql)"
        )
    print(f"database: {engine.url.render_as_string(hide_password=True)}")
    if args.rollback:
        if not args.bak_table:
            sys.exit("--rollback needs --bak-table")
        rollback(engine, args.bak_table)
        return 0

    groups, total = load_groups(engine)
    report(groups, total)
    if not args.execute:
        print("\n  dry-run — nothing written. Re-run with --execute.")
        return 0
    execute(engine, groups, datetime.now().strftime("%Y%m%d_%H%M"), args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
