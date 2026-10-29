"""TC-4.6: POST /api/portfolio/test/breach, with before/after state captured. Read-only otherwise."""
import json, urllib.request
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "http://127.0.0.1:5000"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as u:
        return json.load(u)


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as u:
            return u.status, json.load(u)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def digest(tag):
    risk = get("/api/portfolio/risk?mode=paper")["risk"]
    summ = get("/api/portfolio/summary")["portfolio"]
    paper = (risk.get("buckets") or {}).get("paper") or {}
    return {
        "at_ist": datetime.now(IST).isoformat(),
        "tag": tag,
        "risk.global_config": risk.get("global_config"),
        "risk.paper_bucket": paper,
        "risk.segments": {k: {kk: vv for kk, vv in (v or {}).items()
                              if kk in ("daily_pnl", "halted", "runner_count", "allocated_capital")}
                          for k, v in (risk.get("segments") or {}).items()},
        "summary.daily_pnl": summ.get("daily_pnl"),
        "summary.realized_pnl": summ.get("realized_pnl"),
        "summary.deployed_capital": summ.get("deployed_capital"),
        "summary.peak_equity": summ.get("peak_equity"),
        "summary.total_equity": summ.get("total_equity"),
        "summary.total_capital": summ.get("total_capital"),
        "summary.halt_state": summ.get("halt_state"),
        "summary.halted": summ.get("halted"),
        "summary.order_count": summ.get("order_count"),
        "summary.errors": summ.get("errors"),
    }


out = {"started_ist": datetime.now(IST).isoformat()}
out["before"] = digest("before")
code, body = post("/api/portfolio/test/breach", {"crash_pct": 0.25, "tighten_limits": False})
out["request"] = {"http": code, "body": body}
out["after"] = digest("after")
with open(r"C:/learning/back-test/tools/out/QA_TC46_RESULT.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=1, default=str)

b, a = out["before"], out["after"]
print("http", code, json.dumps(body, default=str)[:400])
for k in ("daily_pnl", "realized_pnl", "peak_equity", "total_equity", "total_capital",
          "halt_state", "halted", "errors"):
    print(f"{k:16} before={b['summary.'+k]!r:<24} after={a['summary.'+k]!r}")
pb, pa = b["risk.paper_bucket"], a["risk.paper_bucket"]
for k in ("daily_pnl", "daily_loss_used", "daily_loss_pct", "peak_equity", "halted", "equity"):
    print(f"bucket.{k:16} before={pb.get(k)!r:<24} after={pa.get(k)!r}")
gc_b, gc_a = b["risk.global_config"], a["risk.global_config"]
print("global_config identical:", gc_b == gc_a)
if isinstance(gc_b, dict) and isinstance(gc_a, dict):
    diff = {k: (gc_b.get(k), gc_a.get(k)) for k in set(gc_b) | set(gc_a) if gc_b.get(k) != gc_a.get(k)}
    print("global_config changed:", json.dumps(diff, default=str)[:400])
