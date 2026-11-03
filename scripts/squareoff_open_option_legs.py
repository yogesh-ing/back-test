"""One-off at-rest square-off of the open option legs before a clean restart.

WHY THIS EXISTS (2026-11-04)
---------------------------
Four option books still hold legs that were never closed:

    CE_instant_buy   NIFTY26NOV23350CE   entry 352.50  mark 579.7
    PE_instant_buy   NIFTY26NOV23350PE   entry 289.80  mark 158.2
    BANKNIFTY legs   55300CE / 55400PE   entry 832.20 / 719.15

They were frozen by the 03-Nov containment (books STOPPED, so no mark loop and
no exit path runs), and the operator's decision is to start 04-Nov onward with
new instances and no carried trades.  On this deployment the mirror tables
``option_open_structures`` / ``option_open_legs`` do not exist yet — the live
database is at alembic 022, migration 024 is unapplied — so an open option leg
has no DB row at all: it exists only in ``src/data/portfolio_state.json``, and
``remove_runner`` deletes that entry without closing the leg.  Squaring off at
rest is therefore the only way to retire these positions with a price attached
instead of letting them vanish mid-air.

WHY THE PRICE IS THE LAST MARK
------------------------------
The writer already has this rule: when a close cannot get a quote it settles at
the last known mark rather than booking a ₹0.00 fill
(`src/backtest/options/paper_trading.py:1101-1114`).  The market is closed and
the books are stopped, so every leg here is in exactly that branch.  Each mark
is re-checked against the same two gates the live seam uses before it is
accepted — the intrinsic floor (`strike`/`option_type` against the book's own
``bridge.scalars.last_spot``, `src/backtest/options/quote_providers.py`
``_sanity_problem``) and the multiplicative distance to its own entry
(`LTP_DISTANCE_MAX_RATIO`).  A leg whose mark fails either gate is reported and
left open; this script will not invent a closing price.

CASH AND P&L ARITHMETIC
-----------------------
Faithful to the writer, per leg:

* realized P&L is gross — ``(exit - entry) * qty_units`` for a BUY leg,
  ``(entry - exit) * qty_units`` for a SELL leg, ``qty_units = quantity * lot_size``
  (`OptionPosition.close`, `paper_trading.py:274-295`);
* cash moves by the exit notional (`+=` selling a long, `-=` buying back a short,
  `paper_trading.py:1133-1139`);
* exit statutory charges come from the same calculator the bridge attaches
  (`CommissionCalculator.for_broker("mstock")`) and are added to
  ``structure.statutory_fees`` (`paper_trading.py:1172-1191`).

`exit_reason` is ``"manual"`` — one of the three values
`StructurePosition.exit_reason` documents (`paper_trading.py:334-338`) and the
one `_map_exit_reason` maps to the DB enum ``manual``.

SAFETY
------
* dry-run is the default and prints every leg, gate result and cash delta;
* refuses ``--execute`` while the web app is listening on :5000 — its persister
  rewrites this file and would clobber the repair (``--force`` overrides);
* ``--execute`` first copies the state file to
  ``portfolio_state.json.bak_squareoff_<stamp>`` and writes the full books
  (closed history included) to an archive file, then writes atomically
  (tmp + ``os.replace``), matching ``StateStore.save``;
* ``--rollback`` restores that exact snapshot;
* touches only ``bridge.broker`` inside the runner entries.  No DB row is
  written or deleted, and no trade history is removed — the 2,960 ``trades``
  rows and 1,974 ``orders`` rows are left as they are.

Removing the instances is deliberately NOT done here: ``remove_runner`` folds
the book's equity into the capital ledger and releases its feed subscriptions,
so it must go through the app (`DELETE /api/portfolio/runner/<id>`) after the
restart, not by editing the file.

Usage
-----
    python scripts/squareoff_open_option_legs.py                 # dry run
    python scripts/squareoff_open_option_legs.py --execute
    python scripts/squareoff_open_option_legs.py --rollback --bak-file <path>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
from decimal import Decimal
from typing import Any, Iterator

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from backtest.options.quote_providers import (  # noqa: E402
    LTP_DISTANCE_MAX_RATIO,
    NSE_OPTION_TICK_SIZE,
)
from backtest.simulator.fees import CommissionCalculator  # noqa: E402
from backtest.timekeeping import now_utc, session_date  # noqa: E402

DEFAULT_STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "data",
    "portfolio_state.json",
)
EXIT_REASON = "manual"
ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except Exception:  # noqa: BLE001
        return ZERO


def _runner_id(entry: dict[str, Any]) -> str:
    cfg = entry.get("config") or {}
    return str(entry.get("instance_id") or cfg.get("instance_id") or "?")


def _runner_name(entry: dict[str, Any]) -> str:
    return str((entry.get("config") or {}).get("name") or "?")


def _books(runners: list[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], dict[str, Any], list]]:
    """Yield ``(entry, broker_book, structures)`` for every runner holding a book."""
    for entry in runners:
        bridge = entry.get("bridge")
        if not isinstance(bridge, dict):
            continue
        book = bridge.get("broker")
        if not isinstance(book, dict):
            continue
        structures = book.get("structures")
        if isinstance(structures, list):
            yield entry, book, structures


def _spot(bridge: dict[str, Any]) -> Decimal:
    return _dec((bridge.get("scalars") or {}).get("last_spot"))


def _gate(leg: dict[str, Any], spot: Decimal) -> tuple[bool, str]:
    """Whether this mark may be used as a closing price, and why."""
    mark = _dec(leg.get("current_price"))
    entry = _dec(leg.get("entry_price"))
    qty = _dec(leg.get("quantity")) * _dec(leg.get("lot_size"))
    if mark <= 0:
        return False, f"mark={mark} is not a price"
    if qty <= 0:
        return False, f"quantity*lot_size={qty} is not a position"

    strike = _dec(leg.get("strike"))
    kind = str(leg.get("option_type") or "").upper()
    if spot > 0 and strike > 0 and kind in ("CE", "PE"):
        intrinsic = max(ZERO, (spot - strike) if kind == "CE" else (strike - spot))
        if intrinsic > 0 and mark < intrinsic - Decimal(str(NSE_OPTION_TICK_SIZE)):
            return False, (
                f"intrinsic_floor: mark={mark} < intrinsic={intrinsic:.2f} "
                f"({kind} strike={strike} spot={spot})"
            )

    ratio = Decimal(str(LTP_DISTANCE_MAX_RATIO))
    if entry > 0 and (mark * ratio < entry or mark > entry * ratio):
        return False, (
            f"entry_distance: mark={mark} vs entry={entry} "
            f"(outside x{LTP_DISTANCE_MAX_RATIO} of its own entry)"
        )
    return True, (
        f"mark={mark} vs entry={entry}, "
        + (f"spot={spot}, {kind} strike={strike}" if spot > 0 else "no spot on book")
    )


def _close_leg(leg: dict[str, Any], mark: Decimal, ts_iso: str) -> tuple[Decimal, Decimal, bool]:
    """Apply the writer's close to one leg dict; return ``(notional, realized, is_long)``."""
    units = _dec(leg.get("quantity")) * _dec(leg.get("lot_size"))
    entry = _dec(leg.get("entry_price"))
    is_long = str(leg.get("side") or "BUY").upper() == "BUY"
    realized = (mark - entry) * units if is_long else (entry - mark) * units
    leg["current_price"] = str(mark)
    leg["realized_pnl"] = str(realized)
    leg["unrealized_pnl"] = "0"
    leg["status"] = "closed"
    leg["closed_at"] = ts_iso
    leg["last_updated"] = ts_iso
    return mark * units, realized, is_long


