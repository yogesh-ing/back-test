"""Post-close evidence collection for runbook P3-EOD (run once, after 15:36 IST)."""
import json, os, time, glob
from datetime import datetime, timezone, timedelta
import urllib.request

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "http://127.0.0.1:5000"

def get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as u:
        return json.load(u)

out = {}
out["collected_ist"] = datetime.now(IST).isoformat()

# 1. square-off / EOD log lines
with open(r"C:/learning/back-test/tools/out/QA_EOD_SQUAREOFF.txt", encoding="utf-8") as f:
    out["eod_watch_file"] = f.read()[-4000:]

# 2. charts dir
files = sorted(glob.glob(r"C:/learning/back-test/charts/2026-10-28/*"))
out["charts"] = [{"name": os.path.basename(p), "size": os.path.getsize(p),
                  "mtime_ist": datetime.fromtimestamp(os.path.getmtime(p), IST).isoformat()}
                 for p in files]

# 3. post-close positions + structures
snap = get("/api/portfolio/equity/snapshot?mode=paper")
s = snap.get("snapshot", snap)
out["session"] = s.get("session")
out["per_runner"] = [{"name": r.get("name"), "status": r.get("status"), "daily_pnl": r.get("daily_pnl"),
                      "realized_pnl": r.get("realized_pnl"), "open_pnl": r.get("open_pnl"),
                      "equity": r.get("equity"), "trades_today": r.get("trades_today")}
                     for r in (s.get("per_runner") or [])]
summ = get("/api/portfolio/summary")
def find(o, k):
    if isinstance(o, dict):
        if o.get(k):
            return o[k]
        for v in o.values():
            r = find(v, k)
            if r is not None:
                return r
    if isinstance(o, list):
        for v in o:
            r = find(v, k)
            if r is not None:
                return r
    return None
runners = find(summ, "runners") or []
out["open_positions_total"] = sum(int(r.get("open_positions") or 0) for r in runners)
out["structures"] = []
for r in runners:
    od = (r.get("options") or {})
    for st in (od.get("open_structures_detail") or od.get("open_structures") or []):
        out["structures"].append({"runner": r.get("name"), "label": st.get("label"),
                                  "expiry": st.get("expiry"), "unrealized_pnl": st.get("unrealized_pnl"),
                                  "bars_held": st.get("bars_held"), "mark_ts": st.get("mark_ts")})
# 4. last drift samples
try:
    with open(r"C:/learning/back-test/tools/out/QA_CAPITAL_DRIFT.jsonl", encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    out["drift_samples"] = len(rows)
    out["drift_tail"] = rows[-3:]
except Exception as exc:
    out["drift_tail"] = f"read failed {exc}"
with open(r"C:/learning/back-test/tools/out/QA_EOD_RESULTS.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=1, default=str)
print(json.dumps({k: out[k] for k in ("collected_ist", "open_positions_total", "drift_samples")}, indent=1))
print("structures open after close:", len(out["structures"]))
print("charts newer than 15:00:", [c["name"] for c in out["charts"] if c["mtime_ist"][11:16] >= "15:00"])
