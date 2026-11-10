"""Measure how far an option's L1 legitimately travels in the time an exit takes.

Item D of ``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`` asks for a *bounded*
protective exit: a collar around an independent reference, so that a stop which
fires on a price nobody is trading at becomes a refusal plus a retry instead of a
booked loss. The collar's width has to be measured, like every other threshold
here — too tight and honest exits stop executing, too wide and the 2026-11-08
₹493.65 offer books again.

The unit of "honest" is the largest move a live ladder makes between two of its
own packets inside the window an exit decision takes. On 2026-11-08 that decision
was five seconds: 14:20:11 → 14:20:16, ₹367.00 → ₹493.65, a +₹126.65 jump on a
₹367 contract with the spot up ₹8.65.

Run it over whatever ``ENABLE_FEED`` tapes exist:

    PYTHONPATH=src python tools/out/measure_exit_collar_band.py \
        logs/feed_depth/feed_mstock_2026-11-09.jsonl \
        logs/feed_depth/feed_upstox_2026-11-09.jsonl

Rows are the recorder's own ``packet`` records; only two-sided ``full``-mode
ladders count, because a one-sided row says nothing an exit could be priced at.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any, Iterator, Optional

#: The window the 2026-11-08 exit took, and the ones around it worth printing.
WINDOWS_S = (1, 2, 5, 10, 20, 30)
#: Two packets further apart than this are a gap in the feed, not a move.
MAX_GAP_S = 30.0


def _rows(paths: list[str]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a rotated file can end mid-line
                if row.get("kind") == "packet":
                    yield row


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def main(paths: list[str], verbose: bool) -> int:
    last: dict[tuple[str, str], tuple[float, float, float]] = {}
    moves: dict[str, dict[int, list[tuple[float, float, float]]]] = {}
    skipped = defaultdict(int)

    for row in _rows(paths):
        # mStock labels its mode ``full``; Upstox's five-level feeder says
        # ``full_d5``. Any other mode (ltp, quote) carries no book to compare.
        if not str(row.get("mode") or "").startswith("full"):
            skipped["not full mode"] += 1
            continue
        ask = _positive(row.get("ask"))
        bid = _positive(row.get("bid"))
        if ask is None or bid is None:
            skipped["one-sided or blank ladder"] += 1
            continue
        mono = row.get("recv_mono")
        if not isinstance(mono, (int, float)):
            skipped["no receive clock"] += 1
            continue
        broker = str(row.get("broker") or "?")
        token = str(row.get("token") or "?")
        bucket = moves.setdefault(broker, {w: [] for w in WINDOWS_S})
        previous = last.get((broker, token))
        last[(broker, token)] = (float(mono), bid, ask)
        if previous is None:
            continue
        gap = float(mono) - previous[0]
        if gap <= 0 or gap > MAX_GAP_S:
            skipped["gap outside 30s"] += 1
            continue
        for window in WINDOWS_S:
            if gap <= window:
                bucket[window].append((abs(ask - previous[2]), previous[2], ask))
                break

    print(f"{sum(len(v) for b in moves.values() for v in b.values())} consecutive "
          f"two-sided pairs; skipped: {dict(skipped)}\n")
    for broker in sorted(moves):
        print(f"== {broker} — the same contract's own ask, {MAX_GAP_S:.0f}s apart at most ==")
        for window in WINDOWS_S:
            pairs = moves[broker][window]
            if not pairs:
                continue
            absolute = [move for move, _p, _n in pairs]
            relative = [move / prev for move, prev, _n in pairs if prev > 0]
            print(
                f"  <= {window:>2}s  n={len(absolute):<7} "
                f"p50=Rs{_percentile(absolute, 0.50):.2f} "
                f"p95=Rs{_percentile(absolute, 0.95):.2f} "
                f"p99=Rs{_percentile(absolute, 0.99):.2f} "
                f"p99.9=Rs{_percentile(absolute, 0.999):.2f} "
                f"max=Rs{max(absolute):.2f} | "
                f"pct p99={_percentile(relative, 0.99) * 100:.2f} "
                f"max={max(relative) * 100:.2f}"
            )
        if verbose:
            pairs = sorted(moves[broker].get(5, []), key=lambda p: p[0], reverse=True)
            print("  largest 5-second ask moves:")
            for move, prev, new in pairs[:10]:
                print(f"    Rs{move:>8.2f}  Rs{prev:>9.2f} -> Rs{new:>9.2f}")
    print("\nThe 2026-11-08 exit moved +Rs126.65 on a Rs367.00 contract in 5s "
          "(34.5 %). Read the max column against that number: a collar below the "
          "legitimate ceiling refuses honest exits.")
    return 0


def _positive(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args.paths, args.verbose))
