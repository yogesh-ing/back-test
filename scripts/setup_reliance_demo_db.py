#!/usr/bin/env python3
"""Build a runnable database from the checked-in RELIANCE export.

One command, because the alternative is four and they get out of order:

    PYTHONPATH=src python scripts/setup_reliance_demo_db.py

What it does:

1. creates (or recreates) ``tools/out/reliance.db`` from every
   ``db/migrations/*.sqlite.sql`` file, in order;
2. ingests ``tools/out/reliance_1min_sept2026.csv`` into
   ``market_data_cache`` through the same loader the Data tab's offline path
   uses (:mod:`scripts.ingest_csv_to_mdc`);
3. prints the coverage the backtest API will report, so a silent empty import
   is visible here rather than three screens later.

Then::

    BACKTEST_SOURCE=db FORWARD_TEST_DB_URL=sqlite:////abs/path/tools/out/reliance.db \\
        PYTHONPATH=src python -m backtest.web.app --port 5003

The database is gitignored (``*.db``) and therefore local-only; this script is
the thing that can be re-run after a fresh checkout. It is idempotent — running
it again rebuilds from scratch rather than doubling the bars.

``--keep`` skips the rebuild and only re-ingests, which is what you want after
editing the CSV.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"
DB = ROOT / "tools" / "out" / "reliance.db"
CSV = ROOT / "tools" / "out" / "reliance_1min_sept2026.csv"
INGEST = ROOT / "scripts" / "ingest_csv_to_mdc.py"


def _load_ingest():
    """Import the ingest script by path — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("ingest_csv_to_mdc", INGEST)
    assert spec and spec.loader, f"cannot load {INGEST}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_csv_to_mdc"] = module
    spec.loader.exec_module(module)
    return module


def create_schema(db: Path) -> int:
    """Apply every sqlite migration in filename order. Returns how many ran."""
    files = sorted(MIGRATIONS.glob("*.sqlite.sql"))
    if not files:
        raise SystemExit(f"no *.sqlite.sql under {MIGRATIONS}")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        for path in files:
            conn.executescript(path.read_text())
        conn.commit()
    finally:
        conn.close()
    return len(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keep", action="store_true", help="do not recreate the database")
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--csv", type=Path, default=CSV)
    ap.add_argument("--symbol", default="RELIANCE")
    args = ap.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"no CSV at {args.csv}")

    if args.keep and args.db.exists():
        print(f"keeping existing database {args.db}")
    else:
        if args.db.exists():
            args.db.unlink()
        n = create_schema(args.db)
        print(f"applied {n} sqlite migrations -> {args.db}")

    # Ingest in-process rather than by subprocess: same code path, and the
    # output stays in this program's ordering instead of interleaving.
    #
    # The ingest resolves its target through ``get_db_url()`` — env first, then
    # config/database.yaml — NOT through the ``--db`` argument this script was
    # given. Without pinning FORWARD_TEST_DB_URL here, the migrations land in
    # ``--db`` and the bars land in the default profile's database, and the
    # failure surfaces only later as "no such table: market_data_cache".
    ingest = _load_ingest()
    argv = [
        str(INGEST),
        "--csv", str(args.csv),
        "--symbol", args.symbol,
        "--timeframe", "1min",
        "--source", "mstock",
    ]
    saved_argv, saved_url = sys.argv, os.environ.get("FORWARD_TEST_DB_URL")
    sys.argv = argv
    os.environ["FORWARD_TEST_DB_URL"] = f"sqlite:///{args.db.resolve()}"
    try:
        rc = ingest.main()
    finally:
        sys.argv = saved_argv
        if saved_url is None:
            os.environ.pop("FORWARD_TEST_DB_URL", None)
        else:
            os.environ["FORWARD_TEST_DB_URL"] = saved_url
    if rc != 0:
        return rc

    conn = sqlite3.connect(args.db)
    try:
        rows = conn.execute(
            "SELECT timeframe, COUNT(*), MIN(ts), MAX(ts) FROM market_data_cache "
            "GROUP BY timeframe ORDER BY timeframe"
        ).fetchall()
    finally:
        conn.close()

    # The bars and the schema must be in the SAME file, or every later command
    # in the docstring is a confusing failure. Cheap to prove, so prove it.
    if not rows:
        raise SystemExit(
            f"ingest wrote nothing to {args.db} — check that the CSV at {args.csv} "
            f"parsed and that FORWARD_TEST_DB_URL was not overridden"
        )

    print("\nmarket_data_cache now holds:")
    for tf, n, lo, hi in rows:
        print(f"  {tf:<6} {n:>6} bars   {lo} .. {hi}")
    print(
        f"\nrun it:  BACKTEST_SOURCE=db "
        f"FORWARD_TEST_DB_URL=sqlite:///{args.db} PYTHONPATH=src "
        f"python -m backtest.web.app --port 5003"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
