"""QA cadence sampler: two spaced SSE snapshots + per-runner bar evidence."""
import json, re, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
URL = "http://127.0.0.1:5000/api/portfolio/stream"


def snapshot(max_bytes=4_000_000):
    req = urllib.request.Request(URL, headers={"Accept": "text/event-stream"})
    body = b""
    with urllib.request.urlopen(req, timeout=30) as resp:
        for raw in resp:
            body += raw
            if raw in (b"\n", b"\r\n") and b"data: " in body:
                break
            if len(body) > max_bytes:
                break
    best = None
    for ev in re.split(r"\n\n+", body.decode("utf-8", "replace")):
        m = re.search(r"^data: (.*)$", ev, re.M)
        if not m:
            continue
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("runners"):
            best = d
    return best


def index(snap):
    return {r["instance_id"]: r for r in snap["runners"]}


def main():
    gap = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    a = snapshot()
    ta = datetime.now(IST)
    if a is None:
        print("no snapshot"); return 1
    time.sleep(gap)
    b = snapshot()
    tb = datetime.now(IST)
    if b is None:
        print("no second snapshot"); return 1
    ia, ib = index(a), index(b)
    mins = (tb - ta).total_seconds() / 60.0
    print(f"A @ {ta:%H:%M:%S} IST   B @ {tb:%H:%M:%S} IST   gap={mins:.2f} min")
    hdr = f"{'runner':34s} {'tf':6s} {'barsA':>6s} {'barsB':>6s} {'dB':>4s} {'per-min':>8s} {'expected':>9s} {'lastBarA':>9s} {'lastBarB':>9s} {'ok?':>5s}"
    print(hdr)
    for iid, ra in ia.items():
        rb = ib.get(iid)
        if not rb:
            continue
        db = rb["bars_processed"] - ra["bars_processed"]
        per_min = db / mins
        tfm = {"1min": 1, "5min": 5, "15min": 15, "1hour": 60}.get(str(ra["timeframe"]), 1)
        expected = mins / tfm
        la = (ra.get("last_bar_ts") or "")[-8:]
        lb = (rb.get("last_bar_ts") or "")[-8:]
        verdict = "OK" if abs(db - expected) <= max(1.0, expected * 0.6) else "VIOLATION"
        print(f"{ra['name'][:34]:34s} {str(ra['timeframe']):6s} {ra['bars_processed']:6d} {rb['bars_processed']:6d} {db:4d} {per_min:8.2f} {expected:9.2f} {la:>9s} {lb:>9s} {verdict:>5s}")
    print()
    for k in ("total_capital", "total_equity", "deployed_capital", "daily_pnl", "realized_pnl",
              "open_positions", "fill_count", "order_count", "bar_events", "stale_marks_count"):
        print(f"{k:20s} A={a.get(k)}  B={b.get(k)}")
    json.dump({"A": a, "B": b}, open(r"C:/learning/back-test/tools/out/QA_CADENCE_RAW.json", "w", encoding="utf-8"), default=str)
    return 0


sys.exit(main())
