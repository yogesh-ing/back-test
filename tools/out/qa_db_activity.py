"""P1-DB + persistence sampler: one row per minute for the runbook's 45-min window."""
import os, sys, time, json
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
import psycopg2

URL = None
for line in open(r"C:/learning/back-test/.env", encoding="utf-8"):
    line = line.strip()
    if line.startswith("FORWARD_TEST_DB_URL=") and not line.startswith("#"):
        URL = line.split("=", 1)[1].strip()
        break
if not URL:
    print("no db url"); sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 45
out = open(r"C:/learning/back-test/tools/out/QA_DB_ACTIVITY.jsonl", "a", encoding="utf-8")
conn = psycopg2.connect(URL.replace("postgresql+psycopg2://", "postgresql://"))
conn.set_session(readonly=True, autocommit=True)
for i in range(minutes):
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) total,
                   count(*) filter (where state <> 'idle') busy,
                   count(distinct usename) users
            from pg_stat_activity where datname = current_database()
            """
        )
        total, busy, users = cur.fetchone()
        try:
            cur.execute("select count(*), coalesce(sum(realized_pnl),0) from trades")
            t_rows, t_pnl = cur.fetchone()
        except Exception as exc:
            conn.rollback()
            t_rows, t_pnl = f"ERR {exc}"[:40], None
        try:
            cur.execute(
                "select count(*) from information_schema.tables where table_name='option_intent_events'"
            )
            ev_exists = cur.fetchone()[0]
            if ev_exists:
                cur.execute("select count(*) from option_intent_events")
                ev_rows = cur.fetchone()[0]
            else:
                ev_rows = "no table"
        except Exception as exc:
            conn.rollback()
            ev_rows = f"ERR {exc}"[:40]
    rec = {
        "ts_ist": datetime.now(IST).strftime("%H:%M:%S"),
        "pg_activity_total": total,
        "pg_activity_busy": busy,
        "trades_rows": t_rows,
        "trades_realized_pnl": float(t_pnl) if t_pnl is not None else None,
        "option_intent_event_rows": ev_rows,
    }
    out.write(json.dumps(rec) + "\n")
    out.flush()
    print(rec, flush=True)
    time.sleep(60)
conn.close()
