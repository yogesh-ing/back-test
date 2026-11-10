"""Show the failure mode the ladder rules cannot see, and the one that can.

A *uniformly* wrong book is self-consistent: multiply every level by 1.345 and
the asks still step up (rule A passes) and level 1 is still the ladder's own
extreme (rule B passes).  Nothing inside that packet is wrong, so no in-packet
check can refuse it -- and it is a plausible failure: a stale snapshot of the
same contract, the same book priced in points instead of rupees, or another
strike's ladder stamped on these terms.

Parity does not look inside the packet.  For one expiry ``CE - PE`` must equal
the same forward-minus-strike at every strike, so the strikes in a sweep each
vote on one number and a leg that is wrong by anything that is not the market's
own move is a dissenting vote.  Cross-broker does the same from the other side:
it names *which* broker dissents.

A what-if probe on one real recorded pass.  The injected variant is labelled as
an injection and is never written to the capture tape -- it exists to show which
detector fires, not to produce evidence.
"""

from __future__ import annotations

import json
import statistics
from bisect import bisect_left
from datetime import datetime
from zoneinfo import ZoneInfo

SHADOW = "logs/feed_depth/feed_shadow_2026-11-09.jsonl"
MSTOCK = "logs/feed_depth/feed_mstock_2026-11-09.jsonl"
IST = ZoneInfo("Asia/Kolkata")
FACTOR = 1.345  # the 2026-11-08 shape: offer 493.65 against a 367.00 last trade


