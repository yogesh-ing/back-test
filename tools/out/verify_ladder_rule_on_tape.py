"""Run the SHIPPED ladder-order rule over the 2026-11-09 dual-broker tape.

Not a re-implementation: it imports ``backtest.data.ladder_shape.order_problem``,
so the numbers below are what the code in the wrappers actually decides. For each
broker it counts flagged packets, splits by side, prints the worst offender, and
measures the rule's blind spot: packets whose ladder is perfectly well-ordered yet
whose touch price is a multiple of the same packet's last trade. Those are not the
order rule's to catch — they belong to the seam's ``ltp_distance`` gate — and the
count says how much of the tape that leaves to the other check.

    PYTHONPATH=src python tools/out/verify_ladder_rule_on_tape.py
"""

from __future__ import annotations

import json
import sys

from backtest.data.ladder_shape import ASK_NOT_MONOTONE, BID_NOT_MONOTONE, order_problem

FILES = {
    "mstock": "logs/feed_depth/feed_mstock_2026-11-09.jsonl",
    "upstox": "logs/feed_depth/feed_upstox_2026-11-09.jsonl",
}
#: The seam's existing multiplier (``LTP_DISTANCE_MAX_RATIO``).
BAND = 5.0


def _levels(pairs) -> list[dict]:
    """The tape stores ``[[price, qty], …]``; the wrapper takes dicts of levels."""
    out: list[dict] = []
    for pair in pairs or []:
        try:
            price = float(pair[0])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0:
            out.append({"price": price, "quantity": pair[1] if len(pair) > 1 else 1})
    return out


def main() -> int:
    for broker, path in FILES.items():
        books = flagged = 0
        ordered_but_absurd = 0
        absurd_only_ladder = 0
        by_reason = {ASK_NOT_MONOTONE: 0, BID_NOT_MONOTONE: 0}
        worst = None
        worst_absurd = None
        torn = 0
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if '"packet"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    torn += 1
                    continue
                if row.get("kind") != "packet":
                    continue
                bids, asks = _levels(row.get("bids")), _levels(row.get("asks"))
                if not bids and not asks:
                    continue
                books += 1
                ltp = float(row.get("ltp") or 0.0)
                ask = float(asks[0]["price"]) if asks else 0.0
                bid = float(bids[0]["price"]) if bids else 0.0
                ratio = max(ask / ltp if ltp else 0.0, bid / ltp if ltp else 0.0)
                # Same test the seam runs: an offer more than x5 its own last
                # trade, or a bid less than a fifth of it.
                off_band = ltp > 0 and (
                    ask > ltp * BAND or (bid > 0 and bid * BAND < ltp)
                )
                reason = order_problem(bids, asks)
                if reason is None:
                    if off_band:
                        ordered_but_absurd += 1
                        if worst_absurd is None or ratio > worst_absurd[0]:
                            worst_absurd = (ratio, row, bids, asks)
                    continue
                flagged += 1
                by_reason[reason] += 1
                if off_band:
                    absurd_only_ladder += 1
                if worst is None or ratio > worst[0]:
                    worst = (
                        ratio,
                        row.get("recv_utc", "")[11:19],
                        row.get("token"),
                        row.get("symbol") or row.get("trading_symbol"),
                        ltp,
                        bid,
                        ask,
                        reason,
                        [lv["price"] for lv in bids],
                        [lv["price"] for lv in asks],
                        row.get("ladder_status"),
                    )
        print(f"\n=== {broker}: ladders={books} flagged={flagged} "
              f"({100.0 * flagged / max(books, 1):.4f}%) torn={torn}")
        print(f"    ask_not_monotone={by_reason[ASK_NOT_MONOTONE]} "
              f"bid_not_monotone={by_reason[BID_NOT_MONOTONE]}")
        print(f"    of the flagged, also outside x{BAND} of their own LTP: "
              f"{absurd_only_ladder}")
        print(f"    BLIND SPOT: well-ordered ladders outside x{BAND} of their own "
              f"LTP (seam's ltp_distance job): {ordered_but_absurd}")
        if worst_absurd:
            ratio, row, bids, asks = worst_absurd
            print(f"          worst ltp={row.get('ltp')} bid={bids[0]['price'] if bids else 0} "
                  f"ask={asks[0]['price'] if asks else 0} ratio={ratio:.1f}x "
                  f"tok={row.get('token')} utc={row.get('recv_utc', '')[11:19]} "
                  f"levels={len(bids)}/{len(asks)}")
        if worst:
            (ratio, utc, token, symbol, ltp, bid, ask, reason, bids, asks, status) = worst
            print(f"    worst flagged utc={utc} tok={token} {symbol} ltp={ltp} bid={bid} ask={ask}")
            print(f"          reason={reason} labelled_then={status} ratio={ratio:.1f}x")
            print(f"          bids={bids}")
            print(f"          asks={asks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
