"""T0.5-1 — which timestamp stamp does what (impl plan Phase 0, measurement 1).

Per recorded frame, three stamps exist on the tape (T0.5-0 widened the
recorder's whitelist to keep them all):

  * ``recv_utc``      — our receipt clock (the recorder's own ``iso_utc``);
  * ``exchange_ts``   — the frame's ``currentTs``, the exchange's frame clock;
  * ``last_trade_ts`` — ``ltpc.ltt``, the clock of the last PRINT (trade).

The measurement answers, without a threshold beyond the plan's own ±0.4 s
receipt band (§1b, the measured transport agreement): which stamp tracks
**receipt** and which tracks **exchange prints**. ``serve_ws_row``'s
wall-clock fallback may only trust a stamp whose job the tape proved.

Print-lock is measured, not assumed: a print stamp moves when ``ltp``
moves and holds when ``ltp`` holds; a frame clock moves every frame. The
verdict compares the two stamps' scores against each other — no
hand-picked cutoff (junk-ask incident §6).

Read-only, stdlib-only; never imports ``backtest``.

    python tools/out/measure_stamp_sources.py 2026-11-12 --broker upstox
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

RECEIPT_BAND_S = 0.4  # §1b: receipt vs exchange offset agrees to ±0.4 s


def _parse_utc(value):
    try:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        stamp = datetime.fromisoformat(text)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def scan(path):
    """One pass over the day's packet rows: receipt-band and print-lock
    scores for each stamp, plus the raw movement counts."""
    frames = 0
    missing = {"exchange_ts": 0, "last_trade_ts": 0, "recv_utc": 0}
    band = {"exchange_ts": 0, "last_trade_ts": 0}
    # print-lock: (moved ∨ held) matching the ltp change, counted per stamp
    ltp_moved = {"exchange_ts": [0, 0], "last_trade_ts": [0, 0]}   # [match, n]
    ltp_held = {"exchange_ts": [0, 0], "last_trade_ts": [0, 0]}    # [match, n]
    prev = {"exchange_ts": None, "last_trade_ts": None, "ltp": None}
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
            frames += 1
            stamps = {}
            for key in ("exchange_ts", "last_trade_ts", "recv_utc"):
                stamps[key] = _parse_utc(row.get(key))
                if stamps[key] is None:
                    missing[key] += 1
            recv = stamps["recv_utc"]
            if recv is not None:
                for key in ("exchange_ts", "last_trade_ts"):
                    if stamps[key] is not None and abs(
                        (stamps[key] - recv).total_seconds()
                    ) <= RECEIPT_BAND_S:
                        band[key] += 1
            ltp = row.get("ltp")
            for key in ("exchange_ts", "last_trade_ts"):
                if prev[key] is not None and stamps[key] is not None \
                        and prev["ltp"] is not None and ltp is not None:
                    moved = stamps[key] != prev[key]
                    if ltp != prev["ltp"]:
                        ltp_moved[key][1] += 1
                        ltp_moved[key][0] += int(moved)
                    elif ltp == prev["ltp"]:
                        ltp_held[key][1] += 1
                        ltp_held[key][0] += int(not moved)
                if stamps[key] is not None:
                    prev[key] = stamps[key]
            if ltp is not None:
                prev["ltp"] = ltp
    return {
        "frames": frames,
        "missing": missing,
        "band": band,
        "ltp_moved": ltp_moved,
        "ltp_held": ltp_held,
    }


def _print_lock(key, stats):
    moved_n, moved_d = stats["ltp_moved"][key]
    held_n, held_d = stats["ltp_held"][key]
    n = moved_d + held_d
    return (moved_n + held_n) / n if n else None


def report(day, broker, path, out_path=None):
    if not os.path.exists(path):
        print(f"=== {broker}: no tape at {path}")
        return 1
    s = scan(path)
    frames = max(1, s["frames"])
    lines = [f"=== stamp sources {day} {broker}  ({s['frames']} frames, {path})"]
    for key in ("exchange_ts", "last_trade_ts"):
        lock = _print_lock(key, s)
        lock_text = f"{100.0 * lock:.1f}%" if lock is not None else "n/a"
        lines.append(
            f"   {key:<14} receipt-band {s['band'][key]:>7}/{s['frames']:<7} "
            f"({100.0 * s['band'][key] / frames:5.1f}%)  print-lock {lock_text}"
        )
        if s["missing"][key]:
            lines.append(
                f"      (no {key} on {s['missing'][key]} frames — a tape from "
                f"before T0.5-0 cannot answer this)"
            )
    lines.append(f"   recv_utc missing on {s['missing']['recv_utc']} frames")

    # The verdict: comparison between the two stamps, not a cutoff.
    verdict = []
    for label, higher in (
        ("tracks receipt (±0.4 s band)", lambda k: s["band"][k]),
        ("tracks exchange prints", lambda k: _print_lock(k, s) or -1.0),
    ):
        scores = {k: higher(k) for k in ("exchange_ts", "last_trade_ts")}
        if scores["exchange_ts"] == scores["last_trade_ts"]:
            verdict.append(f"   verdict: UNCLASSIFIED for '{label}' (tied/no data)")
        else:
            winner = max(scores, key=scores.get)
            verdict.append(f"   verdict: {winner} {label}")
    lines.extend(verdict)
    lines.append(
        "   serve_ws_row's wall-clock fallback may trust only a stamp the "
        "tape proved above."
    )
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"   (wrote {out_path})")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("day", help="session date, e.g. 2026-11-12")
    parser.add_argument("--broker", default="upstox")
    parser.add_argument("--dir", default=os.path.join("logs", "feed_depth"))
    parser.add_argument("--out", default="",
                        help="defaults to tools/out/stamps_<date>.txt")
    args = parser.parse_args(argv[1:])
    path = os.path.join(args.dir, f"feed_{args.broker}_{args.day}.jsonl")
    out = args.out or os.path.join("tools", "out", f"stamps_{args.day}.txt")
    return report(args.day, args.broker, path, out)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
