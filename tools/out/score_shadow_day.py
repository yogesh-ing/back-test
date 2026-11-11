"""Score one shadow day against the S1-S7 scorecard (plan §5 Phase 4).

Every criterion is reported as a count with its denominator — a verdict is only
available for a quantity measured on both sides. The definitions are pinned
HERE, before the bell, so the scorecard cannot be assembled post-hoc:

  S1  frames served with a two-sided book. Coverage := ``two_sided / frames``
      in the cadence vocabulary (two_sided / one_sided / no_book — the shared
      vocabulary plan-review §10.4 fixed). Primary must reach the mStock
      baseline of the SAME session tape. Both sides computed by
      ``measure_feed_cadence.py``'s scan (imported, not retyped).
  S2  structurally impossible ladders published by the primary := 0, counted
      by ``count_impossible_ladders.py``'s scan (imported, not retyped).
  S3  fill-seam refusals attributable to the VENUE, not to an empty book.
      Population: ``sanity_flag`` rows with mode=enforce (a refusal that
      actually blocked). A row whose packet carried ANY level is attributable
      to the venue (the venue printed something the seam refused); a row with
      nothing at all is an honest empty-book refusal and allowed. venue := 0.
  S4  L1 vetoes by the witness (``sanity_flag`` rows with
      reason=cross_broker_l1 — the vote the corroboration gate counts,
      whatever its mode). Split threshold-free, by proof only:
      "garbage" = the row's own ladder is structurally impossible;
      "venue basis" = the print is self-consistent and the venues genuinely
      differ; "unexplained" = nothing to classify. unexplained := 0.
  S5  collar decisions journaling a peer mark := 100 %. The journal's
      ``peer_mark`` rows are per evaluation; the independent denominator is
      the session log's ``EXIT_COLLAR_*`` lines (pass ``--collar-log``).
      Without the log the journal counts print as provisional.
  S6  primary-feed-loss alert fires on socket drop; witness-only loss does
      not. Not a tape quantity — the evidence commands are printed.
  S7  fills that would have priced differently on the other venue := the
      ``peer_mark`` rows where the peer's SAME-SIDE level (ask for a BUY
      cover, bid for a SELL) differs from the fill price — ANY difference
      counts, no threshold at n=1. Listed with both marks.

Read-only, stdlib-only; never imports ``backtest``.

    python tools/out/score_shadow_day.py 2026-11-12
    python tools/out/score_shadow_day.py 2026-11-12 --primary upstox \\
        --witness mstock --collar-log logs/options_paper.log
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

EPS = 1e-9


def _load_sibling(name):
    """Import one of the sibling tape tools by file path (same directory)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    spec = importlib.util.spec_from_file_location(f"_tool_{name[:-3]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_kind(paths, kind):
    """All rows of one recorder kind across the day's tape files."""
    rows = []
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if f'"{kind}"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("kind") == kind:
                    rows.append(row)
    return rows


def _ladder_impossible(row):
    """The S2 structural proof, applied to a recorder row's packet fields."""
    bids = [p for p, _ in row.get("bids") or []]
    asks = [p for p, _ in row.get("asks") or []]
    if not bids or not asks:
        return False
    a_bad = any(asks[i] < asks[i - 1] - EPS for i in range(1, len(asks)))
    b_bad = any(bids[i] > bids[i - 1] + EPS for i in range(1, len(bids)))
    crossed = asks[0] < bids[0] - EPS
    return bool(a_bad or b_bad or crossed)


def _row_has_any_level(row):
    return bool(
        (row.get("bids") or []) or (row.get("asks") or [])
        or row.get("bid") or row.get("ask")
    )


def _peer_same_side_price(row):
    """The peer's counterfactual price for this exit — same side, not the
    other touch (a cover buys at the ask; the peer's bid is not the price the
    fill would have taken there)."""
    peer = row.get("peer") or {}
    side = str(row.get("side") or "").strip().upper()
    key = "ask" if side == "BUY" else "bid" if side == "SELL" else ""
    if not key:
        return None, ""
    try:
        price = float(peer.get(key) or 0.0)
    except (TypeError, ValueError):
        return None, key
    return (price if price > 0 else None), key


def _row(label, count, denom, verdict):
    pct = 100.0 * count / max(1, denom)
    print(f"   {label:<38} {count:>6} / {denom:<6} ({pct:5.1f}%)  {verdict}")


def score(day, primary, witness, directory, collar_log):
    day_paths = lambda name: [  # noqa: E731
        os.path.join(directory, f"feed_{name}_{day}.jsonl")
    ]
    all_paths = sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.startswith("feed_") and f.endswith(f"_{day}.jsonl")
    ) if os.path.isdir(directory) else []

    cadence = _load_sibling("measure_feed_cadence.py")
    ladders = _load_sibling("count_impossible_ladders.py")

    print(f"=== shadow-day scorecard {day}  "
          f"(primary={primary}, witness={witness})")
    print(f"=== tapes: {directory}")

    # S1 — coverage in the shared vocabulary, both sides of the same session.
    print("\n[S1] frames served with a two-sided book  "
          "(coverage := two_sided / frames, cadence vocabulary)")
    baseline = primary_value = None
    for venue in (primary, witness):
        path = day_paths(venue)[0]
        if not os.path.exists(path):
            _row(venue, 0, 0, "NO TAPE — criterion cannot be judged")
            continue
        s = cadence.scan(path)
        frames, two = s["frames"], s["two_sided"]
        _row(f"{venue}: two_sided/frames", two, frames,
             f"one_sided={s['one_sided']} no_book={s['empty']}")
        if venue == primary:
            primary_value = two / max(1, frames)
        else:
            baseline = two / max(1, frames)
    if primary_value is not None and baseline is not None:
        verdict = "PASS" if primary_value >= baseline else "FAIL"
        print(f"   verdict: primary {primary_value:.3f} vs "
              f"{witness} baseline {baseline:.3f} -> {verdict}")
    else:
        print("   verdict: NOT JUDGABLE (missing tape)")

    # S2 — the structural proof on the primary's own packets.
    print("\n[S2] structurally impossible ladders published by the primary "
          "(target 0)")
    path = day_paths(primary)[0]
    if os.path.exists(path):
        stats, _worst = ladders.scan(path)
        _row("impossible_packets", stats["impossible_packets"],
             stats["packets"], "target 0")
        _row("one_sided (context)", stats["one_sided"], stats["packets"], "")
        _row("no_book (context)", stats["no_book"], stats["packets"], "")
    else:
        print("   NO TAPE — criterion cannot be judged")

    # S3 — refusals the venue caused vs honest empty-book refusals.
    print("\n[S3] fill-seam refusals attributable to the venue (target 0)")
    flags = _read_kind(all_paths, "sanity_flag")
    enforced = [r for r in flags if str(r.get("mode")) == "enforce"]
    venue_refusals = [r for r in enforced if _row_has_any_level(r)]
    empty_refusals = [r for r in enforced if not _row_has_any_level(r)]
    _row("venue-attributable", len(venue_refusals), len(enforced), "target 0")
    _row("empty book (allowed)", len(empty_refusals), len(enforced), "")
    for row in venue_refusals[:10]:
        print(f"      {row.get('recv_utc')} tok={row.get('token')} "
              f"reason={row.get('reason')} detail={row.get('detail')}")

    # S4 — the witness's L1 votes, split by proof (no thresholds).
    print("\n[S4] L1 vetoes by the witness (reason=cross_broker_l1) split")
    vetoes = [r for r in flags if r.get("reason") == "cross_broker_l1"]
    garbage = [r for r in vetoes if _ladder_impossible(r)]
    unexplained = [r for r in vetoes if not _row_has_any_level(r)]
    venue_basis = [r for r in vetoes
                   if r not in garbage and r not in unexplained]
    _row("venue basis (venues differ)", len(venue_basis), len(vetoes), "")
    _row("garbage (own ladder impossible)", len(garbage), len(vetoes), "")
    _row("unexplained", len(unexplained), len(vetoes), "target 0")

    # S5 — every collar decision carries a peer mark.
    print("\n[S5] collar decisions journaling a peer mark (target 100%)")
    marks = _read_kind(all_paths, "peer_mark")
    trips = [r for r in marks if r.get("decision") == "trip"]
    _row("journal rows (ok+trip)", len(marks), len(marks), "per evaluation")
    if collar_log and os.path.exists(collar_log):
        with open(collar_log, encoding="utf-8") as handle:
            logged = sum(
                1 for line in handle
                if "EXIT_COLLAR_SHADOW" in line or "EXIT_COLLAR_REFUSED" in line
            )
        _row("trips journaled / trips logged", len(trips), logged,
             "target 100%")
    else:
        print("   journal trips (provisional — pass --collar-log for the "
              f"independent denominator): {len(trips)}")

    # S6 — not a tape quantity.
    print("\n[S6] primary-feed-loss alert (evidence commands, not a tape count)")
    print("   1. PYTHONPATH=src python -m pytest tests/test_feed_loss_alert.py")
    print("   2. grep the day's alert store for "
          "'PRIMARY feed loss' / 'FEED_LOSS_WITNESS'")
    print("   fires on primary socket drop; witness-only loss logs a line only")

    # S7 — the paired marks, no threshold.
    print("\n[S7] fills that would have priced differently on the other venue")
    with_peer = [r for r in marks if r.get("peer")]
    differ = []
    for row in with_peer:
        price, key = _peer_same_side_price(row)
        fill = row.get("fill_price")
        if price is None or fill is None:
            continue
        if abs(price - float(fill)) > 0:
            differ.append((row, key, price))
    _row("would have priced differently", len(differ), len(with_peer),
         "listed with both marks (no threshold)")
    for row, key, price in differ[:10]:
        print(f"      {row.get('recv_utc')} {row.get('symbol')} "
              f"side={row.get('side')} our_fill={row.get('fill_price')} "
              f"our_mark={row.get('mark')} peer_{key}={price} "
              f"peer_ltp={(row.get('peer') or {}).get('ltp')}")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("day", help="session date, e.g. 2026-11-12")
    parser.add_argument("--primary", default="upstox")
    parser.add_argument("--witness", default="mstock")
    parser.add_argument("--dir", default=os.path.join("logs", "feed_depth"))
    parser.add_argument("--collar-log", default="",
                        help="the session's app log: the independent "
                             "EXIT_COLLAR_* denominator for S5")
    args = parser.parse_args(argv[1:])
    return score(args.day, args.primary, args.witness, args.dir,
                 args.collar_log)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