def parse_time(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def shadow_rows() -> list[dict]:
    out = []
    with open(SHADOW, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                out.append(json.loads(line))
    return out


def ladders_by_token() -> dict[str, list[tuple[float, list[float], list[float], float, float]]]:
    """token -> ascending (recv, bids, asks, bid_field, ask_field) from the wire tape."""
    index: dict[str, list] = {}
    with open(MSTOCK, encoding="utf-8") as handle:
        for line in handle:
            if '"packet"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "packet":
                continue
            bids = [p for p, _q in (row.get("bids") or []) if p]
            asks = [p for p, _q in (row.get("asks") or []) if p]
            if not bids and not asks:
                continue
            index.setdefault(str(row.get("token")), []).append(
                (parse_time(row["recv_utc"]), bids, asks,
                 row.get("bid") or 0.0, row.get("ask") or 0.0))
    for value in index.values():
        value.sort(key=lambda item: item[0])
    return index


def ladder_rules(bids: list[float], asks: list[float],
                 bid_field: float, ask_field: float) -> tuple[bool, bool]:
    a = (any(asks[i] < asks[i - 1] for i in range(1, len(asks)))
         or any(bids[i] > bids[i - 1] for i in range(1, len(bids))))
    b = bool((asks and abs(ask_field - min(asks)) > 1e-9)
             or (bids and abs(bid_field - max(bids)) > 1e-9))
    return a, b


def nearest(index: dict, token: str, when: float) -> tuple | None:
    series = index.get(str(token))
    if not series:
        return None
    times = [item[0] for item in series]
    pos = bisect_left(times, when)
    best = None
    for cand in series[max(pos - 1, 0):pos + 2]:
        gap = abs(cand[0] - when)
        if gap <= 3.0 and (best is None or gap < abs(best[0] - when)):
            best = cand
    return best


def passes(rows: list[dict]) -> dict[float, dict]:
    grouped: dict[float, dict] = {}
    for row in rows:
        grouped.setdefault(round(parse_time(row["recv_utc"]) / 5) * 5.0, {})[
            (row["strike"], row["option_type"])] = row
    return grouped


def forwards(legs: dict, broker: str, inject: float | None = None) -> dict[float, float]:
    strikes = sorted({k[0] for k in legs if k[1] == "CE"}
                     & {k[0] for k in legs if k[1] == "PE"})
    out = {}
    for strike in strikes:
        ce = (legs.get((strike, "CE")) or {}).get(broker) or {}
        pe = (legs.get((strike, "PE")) or {}).get(broker) or {}
        if ce.get("ask") and pe.get("ask"):
            out[strike] = ce["ask"] - (pe["ask"] * (FACTOR if inject == strike else 1.0)) + strike
    return out


def main() -> int:
    grouped = passes(shadow_rows())
    index = ladders_by_token()

    # The 08-Nov-style case, in its own numbers, before any injection.
    print("--- 2026-11-08 14:20:16, the fill that lost Rs 11,794.25 (real, recorded) ---")
    for label, ce, pe, when in (("entry 14:20:11", 337.10, 320.85, None),
                                ("exit  14:20:16", 345.75, 493.65, None)):
        print(f"   {label}: CE {ce:8.2f}  PE {pe:8.2f}  CE-PE {ce - pe:+9.2f}   "
              f"implied forward at strike 25000 = {ce - pe + 25000.0:10.2f}")
    print("   spot was ~25,030 with ~14 days of carry at ~7%, so the forward sits near 25,100:")
    entry_off = 25100 - (337.10 - 320.85 + 25000.0)
    exit_off = 25100 - (345.75 - 493.65 + 25000.0)
    print(f"   entry implies {337.10 - 320.85 + 25000.0:.2f} (off ~{entry_off:+.0f} — "
          f"a thin opening book),  exit implies {345.75 - 493.65 + 25000.0:.2f} "
          f"(off ~{exit_off:+.0f}) — three times the entry breach, "
          f"i.e. the put was ~Rs 250 too high")

    # Now the same arithmetic on today's tape, with the error injected on purpose.
    chosen = None
    for stamp, legs in sorted(grouped.items(), key=lambda item: -item[0]):
        pairs = {k[0] for k in legs if k[1] == "CE"} & {k[0] for k in legs if k[1] == "PE"}
        if len(pairs) < 10:
            continue
        if all((legs[(s, o)].get("mstock") or {}).get("ask")
               and (legs[(s, o)].get("upstox") or {}).get("ask")
               for s in pairs for o in ("CE", "PE")):
            chosen = (stamp, legs, pairs)
            break
    if chosen is None:
        print("no pass with 10+ paired strikes and full ask coverage on both brokers")
        return 1
    stamp, legs, pairs = chosen
    real = forwards(legs, "mstock")
    target = max(real)
    wrong = forwards(legs, "mstock", inject=target)
    up = forwards(legs, "upstox")
    pe_row = legs[(target, "PE")]
    print(f"--- real recorded pass, {datetime.fromtimestamp(stamp, IST):%H:%M:%S IST}, "
          f"{len(real)} paired strikes, both brokers quoted ---")
    print(f"inject: multiply every level of {pe_row['symbol']}'s mStock book by x{FACTOR}")

    hit = nearest(index, pe_row["token"], parse_time(pe_row["recv_utc"]))
    if hit:
        _, bids, asks, bid_f, ask_f = hit
        print(f"   its real ladder from the wire tape: bids {bids} asks {asks}")
        wrong_rules = ladder_rules([round(p * FACTOR, 2) for p in bids],
                                   [round(p * FACTOR, 2) for p in asks],
                                   round(bid_f * FACTOR, 2), round(ask_f * FACTOR, 2))
        print(f"   ladder rules on the real book  : {ladder_rules(bids, asks, bid_f, ask_f)}")
        print(f"   ladder rules on the wrong book : {wrong_rules}"
              "   <- both still pass, by construction")
        print(f"   best ask {ask_f} -> {round(ask_f * FACTOR, 2)}  "
              f"while Upstox still says {(pe_row['upstox'].get('ask') or 0):.2f}")
    else:
        print("   no wire ladder within 3s for that token; using level-1 arithmetic only")

    ref = statistics.median(real.values())
    print(f"\n{'strike':>8} {'mStock fwd':>11} {'mStock fwd (wrong book)':>24} {'Upstox fwd':>11}")
    for strike in sorted(real):
        print(f"{strike:>8.0f} {real[strike]:>11.2f} {wrong[strike]:>24.2f} {up[strike]:>11.2f}"
              + ("   <-- injected" if strike == target else ""))
    tables = (("mStock real   ", real), ("mStock wrong  ", wrong), ("Upstox   ", up))
    for label, table in tables:
        vals = list(table.values())
        worst = max(vals, key=lambda v: abs(v - ref))
        print(f"{label}: forward = median {statistics.median(vals):9.2f}  "
              f"spread {max(vals) - min(vals):7.2f}  worst strike off {worst - ref:+9.2f}")
    print(f"\nthe market's own reference from this pass: {ref:.2f} "
          f"(mStock real median vs Upstox median {statistics.median(list(up.values())):.2f})")
    return 0


if __name__ == "__main__":
    main()
