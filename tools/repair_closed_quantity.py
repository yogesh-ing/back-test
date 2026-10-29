#!/usr/bin/env python3
"""One-off at-rest repair: stamp ``closed_quantity`` on persisted closed positions.

``Position.quantity`` is zero once a round trip fully closes, so the history
rows written before the stamp existed carry no size and no direction — the
deep-dive Trades tab showed ``qty 0`` and every trade as LONG. The writer now
stamps ``closed_quantity`` (signed) at the final fill; this script backfills the
rows already on disk.

Derivation: ``realized_pnl = (exit - entry) x signed_quantity``, so the signed
size is recoverable from the row itself when the exit price differs from the
entry. A row whose prices are identical (or whose implied size is not a whole
number of shares) is left alone and reported — guessing is worse than a blank.

The server holds this file in memory and rewrites it on every save, so run this
**after** stopping ``:5000`` and **before** starting it again.

    python tools/repair_closed_quantity.py                 # dry run (default)
    python tools/repair_closed_quantity.py --apply         # write, with .bak_ snapshot
    python tools/repair_closed_quantity.py --state <path>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

KEY = "closed_positions"
STAMP = "closed_quantity"


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def derive_closed_quantity(row: dict) -> tuple[Decimal | None, str]:
    """(signed size, reason) for one closed-position row.

    ``reason`` explains a ``None`` so the report can say *why* a row was skipped.
    """
    realized = _dec(row.get("realized_pnl"))
    entry = _dec(row.get("average_entry_price"))
    exit_price = _dec(row.get("current_price"))
    if realized is None or entry is None or exit_price is None:
        return None, "missing price/pnl field"
    diff = exit_price - entry
    if diff == 0:
        return None, "exit price equals entry — size is not recoverable"
    signed = realized / diff
    nearest = signed.to_integral_value(rounding=ROUND_HALF_UP)
    if abs(signed - nearest) > Decimal("0.01"):
        return None, f"implied size {signed:.4f} is not a whole number of shares"
    if nearest == 0:
        return None, "implied size is zero"
    return nearest, "derived"


def walk(node, visit, path="$"):
    """Apply ``visit(row)`` to every closed-position dict in the state tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == KEY and isinstance(value, list):
                for index, row in enumerate(value):
                    if isinstance(row, dict):
                        visit(row, f"{path}.{key}[{index}]")
            else:
                walk(value, visit, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            walk(item, visit, f"{path}[{index}]")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_state = Path(__file__).resolve().parents[1] / "src" / "data" / "portfolio_state.json"
    parser.add_argument("--state", type=Path, default=default_state)
    parser.add_argument(
        "--apply", action="store_true", help="write the file (a .bak_ snapshot is taken first)"
    )
    args = parser.parse_args(argv)

    state_path: Path = args.state
    if not state_path.exists():
        print(f"state file not found: {state_path}", file=sys.stderr)
        return 2

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    stamped, skipped, already = [], [], 0

    def visit(row: dict, where: str) -> None:
        nonlocal already
        if row.get(STAMP) is not None:
            already += 1
            return
        quantity = _dec(row.get("quantity"))
        if quantity is None or quantity != 0:
            return  # an open (or half-closed) row keeps its own quantity
        size, reason = derive_closed_quantity(row)
        if size is None:
            skipped.append((where, row.get("symbol"), reason))
            return
        row[STAMP] = format(size, "f")
        stamped.append((where, row.get("symbol"), row[STAMP]))

    walk(payload, visit)

    print(f"state: {state_path}")
    print(f"rows already stamped: {already}")
    print(f"rows stamped now:     {len(stamped)}")
    for where, symbol, size in stamped[:20]:
        print(f"  + {symbol:<14} {size:>12}  {where}")
    if len(stamped) > 20:
        print(f"  … {len(stamped) - 20} more")
    print(f"rows left unrecoverable: {len(skipped)}")
    for where, symbol, reason in skipped[:20]:
        print(f"  - {symbol:<14} {reason}  {where}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return 0
    if not stamped:
        print("\nNothing to write.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = state_path.with_name(f"{state_path.name}.bak_{stamp}")
    shutil.copy2(state_path, backup)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {state_path}\nBackup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
