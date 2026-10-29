"""Sample the SSE headline fields vs the bucket sums every 5 min until 15:32 IST.

Purpose: watch BUG-03's capital drift grow (or not) over the session, so the
over-report can be shown to be monotonic accumulator drift rather than a
one-off arithmetic slip.
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
BASE = "http://127.0.0.1:5000"
OUT = "tools/out/QA_CAPITAL_DRIFT.jsonl"


def snapshot():
    req = urllib.request.Request(BASE + "/api/portfolio/stream",
                                 headers={"Accept": "text/event-stream"})
    body = b""
    with urllib.request.urlopen(req, timeout=30) as resp:
        for raw in resp:
            body += raw
            if raw in (b"\n", b"\r\n") and b"data: " in body:
                break
    return json.loads(body.decode("utf-8").split("data:", 1)[1].strip())


def main():
    while datetime.now(IST) < datetime(2026, 10, 28, 15, 32, tzinfo=IST):
        try:
            p = snapshot()
            buckets = p.get("buckets") or {}
            bsum = sum((b.get("capital") or 0) for b in buckets.values())
            esum = sum((b.get("equity") or 0) for b in buckets.values())
            runners = p.get("runners") or []
            rsum = sum(r.get("allocated_capital") or 0 for r in runners)
            row = {
                "ist": datetime.now(IST).isoformat(timespec="seconds"),
                "total_capital": p.get("total_capital"),
                "bucket_capital_sum": bsum,
                "runner_capital_sum": rsum,
                "total_equity": p.get("total_equity"),
                "bucket_equity_sum": esum,
                "deployed_capital": p.get("deployed_capital"),
                "daily_pnl": p.get("daily_pnl"),
                "realized_pnl": p.get("realized_pnl"),
                "open_pnl_sum": round(sum(r.get("open_pnl") or 0 for r in runners), 2),
                "daily_pnl_sum": round(sum(r.get("daily_pnl") or 0 for r in runners), 2),
                "realized_sum": round(sum(r.get("realized_pnl") or 0 for r in runners), 2),
                "fill_count": p.get("fill_count"),
                "order_count": p.get("order_count"),
                "open_positions": p.get("open_positions"),
                "stale_marks_count": p.get("stale_marks_count"),
                "errors": p.get("errors"),
                "warnings": p.get("warnings"),
                "halted": p.get("halted"),
                "duplicates": sorted(
                    {r["name"] for r in runners if r.get("duplicate_of")}),
            }
            with open(OUT, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"{row['ist']} cap={row['total_capital']:.0f} "
                  f"bucket={bsum:.0f} runner={rsum:.0f} drift={row['total_capital']-bsum:.0f} "
                  f"fills={row['fill_count']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("sample failed:", exc, flush=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
