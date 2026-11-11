"""Measure a broker's own print cadence and clock, from the ENABLE_FEED depth tape.

WHY THIS EXISTS
The quote-freshness ceilings in the serve gate were calibrated on mStock's print
cadence: a NIFTY option leg that is genuinely quiet still gets a frame every few
seconds, so "no frame for 10 s" means the socket, not the contract. The same
number copied onto a different venue is a guess. This script produces the number
instead of guessing it, per broker, from the packets the venue actually sent.

WHAT IT ANSWERS
  frames/token   how many packets the venue published per subscribed contract
  gap p50/p90/p99 consecutive-frame interval for one contract (the cadence)
  book coverage  two-sided vs one-sided vs no book at all, per venue. A venue that
                 answers half the day with nothing to cross cannot be primary.
  clock skew     feed's own ``exchange_ts`` minus our ``recv_utc``, per frame:
                 a venue stamping exchange time gives a stable negative offset
                 plus a sawtooth; a venue stamping its own server clock gives a
                 tight, always-positive, small distribution. This is how the
                 two-clock gate decides which stamp it may trust.
  stamp taxonomy how many frames carry a real stamp at all. mStock sends a large
                 share of Go-zero sentinels (``1980-01-01``) and naive
                 exchange-time strings with no tz door; a venue like that cannot
                 be the second clock, and the gate must say so rather than print
                 a skew percentile made of sentinels.

Read-only, stdlib-only; never imports ``backtest``.

    python tools/out/measure_feed_cadence.py 2026-11-11
    python tools/out/measure_feed_cadence.py 2026-11-11 --broker upstox -v
"""

from __future__ import annotations

from collections import defaultdict
import json
import os
import sys
from datetime import datetime, timedelta, timezone

MAX_GAP_S = 120.0  # a longer silence is a session gap, not a print interval
QUIET_TOKENS = 40  # only report tokens with at least this many frames
MILLENNIUM_EPOCH_S = 946_684_800.0  # 2000-01-01 UTC; below this the stamp is a sentinel
# The exchange wall clock, used ONLY to read a venue stamp that arrives with no
# offset declared. This tool must not import backtest.timekeeping (stdlib-only
# rule), so the offset is spelled out here rather than borrowed from the app.
EXCHANGE_OFFSET = timezone(timedelta(hours=5, minutes=30))


def _pct(values, q):
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _parse_utc(stamp):
    """Our own receipt stamp: aware UTC as written by ``iso_utc()``."""
    if stamp in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:  # ours are never naive; assume UTC if they are
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _venue_stamp(raw):
    """Classify one venue stamp -> (kind, epoch_seconds_or_None).

    ``sentinel`` is a Go-zero date the venue never filled in, ``tzless`` is a
    plausible datetime with no declared offset (read here as exchange wall time),
    ``aware`` carries its own clock, ``ms`` is an epoch-milliseconds integer.
    """
    if raw in (None, "", 0):
        return "missing", None
    if isinstance(raw, (int, float)):
        secs = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
        return ("ms" if secs >= MILLENNIUM_EPOCH_S else "sentinel"), secs
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:19])
        except ValueError:
            return "unparseable", None
    if parsed.tzinfo is None:
        secs = parsed.replace(tzinfo=EXCHANGE_OFFSET).timestamp()
        return ("sentinel" if secs < MILLENNIUM_EPOCH_S else "tzless"), secs
    return "aware", parsed.timestamp()


def scan(path, verbose=False):
    per_token = defaultdict(list)
    token_frames = defaultdict(int)
    skew_aware = []
    skew_tzless = []
    stamp_kinds = defaultdict(int)
    sentinel_no_book = 0
    frames = 0
    empty = 0
    one_sided = 0
    two_sided = 0
    kinds = defaultdict(int)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if '"kind"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            kinds[str(row.get("kind"))] += 1
            if row.get("kind") != "packet":
                continue
            frames += 1
            token = str(row.get("token"))
            recv = _parse_utc(row.get("recv_utc"))
            bids = row.get("bids") or []
            asks = row.get("asks") or []
            no_book_this_frame = False
            if bids and asks and row.get("bid") and row.get("ask"):
                two_sided += 1
            elif bids or asks or row.get("bid") or row.get("ask"):
                one_sided += 1
            else:
                empty += 1
                no_book_this_frame = True
            if recv is not None:
                per_token[token].append(recv)
            token_frames[token] += 1
            kind, theirs = _venue_stamp(row.get("exchange_ts", row.get("timestamp")))
            stamp_kinds[kind] += 1
            if kind == "sentinel" and no_book_this_frame:
                sentinel_no_book += 1
            if recv is None or theirs is None or kind not in ("aware", "ms", "tzless"):
                continue
            if kind == "tzless":
                skew_tzless.append(recv - theirs)
            else:
                skew_aware.append(recv - theirs)

    gaps = []
    per_token_gaps = {}
    for token, times in per_token.items():
        times.sort()
        local = [
            b - a
            for a, b in zip(times, times[1:])
            if 0.0 <= b - a <= MAX_GAP_S
        ]
        if len(times) >= QUIET_TOKENS:
            per_token_gaps[token] = local
        gaps.extend(local)

    out = {
        "frames": frames,
        "tokens": len(per_token),
        "empty": empty,
        "one_sided": one_sided,
        "two_sided": two_sided,
        "kinds": dict(kinds),
        "stamp_kinds": dict(stamp_kinds),
        "sentinel_and_no_book": sentinel_no_book,
        "gap_p50": _pct(gaps, 0.50),
        "gap_p90": _pct(gaps, 0.90),
        "gap_p99": _pct(gaps, 0.99),
        "gap_max": max(gaps) if gaps else float("nan"),
        "n_gaps": len(gaps),
        "frames_per_token_min": min(token_frames.values()) if token_frames else 0,
        "frames_per_token_max": max(token_frames.values()) if token_frames else 0,
        "subscribed_tokens": len(token_frames),
        "skew_p05": _pct(skew_aware, 0.05),
        "skew_p50": _pct(skew_aware, 0.50),
        "skew_p95": _pct(skew_aware, 0.95),
        "skew_min": min(skew_aware) if skew_aware else float("nan"),
        "skew_max": max(skew_aware) if skew_aware else float("nan"),
        "n_skew": len(skew_aware),
        "tzless_p05": _pct(skew_tzless, 0.05),
        "tzless_p50": _pct(skew_tzless, 0.50),
        "tzless_p95": _pct(skew_tzless, 0.95),
        "n_tzless": len(skew_tzless),
        "per_token_gaps": per_token_gaps,
    }
    if verbose:
        worst = sorted(
            (t for t in per_token_gaps if per_token_gaps[t]),
            key=lambda t: -max(per_token_gaps[t]),
        )[:10]
        for token in worst:
            g = per_token_gaps[token]
            print(f"   token {token}: frames={len(g) + 1} p99={_pct(g, 0.99):.1f}s "
                  f"max={max(g):.1f}s")
    return out


