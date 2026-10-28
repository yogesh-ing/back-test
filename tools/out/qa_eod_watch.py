"""Watch the live log for the EOD intraday square-off (runbook P3-EOD) until 15:35 IST."""
import re, time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
LOG = r"C:/learning/back-test/tools/out/qa_server_20261028_cutover.log"
OUT = r"C:/learning/back-test/tools/out/QA_EOD_SQUAREOFF.txt"
PATTERNS = re.compile(
    r"SQUAREOFF|squareoff|square_off|intraday_squareoff|EXIT_INTRADAY|eod_flat|EOD|expiry_settlement|15:30 chart|Export charts"
)

out = open(OUT, "a", encoding="utf-8", buffering=1)
pos = 0
out.write(f"=== watcher start {datetime.now(IST):%H:%M:%S} IST ===\n")
while datetime.now(IST) < datetime.now(IST).replace(hour=15, minute=36, second=0):
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()
    except Exception as exc:
        out.write(f"read error {exc}\n")
        time.sleep(20)
        continue
    for line in chunk.splitlines():
        if PATTERNS.search(line):
            out.write(line[:400] + "\n")
    time.sleep(15)
out.write(f"=== watcher stop {datetime.now(IST):%H:%M:%S} IST ===\n")
out.close()
