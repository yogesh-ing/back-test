"""One-shot ledger repair: retire ``portfolios`` rows no runner owns (BUG-37).

The writer-side fix is ``PortfolioManager._reap_orphan_portfolio_rows()``, which
claims rows against the runners that boot. This tool does the same job against
the rows already sitting in the live database, so the capital ledger can be
corrected without restarting the trading process — and so the plan can be read
before anything is written.

Why it exists: measured 2026-10-30 on this deployment, ``portfolios`` held 63
``status='active'`` rows carrying ₹3.40 crore of ``initial_capital`` while only
14 runners were live. Every capital check that reads ``sum(initial_capital)
WHERE status='active'`` was measuring books that do not exist.

SAFETY
------
* dry-run is the default: it prints the plan and touches nothing;
* ``--execute`` snapshots the whole table into ``bak_portfolios_reap_<utcstamp>``
  in the same transaction as the flip, and prints the rollback command;
* a reap whose state file lists no runners is refused — an empty claim set
  would retire the entire ledger (the 2026-10-27 refused-restore shape);
* retirement is a status flip to ``stopped``. No row is deleted, so trades,
  positions, orders and signals keep their foreign keys.

    PYTHONPATH=src python scripts/reap_orphan_portfolios.py
    PYTHONPATH=src python scripts/reap_orphan_portfolios.py --execute
    PYTHONPATH=src python scripts/reap_orphan_portfolios.py --rollback \\
        --bak-table bak_portfolios_reap_20261030T060000Z
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, List

from sqlalchemy import func, select, text

from backtest.db import DatabaseManager
from backtest.db.models import Portfolio
from backtest.forward.portfolio_reaper import (
    RETIRED_STATUS,
    is_claimed,
    reap_orphan_portfolio_rows,
    runner_claim_keys,
)
from backtest.timekeeping import now_utc

DEFAULT_STATE_PATH = Path("src/data/portfolio_state.json")


@dataclass
class ClaimedConfig:
    name: str


@dataclass
class ClaimedRunner:
    """The two fields :func:`runner_claim_keys` reads off a real runner."""

    instance_id: str
    config: ClaimedConfig
    portfolio: Any = None
    _db_portfolio_id: Any = None


def runners_from_state(state_path: Path) -> List[ClaimedRunner]:
    """The runners this deployment boots from — the claim set for the ledger."""
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    runners: List[ClaimedRunner] = []
    for snap in payload.get("runners", []):
        cfg = snap.get("config", {}) or {}
        instance_id = str(cfg.get("instance_id") or "").strip()
        if not instance_id:
            continue
        runners.append(
            ClaimedRunner(instance_id=instance_id, config=ClaimedConfig(str(cfg.get("name") or "")))
        )
    return runners


def active_capital(session: Any) -> Decimal:
    total = session.scalar(
        select(func.sum(Portfolio.initial_capital)).where(Portfolio.status == "active")
    )
    return Decimal(str(total or 0))


def snapshot_name() -> str:
    return f"bak_portfolios_reap_{now_utc().strftime('%Y%m%dT%H%M%SZ')}"


def print_plan(session: Any, claims, before: Decimal) -> List[str]:
    rows = session.scalars(select(Portfolio).where(Portfolio.status == "active")).all()
    reaped = [row for row in rows if not is_claimed(row, claims)]
    print(f"active rows: {len(rows)}  claimed: {len(rows) - len(reaped)}  "
          f"to retire (status -> {RETIRED_STATUS!r}): {len(reaped)}")
    print(f"sum(initial_capital) WHERE status='active': Rs {before:,.2f}")
    for row in sorted(reaped, key=lambda r: str(r.name)):
        print(f"  retire  {row.name!r}  capital={row.initial_capital}  id={row.portfolio_id}")
    if reaped:
        remaining = before - sum(Decimal(str(r.initial_capital)) for r in reaped)
        print(f"after the flip the ledger reads: Rs {remaining:,.2f}")
    return [str(row.name) for row in reaped]


def do_rollback(db: DatabaseManager, bak_table: str) -> int:
    """Restore statuses from a snapshot taken by a previous --execute."""
    if not bak_table.startswith("bak_portfolios_reap_"):
        raise SystemExit("--bak-table must be a bak_portfolios_reap_* snapshot")
    restored = 0
    with db.session() as session:
        pairs = session.execute(
            text(f'select portfolio_id, status from "{bak_table}"')  # noqa: S608 — name is ours
        ).all()
        for portfolio_id, status in pairs:
            row = session.get(Portfolio, str(portfolio_id))
            if row is not None and row.status != str(status):
                row.status = str(status)
                restored += 1
    print(f"restored {restored} row(s) from {bak_table}")
    return restored


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"runner state file (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--execute", action="store_true", help="write the flip (default is dry-run)"
    )
    parser.add_argument("--rollback", action="store_true", help="restore from --bak-table")
    parser.add_argument("--bak-table", default="", help="snapshot table to roll back from")
    args = parser.parse_args(argv)

    db = DatabaseManager.from_env()

    if args.rollback:
        if not args.bak_table:
            raise SystemExit("--rollback needs --bak-table")
        return do_rollback(db, args.bak_table)

    if not args.state_file.exists():
        raise SystemExit(f"state file not found: {args.state_file} — nothing can be claimed")
    runners = runners_from_state(args.state_file)
    if not runners:
        raise SystemExit(
            f"{args.state_file} lists no runners — refusing to reap (every active row "
            "would be retired). Boot the app and let the restore claim the ledger first."
        )
    claims = runner_claim_keys(runners)

    with db.session() as session:
        before = active_capital(session)
        print(f"claim set: {len(runners)} runner(s) from {args.state_file}")
        names = print_plan(session, claims, before)

        if not names:
            print("ledger is already clean — nothing to do")
            return 0
        if not args.execute:
            print("dry-run: nothing written. Re-run with --execute to apply.")
            return 0

        bak = snapshot_name()
        session.execute(text(f'create table "{bak}" as select * from portfolios'))  # noqa: S608
        report = reap_orphan_portfolio_rows(session, claims)
        after = active_capital(session)
        print(
            f"\nretired {report.changed} row(s), Rs {report.reaped_capital:,.2f} off the ledger "
            f"({before:,.2f} -> {after:,.2f})"
        )
        if report.reaped_open_exposure_rows:
            print(
                f"note: {report.reaped_open_exposure_rows} open position row(s) still hang off "
                "retired books; their history is untouched"
            )
        print(f"snapshot: {bak}")
        print(f"rollback: PYTHONPATH=src python scripts/reap_orphan_portfolios.py --rollback "
              f"--bak-table {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