def main(argv):
    day = argv[1] if len(argv) > 1 else "2026-11-11"
    brokers = ["mstock", "upstox"]
    if "--broker" in argv:
        brokers = [argv[argv.index("--broker") + 1]]
    verbose = "-v" in argv
    root = os.getcwd()
    for broker in brokers:
        path = os.path.join(root, "logs", "feed_depth", f"feed_{broker}_{day}.jsonl")
        if not os.path.exists(path):
            print(f"=== {broker}: no tape at {path}")
            continue
        mb = os.path.getsize(path) / 1e6
        s = scan(path, verbose=verbose)
        print(f"\n=== {broker} {day}  ({mb:.0f} MB, {s['frames']} packets, "
              f"{s['tokens']} tokens, kinds={s['kinds']})")
        print(f"   two-sided book   {s['two_sided']}  "
              f"({100.0 * s['two_sided'] / max(1, s['frames']):.2f}%)")
        print(f"   one-sided        {s['one_sided']}  "
              f"({100.0 * s['one_sided'] / max(1, s['frames']):.2f}%)")
        print(f"   no book at all   {s['empty']}  "
              f"({100.0 * s['empty'] / max(1, s['frames']):.2f}%)")
        print(f"   frame gap   p50={s['gap_p50']:.2f}s p90={s['gap_p90']:.2f}s "
              f"p99={s['gap_p99']:.2f}s max={s['gap_max']:.1f}s  n={s['n_gaps']}")
        print(f"   tokens={s['subscribed_tokens']} "
              f"frames/token min={s['frames_per_token_min']} "
              f"max={s['frames_per_token_max']}")
        leg_p99 = sorted(
            (_pct(g, 0.99), max(g), t)
            for t, g in s["per_token_gaps"].items() if g)
        if leg_p99:
            # The aggregate above is dominated by the underlying's index frames,
            # which print many times a second. The serve gate ages a leg, so
            # report the leg-level distribution: median p99 across legs, and the
            # quietest leg in the session.
            vals = [v for v, _, _ in leg_p99]
            print(f"   per-LEG p99 gap: median={_pct(vals, 0.50):.2f}s "
                  f"p90={_pct(vals, 0.90):.2f}s worst={vals[-1]:.1f}s "
                  f"(n={len(vals)} legs); quietest leg max={leg_p99[-1][1]:.1f}s")
        total = max(1, s["frames"])
        print("   venue stamp kinds: " + ", ".join(
            f"{k}={v} ({100.0 * v / total:.2f}%)"
            for k, v in sorted(s["stamp_kinds"].items(), key=lambda kv: -kv[1])))
        if s["stamp_kinds"].get("sentinel"):
            print(f"   sentinels that ALSO carried no book: "
                  f"{s['sentinel_and_no_book']} of {s['stamp_kinds']['sentinel']} "
                  "— a frame with nothing to cross is the frame with no clock")
        print(f"   recv_utc - venue timestamp (offset-bearing stamps only): "
              f"p05={s['skew_p05']:.3f}s "
              f"p50={s['skew_p50']:.3f}s p95={s['skew_p95']:.3f}s "
              f"min={s['skew_min']:.3f}s max={s['skew_max']:.3f}s  n={s['n_skew']}")
        if s["n_tzless"]:
            print(f"   tz-less venue strings, read as exchange wall clock — skew "
                  f"p05={s['tzless_p05']:.3f}s p50={s['tzless_p50']:.3f}s "
                  f"p95={s['tzless_p95']:.3f}s n={s['n_tzless']}"
                  " (usable only if this box's clock is the exchange clock)")
        if s["n_skew"] == 0:
            print("   (no offset-bearing venue stamps — the two-clock gate has no "
                  "second clock from this venue; recv_mono only)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
