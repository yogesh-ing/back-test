"""P2-RING evidence: per-structure live depth for every option runner.

Reads /api/portfolio/runner/<id> for each runner in the SSE registry and dumps
the option book rows with the fields HotStrikeRing is judged on: mark_stale,
mtm_failures, last_quote_error, bid/ask presence and quote age.
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:5000"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def runners():
    req = urllib.request.Request(BASE + "/api/portfolio/stream", headers={"Accept": "text/event-stream"})
    body = b""
    with urllib.request.urlopen(req, timeout=30) as resp:
        for raw in resp:
            body += raw
            if raw in (b"\n", b"\r\n") and b"data: " in body:
                break
    payload = json.loads(body.decode("utf-8").split("data:", 1)[1].strip())
    rows = payload.get("runners") or payload.get("instances") or []
    return payload, rows


def find_rows(node, out):
    if isinstance(node, dict):
        if "structures" in node and isinstance(node["structures"], list):
            out.append(node)
        for v in node.values():
            find_rows(v, out)
    elif isinstance(node, list):
        for v in node:
            find_rows(v, out)


def main():
    payload, rows = runners()
    ids = []
    for r in rows:
        rid = r.get("instance_id") or r.get("id") or r.get("runner_id")
        if rid:
            ids.append((rid, r))
    print(f"SSE keys: {sorted(payload.keys())}")
    print(f"runners seen: {len(ids)}")
    now = time.time()
    for rid, row in ids:
        if row.get("cadence") not in ("tick", None) and not row.get("structures"):
            pass
        try:
            d = get(f"/api/portfolio/runner/{rid}")
        except Exception as exc:
            print(f"{rid}: DETAIL FAILED {exc}")
            continue
        holders = []
        find_rows(d, holders)
        seg = (d.get("segment") or d.get("mode") or "?")
        st_count = 0
        stale = 0
        fails = 0
        no_depth = 0
        ages = []
        marks = []
        for h in holders:
            for s in h.get("structures") or []:
                st_count += 1
                stale += 1 if s.get("mark_stale") else 0
                fails += int(s.get("mtm_failures") or 0)
                ts = s.get("last_mark_ts") or s.get("updated_at")
                marks.append((s.get("symbol"), s.get("mark") or s.get("mtm"), s.get("bid"), s.get("ask"),
                              s.get("last_quote_error"), ts))
                if s.get("bid") in (None, 0) and s.get("ask") in (None, 0):
                    no_depth += 1
        if st_count == 0:
            continue
        print(f"\n== {rid}  structures_open={st_count} mark_stale={stale} mtm_failures={fails} zero_depth={no_depth}")
        for m in marks[:12]:
            print("   ", m)
    print(f"\nprobe done at epoch {now:.0f}")


if __name__ == "__main__":
    sys.exit(main())
