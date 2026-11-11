"""Count structurally impossible ladders in the captured depth tapes.

WHAT "STRUCTURALLY IMPOSSIBLE" MEANS (the definition `docs/plans/upstox-primary-
depth-plan.md` section 1 cites)

A five-level book has one invariant that needs no calibration, no threshold and
no second broker:

  * bids must step DOWN as you walk away from the touch,
  * asks must step UP as you walk away from the touch,
  * the book must not be crossed (best ask >= best bid).

Any packet that violates one of those was mis-published (or mis-parsed): the
level the engine would trade at is not the best price anyone is actually
quoting. It is a proof, not a heuristic -- a "wrong price" detector that needs a
hand-picked number is exactly what the 2026-11-08 junk-ask incident said we may
not add (docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md section 6).

The off-touch ratios below (ask > 1.5x ltp, bid < ltp/1.5) are NOT part of that
definition; they are reported separately and are a ratio gate, so treat them as
context for a breach rather than as evidence of one.

COVERAGE VOCABULARY (plan-review §10.4): this tool and
``measure_feed_cadence.py`` split packet frames the same way —
``two_sided`` (both ladders and both touches present) / ``one_sided``
(anything on at least one side) / ``no_book`` (nothing at all). Scorecard
S1's "frames served with a two-sided book" means ``two_sided / packets``
in exactly this vocabulary.

Read-only, stdlib-only; never imports ``backtest``.

    python tools/out/count_impossible_ladders.py 2026-11-09
    python tools/out/count_impossible_ladders.py 2026-11-11 --broker upstox
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

BROKERS = ("mstock", "upstox")
EPS = 1e-9
# ask > RATIO x ltp / bid < ltp / RATIO: a ratio gate, kept separate from the
# structural invariant above so the headline never quietly includes it.
OFF_TOUCH_RATIO = 1.5


def scan(path):
    stats = Counter()
    worst = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if '"packet"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "packet":
                continue
            stats["packets"] += 1
            bids = [p for p, _ in row.get("bids") or []]
            asks = [p for p, _ in row.get("asks") or []]
            ltp = row.get("ltp") or 0.0
            # Coverage vocabulary — plan-review §10.4: the two analysis tools
            # must mean the same thing by "coverage". Adopting the cadence
            # tool's split (two_sided / one_sided / no_book) verbatim, so
            # scorecard S1 can state one definition and both scripts agree.
            if bids and asks and row.get("bid") and row.get("ask"):
                stats["two_sided"] += 1
            elif bids or asks or row.get("bid") or row.get("ask"):
                stats["one_sided"] += 1
            else:
                stats["no_book"] += 1
            if asks and bids:
                a_bad = [i for i in range(1, len(asks))
                         if asks[i] < asks[i - 1] - EPS]
                b_bad = [i for i in range(1, len(bids))
                         if bids[i] > bids[i - 1] + EPS]
                crossed = asks[0] < bids[0] - EPS
                stats["ask_not_monotone"] += bool(a_bad)
                stats["bid_not_monotone"] += bool(b_bad)
                stats["crossed_book"] += bool(crossed)
                if a_bad or b_bad or crossed:
                    stats["impossible_packets"] += 1
                    worst.append((max(asks) - min(asks), row.get("recv_utc"),
                                  row.get("token"), ltp, bids[:5], asks[:5],
                                  bool(a_bad), bool(b_bad), crossed))
            if ltp > 0:
                if row.get("ask") and row["ask"] > ltp * OFF_TOUCH_RATIO:
                    stats["ask_over_1p5x_ltp"] += 1
                if row.get("bid") and 0 < row["bid"] < ltp / OFF_TOUCH_RATIO:
                    stats["bid_under_ltp_over_1p5x"] += 1
    return stats, sorted(worst, key=lambda x: -x[0])[:10]


def report(broker, path):
    if not os.path.exists(path):
        print(f"=== {broker}: no tape at {path}")
        return
    stats, worst = scan(path)
    n = max(1, stats["packets"])
    print(f"\n=== {broker}: {stats['packets']} packets, "
          f"{os.path.getsize(path) / 1e6:.0f} MB  ({path})")
    for key in ("two_sided", "one_sided", "no_book", "ask_not_monotone",
                "bid_not_monotone", "crossed_book", "impossible_packets",
                "ask_over_1p5x_ltp", "bid_under_ltp_over_1p5x"):
        print(f"   {key:<22} {stats[key]:>7}  ({100.0 * stats[key] / n:.3f}%)")
    print(f"   structurally impossible (unique packets, breaching the "
          f"monotone/non-crossed invariant): {stats['impossible_packets']}")
    for spread, recv, token, ltp, bids, asks, ab, bb, cr in worst:
        print(f"   {recv} tok={token} ltp={ltp} ask_spread={spread:.2f} "
              f"askbad={ab} bidbad={bb} crossed={cr}")
        print(f"      bids={bids}")
        print(f"      asks={asks}")


def main(argv):
    day = argv[1] if len(argv) > 1 else "2026-11-09"
    brokers = list(BROKERS)
    if "--broker" in argv:
        brokers = [argv[argv.index("--broker") + 1]]
    root = os.getcwd()
    for broker in brokers:
        report(broker, os.path.join(root, "logs", "feed_depth",
                                    f"feed_{broker}_{day}.jsonl"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