def _port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", 5000)) == 0


def _snapshot(path: str, kind: str, stamp: str) -> str:
    bak = f"{path}.bak_{kind}_{stamp}"
    shutil.copy2(path, bak)
    return bak


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-file", default=DEFAULT_STATE)
    ap.add_argument("--execute", action="store_true", help="write the square-off (else dry run)")
    ap.add_argument("--force", action="store_true", help="allow --execute while :5000 listens")
    ap.add_argument("--rollback", action="store_true", help="restore --bak-file over the file")
    ap.add_argument("--bak-file", help="snapshot made by --execute")
    ap.add_argument(
        "--archive-dir",
        help="where to write the full-book archive (default: beside the state file)",
    )
    args = ap.parse_args(argv)

    path = os.path.abspath(args.state_file)
    if not os.path.exists(path):
        print(f"state file not found: {path}")
        return 2

    if args.rollback:
        if not args.bak_file or not os.path.exists(args.bak_file):
            print("--rollback needs --bak-file pointing at an existing snapshot")
            return 2
        if _port_open() and not args.force:
            print("refusing to roll back while the web app is listening on :5000 (use --force)")
            return 3
        shutil.copy2(args.bak_file, path)
        print(f"restored {path} from {args.bak_file}")
        return 0

    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    runners = payload.get("runners") or []
    if not isinstance(runners, list):
        print("unexpected state shape: runners is not a list — refusing")
        return 2

    calc = CommissionCalculator.for_broker("mstock")
    ts = now_utc()
    ts_iso = ts.isoformat()
    sess = (session_date(ts) or ts.date()).isoformat()

    closed_legs: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    books_with_legs = 0
    total_cash_delta = ZERO
    total_realized = ZERO
    total_fees = ZERO

    for entry, book, structures in _books(runners):
        bridge = entry["bridge"]
        spot = _spot(bridge)
        open_pairs = [
            (s, lg)
            for s in structures
            if isinstance(s, dict)
            for lg in (s.get("legs") or [])
            if isinstance(lg, dict) and lg.get("closed_at") is None
        ]
        if not open_pairs:
            continue
        books_with_legs += 1
        ok_pairs, bad_pairs = [], []
        for struct, leg in open_pairs:
            passed, why = _gate(leg, spot)
            (ok_pairs if passed else bad_pairs).append((struct, leg, why))
        for struct, leg, why in bad_pairs:
            refused.append({
                "runner": _runner_name(entry),
                "symbol": leg.get("trading_symbol"),
                "why": why,
            })

        cash = _dec(book.get("available_cash"))
        for struct, leg, why in ok_pairs:
            mark = _dec(leg.get("current_price"))
            notional, realized, is_long = _close_leg(leg, mark, ts_iso)
            cash += notional if is_long else -notional
            exit_fills = [{"side": "SELL" if is_long else "BUY",
                           "quantity": _dec(leg.get("quantity")) * _dec(leg.get("lot_size")),
                           "price": mark}]
            try:
                fees = _dec(calc.calculate_structure(exit_fills, when=ts).total)
            except Exception:  # noqa: BLE001 — never invent a fee we cannot compute
                fees = ZERO
            cash -= fees
            struct["closed_at"] = ts_iso
            struct["exit_reason"] = EXIT_REASON
            struct["session_date"] = sess
            struct["statutory_fees"] = str(_dec(struct.get("statutory_fees")) + fees)
            total_cash_delta += (notional if is_long else -notional) - fees
            total_realized += realized
            total_fees += fees
            closed_legs.append({
                "runner": _runner_name(entry),
                "instance_id": _runner_id(entry),
                "structure_id": struct.get("structure_id"),
                "symbol": leg.get("trading_symbol"),
                "side": leg.get("side"),
                "units": int(_dec(leg.get("quantity")) * _dec(leg.get("lot_size"))),
                "entry": str(_dec(leg.get("entry_price"))),
                "exit": str(mark),
                "realized_gross": str(realized),
                "exit_fees": str(fees),
                "gate": why,
            })
        book["available_cash"] = str(cash)

    print(f"state file          : {path}")
    print(f"runners in file     : {len(runners)}")
    print(f"books with open legs: {books_with_legs}")
    print(f"square-off stamp    : {ts_iso}  (session_date {sess}, exit_reason '{EXIT_REASON}')\n")
    print(f"{'runner':30} {'symbol':24} {'side':5}{'entry':>11}{'exit':>11}"
          f"{'gross':>13}{'fees':>9}")
    for row in closed_legs:
        print(f"{row['runner'][:30]:30} {str(row['symbol'])[:24]:24} {row['side'][:5]:5}"
              f"{row['entry']:>11}{row['exit']:>11}"
              f"{row['realized_gross']:>13}{row['exit_fees']:>9}")
    print(f"\ngross realized on the closed legs : {total_realized}")
    print(f"exit statutory fees charged       : {total_fees}")
    print(f"net cash moved into the books     : {total_cash_delta}")

    if refused:
        print("\nREFUSED — left open, no price invented:")
        for row in refused:
            print(f"  {row['runner']:30} {str(row['symbol'])[:24]:24} {row['why']}")

    if not closed_legs:
        print("\nnothing to do.")
        return 0

    if not args.execute:
        print("\nDRY RUN — nothing written. Re-run with --execute (web app stopped).")
        return 0
    if _port_open() and not args.force:
        print("\nrefusing to write while the web app is listening on :5000 — its persister "
              "would rewrite this file. Stop it first, or pass --force.")
        return 3

    stamp = ts.strftime("%Y%m%d_%H%M%S")
    bak = _snapshot(path, "squareoff", stamp)
    archive_dir = args.archive_dir or os.path.dirname(path)
    os.makedirs(archive_dir, exist_ok=True)
    archive = os.path.join(archive_dir, f"option_books_archive_{stamp}.json")
    with open(archive, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "archived_at": ts_iso,
                "reason": "clean-start square-off; remove_runner drops these books' history",
                "state_file": path,
                "bak_file": bak,
                "books": [
                    {
                        "instance_id": _runner_id(entry),
                        "name": _runner_name(entry),
                        "status": entry.get("status"),
                        "config": entry.get("config"),
                        "closed_trades_cache": entry.get("closed_trades_cache"),
                        "bridge": entry.get("bridge"),
                    }
                    for entry, _book, _st in _books(runners)
                ],
            },
            fh,
            indent=2,
        )

    payload["saved_at"] = ts_iso
    tmp = f"{path}.squareoff.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)

    with open(path, encoding="utf-8") as fh:
        after = json.load(fh)
    still_open = sum(
        1
        for _e, _b, structures in _books(after.get("runners") or [])
        for s in structures
        for lg in (s.get("legs") or [])
        if isinstance(lg, dict) and lg.get("closed_at") is None
    )
    print(f"\nwrote {path}")
    print(f"snapshot : {bak}")
    print(f"archive  : {archive}")
    print(f"open legs after the write: {still_open} (refused above stay open by design)")
    print(f"rollback : python {os.path.basename(__file__)} --rollback --bak-file {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
