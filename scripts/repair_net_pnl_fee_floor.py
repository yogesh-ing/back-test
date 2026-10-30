"""One-off at-rest repair for B2: ``trades`` rows whose ``net_pnl`` rebates the fee floor.

WHAT WENT WRONG (measured on this deployment's Postgres, 2026-10-30)
-------------------------------------------------------------------
The schema states the contract on the column itself
(`src/backtest/db/models.py:633`)::

    net_pnl = gross_pnl - commission_total - slippage_total

Ticket 1.2 put the ₹20-per-leg minimum brokerage into ``commission_total``,
but the rows written before the Ticket 2.2 writer floored ``net_pnl`` back
down again. So both money columns describe the same round trip and
contradict each other by exactly the floor:

    1,128 rows at net = gross - commission + ₹40 (two legs),
       1 row at net = gross - commission + ₹20 (one leg),
    ₹51,200.00 of realized loss the ledger never booked.

``commission_total`` is the honest column: the floor is charged where the
money actually leaves the book
(`src/backtest/options/paper_trading.py:1378` debits
``total_entry_cost + total_commission + fees_paid`` from ``available_cash``).
``net_pnl`` is the one that lied, so this script rewrites ``net_pnl`` and
never touches ``gross_pnl`` or ``commission_total``.

The writer half landed in Ticket 2.2 (``668ed96``) — ``StructurePosition.net_pnl``
is ``realized - commission - statutory`` and post-deploy closes already book the
contract exactly. This repairs only the rows that predate it.

WHAT IT DOES
------------
* Reads every ``trades`` row, computes the contract value from the row's own
  ``gross_pnl``/``commission_total``/``slippage_total``, and moves ``net_pnl``
  to that value where the two disagree.
* Does not invent costs. On the measured cohort ``commission_total`` is exactly
  the ₹20-per-leg floor, so the venue and statutory charges those books also
  paid never reached the table at all: the repaired ``net_pnl`` is the contract,
  not the full cost stack. Recovering the statutory half means re-pricing from
  the fills, which is writer work (Ticket 2.2 / B9), not a one-off UPDATE.
* Refuses ``--execute`` while B1's duplicate copies are still in the table:
  pricing the survivor and leaving seventeen rebate copies behind would move the
  day total twice. Run ``scripts/repair_trade_duplicates.py`` first. The dry-run
  measures with the duplicates in place, because the disagreement has to be
  counted before anyone can decide to repair it.

SAFETY
------
* dry-run is the default: buckets the disagreement by rupee delta and by cohort,
  prints what the day totals become, writes nothing;
* ``--execute`` snapshots the whole table into ``bak_trades_netpnl_<stamp>``
  first and prints the rollback command;
* each UPDATE is matched on the row's OLD ``net_pnl``/``gross_pnl``/
  ``commission_total`` too, so a row a writer changed between the read and the
  write is skipped rather than clobbered (the count of skipped rows is printed);
* ``--rollback --bak-table <name>`` restores ``net_pnl`` from the snapshot for
  rows that still exist and leaves rows written since alone;
* refuses when ``gross_pnl`` or ``net_pnl`` is missing from the schema, and
  re-reads the table after ``--execute`` to prove zero violations remain.

Run with the web app stopped, so no persister writes a close mid-repair.

    PYTHONPATH=src python scripts/repair_net_pnl_fee_floor.py
    PYTHONPATH=src python scripts/repair_net_pnl_fee_floor.py --execute
    PYTHONPATH=src python scripts/repair_net_pnl_fee_floor.py --rollback \
        --bak-table bak_trades_netpnl_20261031_1530
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import bindparam, create_engine, inspect, select, text, update  # noqa: E402

from backtest.forward.trade_persistence import trade_dedupe_key  # noqa: E402
from backtest.timekeeping import EXCHANGE_TZ, from_stored_utc  # noqa: E402

load_dotenv(ROOT / ".env")

#: The Ticket 1.2 per-leg minimum brokerage, so the dry-run can name the shape
#: of the disagreement instead of just its size.
LEG_FLOOR = Decimal("20")

_READ_COLUMNS = (
    "trade_id",
    "portfolio_id",
    "symbol",
    "direction",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "quantity",
    "gross_pnl",
    "net_pnl",
    "commission_total",
    "created_at",
)


def db_url() -> str:
    url = os.environ.get("FORWARD_TEST_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("no DB URL in the environment — copy .env from the main checkout")
    return url


def money(value) -> Decimal:
    """A stored Money column as a Decimal, with NULL read as zero.

    Postgres returns Decimal, SQLite returns the text this app wrote, and the
    server_default-zero on ``slippage_total`` can still be NULL on rows older
    than the column. All three are this application's own numbers.
    """
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def contract(row: dict, has_slippage: bool) -> Decimal:
    """What ``net_pnl`` must be for this row.

    ``slippage_total`` is part of the contract where the column exists. On the
    live schema it does (019+), but the repair is the same arithmetic either
    way, so a checkout that predates it still repairs correctly rather than
    half-fixing by the slippage.
    """
    net = money(row["gross_pnl"]) - money(row["commission_total"])
    if has_slippage:
        net -= money(row.get("slippage_total"))
    return net


def load_rows(engine, has_slippage: bool) -> list[dict]:
    columns = ", ".join(_READ_COLUMNS) + (", slippage_total" if has_slippage else "")
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(text(f"SELECT {columns} FROM trades"))]
    for row in rows:
        for column in ("entry_time", "exit_time"):
            row[column] = from_stored_utc(row[column])
    return rows


def duplicate_groups(rows: list[dict]) -> int:
    """How many round trips are still stored more than once (B1's problem)."""
    buckets: Counter = Counter(trade_dedupe_key(row) for row in rows)
    return sum(1 for count in buckets.values() if count > 1)


def _survivor_order(row: dict) -> tuple:
    """B1's keep-rule, copied so this script can price the same row it keeps.

    ISO text compares chronologically across both engines and puts a NULL first,
    which is "earliest ``created_at``, tie-broken by ``trade_id``".
    """
    return (str(row["created_at"] or ""), str(row["trade_id"]))


def survivor_rows(rows: list[dict]) -> list[dict]:
    """The one row per round trip that :mod:`repair_trade_duplicates` keeps."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[trade_dedupe_key(row)].append(row)
    return [sorted(group, key=_survivor_order)[0] for group in buckets.values()]


def violations(rows: list[dict], has_slippage: bool) -> list[tuple[dict, Decimal, Decimal]]:
    """(row, stored net, contract net) for every row that breaks the contract."""
    found = []
    for row in rows:
        want = contract(row, has_slippage)
        if money(row["net_pnl"]) != want:
            found.append((row, money(row["net_pnl"]), want))
    return found


def _ist_day(value) -> str:
    """The session date a stamp belongs to, for cohort bucketing."""
    if value is None:
        return "?"
    try:
        return str(value.astimezone(EXCHANGE_TZ).date())
    except (AttributeError, ValueError, OSError):
        return str(value)[:10]


def remaining_violations(conn, has_slippage: bool) -> int:
    """Count rows that still break the contract, on the caller's own connection.

    Read inside the write transaction, because a second connection would either
    see the pre-commit state (PostgreSQL) or fail with "database is locked"
    (SQLite).
    """
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM trades WHERE net_pnl <> gross_pnl - commission_total"
                + (" - COALESCE(slippage_total, 0)" if has_slippage else "")
            )
        ).scalar_one()
    )


def report(
    rows: list[dict],
    breaks: list[tuple[dict, Decimal, Decimal]],
    survivor_breaks: list[tuple[dict, Decimal, Decimal]],
) -> None:
    print(f"  rows in trades        {len(rows):,}")
    print(f"  contract violations   {len(breaks):,}")
    if not breaks:
        print("  nothing to repair — every row already books net = gross - commission - slippage.")
        return

    deltas = Counter(money(row["net_pnl"]) - want for row, _, want in breaks)
    print("  disagreement by size (stored net − contract net):")
    for delta, count in sorted(deltas.items(), key=lambda kv: -abs(kv[0])):
        shape = ""
        if delta > 0 and delta % LEG_FLOOR == 0:
            shape = f"  (= {int(delta // LEG_FLOOR)} legs of the ₹{LEG_FLOOR} floor)"
        print(f"    ₹{delta:>12,.2f} x {count:>6,} rows{shape}")

    over = sum(money(r["net_pnl"]) - w for r, _, w in breaks)
    print(f"  net_pnl is overstated by ₹{over:,.2f} in total "
          f"(loss the ledger never booked)")
    survivor_over = sum(money(r["net_pnl"]) - w for r, _, w in survivor_breaks)
    print(f"  on the {len(survivor_breaks):,} rows B1's dedupe keeps: "
          f"₹{survivor_over:,.2f} — that is what --execute moves")

    # Which of the two columns is the liar decides whether this repair lowers
    # net or raises commission_total. If ``net == gross`` then the fee never
    # reached the P&L at all and ``commission_total`` — the number the book was
    # actually debited — is the honest one.
    published_gross = sum(1 for r, _, _ in breaks if money(r["net_pnl"]) == money(r["gross_pnl"]))
    charges = Counter(money(r["commission_total"]) for r, _, _ in breaks)
    print(f"  stored net == gross on {published_gross:,} of them — that flow published "
          "gross realized as net")
    print("  commission_total on the violating rows: "
          + ", ".join(f"₹{value:,.2f} x {count:,}"
                      for value, count in sorted(charges.items(), key=lambda kv: -kv[1])[:6]))

    # Where the whole charge is the floor, no venue/statutory fee ever reached the
    # row either. Worth saying out loud before a money column is rewritten: this
    # repair enforces the stored contract, it cannot invent a fee the writer
    # never recorded.
    floor_only = sum(
        1
        for r, stored, want in breaks
        if money(r["commission_total"]) == stored - want
    )
    if floor_only:
        print(f"  on {floor_only:,} of them commission_total IS the whole disagreement, so no "
              "statutory fee was ever recorded on the row either — the repaired net is "
              "the contract, not the full cost stack")

    by_day: dict[str, list] = defaultdict(list)
    for row, stored, want in breaks:
        by_day[_ist_day(row["exit_time"])].append((row, stored, want))
    print("  cohort by exit session (IST):")
    for day in sorted(by_day):
        group = by_day[day]
        print(
            f"    {day}  {len(group):>5,} rows  "
            f"stored ₹{sum(s for _, s, _ in group):>14,.2f} → "
            f"contract ₹{sum(w for _, _, w in group):>14,.2f}"
        )

    options = [item for item in breaks if " " in str(item[0]["symbol"])]
    print(
        f"  split: {len(options):,} option structures / "
        f"{len(breaks) - len(options):,} equity round trips"
    )

    odd = [
        (row, stored, want)
        for row, stored, want in breaks
        if not (stored > want and (stored - want) % LEG_FLOOR == 0)
    ]
    if odd:
        print(f"  outside the floor shape — {len(odd)} rows, listed below")
        for row, stored, want in odd[:10]:
            print(
                f"    {str(row['trade_id'])[:8]} {row['symbol']!r} "
                f"gross ₹{money(row['gross_pnl']):,.2f} "
                f"comm ₹{money(row['commission_total']):,.2f} "
                f"net ₹{stored:,.2f} → contract ₹{want:,.2f}"
            )
    print("  sample of the repair:")
    for row, stored, want in breaks[:5]:
        print(
            f"    keep id {str(row['trade_id'])[:8]} exit {_ist_day(row['exit_time'])} "
            f"{row['symbol']!r}: net ₹{stored:,.2f} → ₹{want:,.2f}"
        )


def execute(engine, breaks: list[tuple[dict, Decimal, Decimal]], stamp: str, force: bool,
            has_slippage: bool) -> None:
    if not breaks:
        print("\n  no violations — nothing to do.")
        return

    from backtest.db.models import Trade as TradeRow

    #: trade_id → (the ``net_pnl`` this plan was built from, its contract value)
    plan = {str(row["trade_id"]): (stored, want) for row, stored, want in breaks}
    bak = f"bak_trades_netpnl_{stamp}"

    # A typed Core statement rather than text(): SQLite's driver refuses a
    # Decimal parameter outright ("type 'decimal.Decimal' is not supported"),
    # and the column's own Numeric type is what makes the same bind exact on
    # both engines.
    write = (
        update(TradeRow)
        .where(TradeRow.trade_id == bindparam("tid"))
        .values(net_pnl=bindparam("new"))
    )

    with engine.begin() as conn:
        if bak in inspect(conn).get_table_names() and not force:
            sys.exit(
                f"{bak} already exists — its rows are post-repair, not pre-repair. "
                "Pass --force to re-snapshot over it."
            )
        conn.execute(text(f"CREATE TABLE {bak} AS SELECT * FROM trades"))

        updated = 0
        skipped = 0
        ids = list(plan)
        for chunk in (ids[i : i + 500] for i in range(0, len(ids), 500)):
            # Re-read the money inside the write transaction. A row that no
            # longer holds the number this plan was built from was touched by a
            # writer since the dry-run read, so it is skipped, not clobbered.
            live = {
                str(row["trade_id"]): money(row["net_pnl"])
                for row in conn.execute(
                    select(TradeRow.trade_id, TradeRow.net_pnl).where(
                        TradeRow.trade_id.in_(chunk)
                    )
                ).mappings()
            }
            params = [
                {"tid": tid, "new": plan[tid][1]}
                for tid in chunk
                if live.get(tid) == plan[tid][0]
            ]
            if params:
                updated += conn.execute(write, params).rowcount
            skipped += len(chunk) - len(params)

        remaining = remaining_violations(conn, has_slippage)
        print(f"\n  snapshot   {bak}")
        print(f"  re-priced  {updated:,} rows (planned {len(plan):,})")
        print(f"  skipped    {skipped:,} — net_pnl no longer the number this plan read")
        print(f"  violations {remaining} after repair")
        print(
            f"\n  rollback:  PYTHONPATH=src python scripts/repair_net_pnl_fee_floor.py "
            f"--rollback --bak-table {bak}"
        )
        if remaining:
            sys.exit("rows still break the contract — re-run the dry-run to see them")


def rollback(engine, bak: str, has_slippage: bool) -> None:
    """Put the pre-repair ``net_pnl`` back, for the rows this repair could have touched.

    A correlated subselect rather than ``UPDATE ... FROM`` because that form is
    PostgreSQL-only and this script has to be rollback-able on the SQLite
    checkouts too. Rows written after the snapshot are not in it, so they stay.
    """
    with engine.begin() as conn:
        if bak not in inspect(conn).get_table_names():
            sys.exit(f"{bak} is not a table in this database")
        restored = conn.execute(
            text(
                f"UPDATE trades SET net_pnl = "
                f"(SELECT net_pnl FROM {bak} WHERE {bak}.trade_id = trades.trade_id) "
                f"WHERE trade_id IN (SELECT trade_id FROM {bak})"
            )
        ).rowcount
        remaining = remaining_violations(conn, has_slippage)
        print(f"  restored net_pnl on {restored:,} rows from {bak}")
        print(f"  contract violations now {remaining} (the pre-repair number is expected)")


def main() -> int:
    try:  # Windows consoles default to cp1252 and choke on the rupee sign
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 — a redirected stream simply keeps its encoding
        pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true", help="actually write (default: dry-run)")
    parser.add_argument("--rollback", action="store_true", help="undo --execute from a snapshot")
    parser.add_argument("--bak-table", help="snapshot name for --rollback / --force check")
    parser.add_argument("--force", action="store_true", help="re-snapshot over an existing backup")
    args = parser.parse_args()

    engine = create_engine(db_url())
    cols = {c["name"] for c in inspect(engine).get_columns("trades")}
    missing = {"gross_pnl", "net_pnl", "commission_total"} - cols
    if missing:
        sys.exit(f"trades is missing {sorted(missing)} — wrong schema for this repair")
    has_slippage = "slippage_total" in cols
    print(f"database: {engine.url.render_as_string(hide_password=True)}")
    print(f"slippage_total present: {has_slippage}")

    rows = load_rows(engine, has_slippage)
    if args.rollback:
        if not args.bak_table:
            sys.exit("--rollback needs --bak-table")
        rollback(engine, args.bak_table, has_slippage)
        return 0

    dupes = duplicate_groups(rows)
    if dupes:
        print(
            f"  NOTE: {dupes} round trips are stored as duplicate copies (B1). "
            "The dry-run measures them anyway; --execute refuses until "
            "scripts/repair_trade_duplicates.py has run."
        )
        if args.execute:
            sys.exit(
                "re-pricing survivors while the copies remain would change the day "
                "total twice — run scripts/repair_trade_duplicates.py first (B1)."
            )

    breaks = violations(rows, has_slippage)
    survivors = violations(survivor_rows(rows), has_slippage)
    report(rows, breaks, survivors)
    if not args.execute:
        print("\n  dry-run — nothing written. Re-run with --execute.")
        return 0
    execute(engine, breaks, datetime.now().strftime("%Y%m%d_%H%M"), args.force, has_slippage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
