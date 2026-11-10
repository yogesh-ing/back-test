"""Measure the pair-parity noise floor from the 2026-11-09 cross-broker witness.

The band on ``PAIR_PARITY_MAX_DEVIATION`` has to come from a session of capture
rows, not from a hand-picked number, so this replays the ``shadow_diff`` tape —
one row per contract per 10-second sweep, carrying BOTH brokers' bid/ask for the
same contract at the same instant — through the shipped ``parity_deviation`` and
reports the distribution of |deviation| per broker per side.

Grouping is by (sweep, expiry), so a ring holding two expiries never compares a
weekly against a monthly, and a sweep where fewer than PAIR_PARITY_MIN_STRIKES
strikes have both sides is skipped, exactly as the gate skips it.

    PYTHONPATH=src python tools/out/measure_parity_band_20261109.py
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict

from backtest.options.quote_providers import (
    PAIR_PARITY_MAX_DEVIATION,
    parity_deviation,
)

SHADOW_FILE = "logs/feed_depth/feed_shadow_2026-11-09.jsonl"
#: mStock's own best quote and Upstox's, taken in the same sweep.
SIDES = ("bid", "ask")


def _num(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    # (sweep_second, expiry, side) -> [(strike, "CE"|"PE", price)]
    groups: dict[tuple[str, str, str], list[tuple[float, str, float]]] = defaultdict(list)
    rows = 0
    with open(SHADOW_FILE, encoding="utf-8") as handle:
        for line in handle:
            if '"shadow_diff"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            strike = _num(row.get("strike"))
            otype = str(row.get("option_type") or "").upper()
            expiry = str(row.get("expiry") or "")
            if strike <= 0 or otype not in ("CE", "PE") or not expiry:
                continue
            sweep = str(row.get("recv_utc") or "")[:19]
            rows += 1
            for broker_key in ("mstock", "upstox"):
                book = row.get(broker_key) or {}
                for side in SIDES:
                    price = _num(book.get(side))
                    if price > 0:
                        groups[(sweep, expiry, f"{broker_key}:{side}")].append(
                            (strike, otype, price)
                        )

    print(f"{rows} shadow rows -> {len(groups)} (sweep, expiry, broker, side) groups")

    for group_key in sorted({k[2] for k in groups}):
        devs: list[float] = []
        breaches = 0
        own_spread: list[float] = []
        for (sweep, expiry, side), legs in groups.items():
            if side != group_key:
                continue
            for strike in {leg[0] for leg in legs}:
                result = parity_deviation(legs, strike)
                if result is None:
                    continue
                own, reference, peers = result
                deviation = abs(own - reference)
                devs.append(deviation)
                own_spread.append(float(peers))
                if deviation > PAIR_PARITY_MAX_DEVIATION:
                    breaches += 1
        if not devs:
            print(f"{group_key}: nothing measurable")
            continue
        ordered = sorted(devs)
        pct = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
        print(
            f"\n{group_key}: {len(devs)} leg-reads, median paired strikes "
            f"{statistics.median(own_spread):.0f}"
        )
        print(
            f"    |dev| p50={pct(0.50):.2f} p95={pct(0.95):.2f} "
            f"p99={pct(0.99):.2f} max={ordered[-1]:.2f} mean={statistics.mean(devs):.2f}"
        )
        print(
            f"    over the shipped band ({PAIR_PARITY_MAX_DEVIATION:.2f}): {breaches} "
            f"({100.0 * breaches / len(devs):.3f}%)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
