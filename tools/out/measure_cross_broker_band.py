"""Measure the cross-broker L1 disagreement from the ``shadow_diff`` tape.

This is the file the band for ``cross_broker_l1`` corroboration (and, later, the
width of the protective-exit collar) must come from — the architect's rule is
thresholds out of A's capture, never hand-picked
(``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`` §6).

Run it on whatever sessions have dual-broker rows:

    PYTHONPATH=src python tools/out/measure_cross_broker_band.py \
        logs/feed_depth/feed_shadow_2026-11-09.jsonl

Two things decide the answer, and both are printed: how tight the agreement is
when BOTH brokers are fresh (the noise floor a band must sit above), and how much
of the tape is abstention — a peer that is missing, one-sided or hours old is not
a witness, and a gate built on those rows would refuse the market.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections import defaultdict
from typing import Any, Optional

#: Peer ages this script treats as "still a witness". A row older than the whole
#: trading session is a cached book from another day, not a disagreement.
FRESH_S = 10.0
#: Peer-price floors the sample is cut at, so a band can be judged per scale.
#: A rupee band that is right on a ₹400 premium is wrong on a ₹3 one, which is
#: why the rule is ``max(floor, pct * peer)`` and both halves need measuring.
BUCKET_FLOORS = (0.0, 5.0, 10.0, 25.0, 50.0, 100.0, 200.0, 400.0)
CANDIDATE_RULES = ((2.0, 0.05), (5.0, 0.05), (5.0, 0.02), (10.0, 0.02),
                   (10.0, 0.05), (20.0, 0.02), (10.0, 0.10), (2.0, 0.02))


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _age(row: dict) -> Optional[float]:
    try:
        return float(row.get("age_s"))
    except (TypeError, ValueError):
        return None


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]
    return {
        "p50": at(0.50), "p90": at(0.90), "p95": at(0.95), "p99": at(0.99),
        "p99.9": at(0.999), "max": ordered[-1], "n": len(ordered),
    }


def main(paths: list[str], verbose: bool) -> int:
    absolute: dict[str, list[float]] = defaultdict(list)
    scaled: dict[str, list[tuple[float, float]]] = defaultdict(list)
    coverage = Counter()
    worst: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    stale_pairs: dict[str, list[float]] = defaultdict(list)

    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("kind") != "shadow_diff":
                    continue
                # Roles first (Phase-3 schema: primary/secondary + venue
                # names); the literal pair is the legacy-tape fallback.
                primary = row.get("primary") or row.get("mstock") or {}
                secondary = row.get("secondary") or row.get("upstox") or {}
                if not _num(primary.get("ltp")) and not _num(primary.get("bid")):
                    coverage["primary_blank"] += 1
                    continue
                if not _num(secondary.get("ltp")) and not _num(secondary.get("bid")):
                    coverage["secondary_blank"] += 1
                    continue
                p_age, s_age = _age(primary), _age(secondary)
                if p_age is None or s_age is None:
                    coverage["unageable"] += 1
                    continue
                if max(p_age, s_age) > FRESH_S:
                    coverage[f"stale>{int(FRESH_S)}s"] += 1
                    for side in ("bid", "ask"):
                        a, b = _num(primary.get(side)), _num(secondary.get(side))
                        if a and b:
                            stale_pairs[side].append(abs(a - b))
                    continue
                coverage["compared"] += 1
                for side in ("bid", "ask"):
                    key = f"mstock:{side}"
                    a, b = _num(primary.get(side)), _num(secondary.get(side))
                    if a is None or b is None:
                        coverage[f"{key}:one_sided"] += 1
                        continue
                    deviation = abs(a - b)
                    absolute[key].append(deviation)
                    scaled[key].append((deviation, b))
                    coverage[f"{key}:priced"] += 1
                    worst[key].append((deviation, row))

    print(f"rows read from {len(paths)} file(s); coverage:")
    for key in sorted(coverage):
        print(f"  {key:<28} {coverage[key]:>8}")

    print(f"\nboth-fresh (<= {FRESH_S:.0f}s) disagreement, per side:")
    for key in sorted(absolute):
        abs_pct = _percentiles(absolute[key])
        rel_pct = _percentiles([dev / peer for dev, peer in scaled[key]])
        if not abs_pct:
            continue
        print(
            f"  {key:<14} n={abs_pct['n']:<7} ₹: p50={abs_pct['p50']:.2f} "
            f"p95={abs_pct['p95']:.2f} p99={abs_pct['p99']:.2f} "
            f"p99.9={abs_pct['p99.9']:.2f} max={abs_pct['max']:.2f} | "
            f"pct: p95={rel_pct['p95'] * 100:.2f} p99={rel_pct['p99'] * 100:.2f} "
            f"max={rel_pct['max'] * 100:.1f}"
        )

    print("\nnoise floor, stratified by the peer's own price (both brokers fresh):")
    print(f"  {'peer Rs band':<16}{'side':<13}{'n':>7}{'p50':>8}{'p95':>8}{'p99':>9}{'max':>10}")
    for key in ("mstock:ask", "mstock:bid"):
        for index, low in enumerate(BUCKET_FLOORS):
            high = BUCKET_FLOORS[index + 1] if index + 1 < len(BUCKET_FLOORS) else float("inf")
            values = [dev for dev, peer in scaled[key] if low <= peer < high]
            if not values:
                continue
            pct = _percentiles(values)
            label = f"{low:.0f}-{high:.0f}" if high != float("inf") else f"{low:.0f}+"
            print(f"  {label:<16}{key:<13}{pct['n']:>7}{pct['p50']:>8.2f}"
                  f"{pct['p95']:>8.2f}{pct['p99']:>9.2f}{pct['max']:>10.2f}")

    print("\nwhat the rule dev > max(Rs floor, pct * peer) would flag (both fresh):")
    print(f"  {'floor / pct':<16}{'mstock:ask':>16}{'mstock:bid':>16}")
    for floor, pct in CANDIDATE_RULES:
        label = f"Rs{floor:.0f} / {pct * 100:.0f}%"
        cells = []
        for key in ("mstock:ask", "mstock:bid"):
            hits = sum(1 for dev, peer in scaled[key] if dev > max(floor, pct * peer))
            cells.append(f"{hits} ({hits / max(1, len(scaled[key])) * 100:.3f}%)")
        print(f"  {label:<16}{cells[0]:>16}{cells[1]:>16}")

    print("\nstale-peer comparison (NOT a witness — what a gate without an age")
    print("condition would have compared against):")
    for key in sorted(stale_pairs):
        pct = _percentiles(stale_pairs[key])
        print(f"  {key:<14} n={pct.get('n', 0):<7} p50=₹{pct.get('p50', 0):.2f} "
              f"max=₹{pct.get('max', 0):.2f}")

    if verbose:
        for key in sorted(worst):
            print(f"\nworst {key} disagreements:")
            ranked = sorted(worst[key], key=lambda item: item[0], reverse=True)
            for deviation, row in ranked[:15]:
                primary = row.get("primary") or row["mstock"]
                secondary = row.get("secondary") or row["upstox"]
                side = key.split(":")[1]
                print(
                    f"  {str(row.get('symbol')):<24} D=₹{deviation:>8.2f} "
                    f"m_{side}={primary.get(side)!s:>10} "
                    f"u_{side}={secondary.get(side)!s:>10} "
                    f"m_ltp={primary.get('ltp')!s:>9} u_ltp={secondary.get('ltp')!s:>9} "
                    f"ages=({primary.get('age_s')},{secondary.get('age_s')}) "
                    f"status=({primary.get('ladder_status')},{secondary.get('ladder_status')})"
                )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args.paths, args.verbose))
