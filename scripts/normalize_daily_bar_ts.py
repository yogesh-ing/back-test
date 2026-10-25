"""One-off repair: anchor stored daily/weekly cache bars to the NSE session open.

``scripts/migrate_cache_ts_to_session_clock.py`` moved the 2026 intraday clock
bug, but every ``timeframe='1day'`` row still keeps whatever time-of-day mStock
sent for that chunk — 05:30 IST for the older part of a series, 09:15 IST for
the newer part, plus a few late stamps from short sessions. This script
re-anchors the daily/weekly rows so a session's date is identical read in UTC or
in ``Asia/Kolkata``, and so a nightly rollup writes bars the rest of the series
already looks like.

Intraday rows are never touched, and an anchored row stays inside its IST
calendar day — so it never crosses a Timescale chunk boundary.

The passes that need every daily row's ``ts`` read them out once and bucket in
Python: the self-join that finds date collisions blows the 30 s
``statement_timeout`` on a 46M-row hypertable, while the ~90k daily rows are
cheap to hold.

    PYTHONPATH=src python scripts/normalize_daily_bar_ts.py            # dry run
    PYTHONPATH=src python scripts/normalize_daily_bar_ts.py --execute  # repair
    PYTHONPATH=src python scripts/normalize_daily_bar_ts.py --rollback --bak-table BAK

The repair snapshots every row it is about to change into a ``bak_`` table and
refuses to run without one, so ``--rollback`` restores the original ``ts``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from backtest.db import DatabaseManager  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
TIMEFRAMES = ("1day", "1week")
ANCHOR_HOUR = 9
ANCHOR_MINUTE = 15
ANCHOR_LABEL = f"{ANCHOR_HOUR:02d}:{ANCHOR_MINUTE:02d}"
BATCH = 1000


def anchor_of(ts: datetime) -> datetime:
    """The session-open instant on the IST calendar day ``ts`` falls on."""
    ist = ts.astimezone(IST)
    anchored = ist.replace(hour=ANCHOR_HOUR, minute=ANCHOR_MINUTE, second=0, microsecond=0)
    return anchored.astimezone(timezone.utc)


def is_anchored(ts: datetime) -> bool:
    ist = ts.astimezone(IST)
    return (ist.hour, ist.minute, ist.second, ist.microsecond) == (ANCHOR_HOUR, ANCHOR_MINUTE, 0, 0)


def load_daily(session) -> list[dict]:
    """Every daily/weekly row — the working set for the anchor and collision pass."""
    rows = session.execute(
        text(
            """
            SELECT data_id, symbol, exchange, timeframe, ts
            FROM market_data_cache
            WHERE timeframe = ANY(:tfs)
            """
        ),
        {"tfs": list(TIMEFRAMES)},
    ).mappings().all()
    return [dict(r) for r in rows]


def plan(rows: list[dict]) -> tuple[list[tuple], list[tuple], list[str]]:
    """One bar per (symbol, exchange, timeframe, session date), stamped at the anchor.

    Returns (updates, deletes, notes). ``updates`` are ``(data_id, old_ts, new_ts)``
    and ``deletes`` ``(data_id, old_ts)``; both match on the ``(ts, data_id)``
    primary key so every statement stays index-bounded.
    """
    occupied = {
        (r["symbol"], r["exchange"], r["timeframe"], anchor_of(r["ts"]))
        for r in rows
        if is_anchored(r["ts"])
    }
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if not is_anchored(r["ts"]):
            buckets[(r["symbol"], r["exchange"], r["timeframe"], anchor_of(r["ts"]))].append(r)

    updates: list[tuple] = []
    deletes: list[tuple] = []
    notes: list[str] = []
    for key in sorted(buckets):
        symbol, _exchange, timeframe, anchor = key
        group = sorted(buckets[key], key=lambda r: r["ts"], reverse=True)
        if key in occupied:
            # The session already has an anchored bar; these are its stale twins.
            deletes.extend((r["data_id"], r["ts"]) for r in group)
            notes.append(
                f"{symbol} {timeframe} {anchor:%Y-%m-%d}: already anchored, "
                f"dropping {len(group)} stale row(s)"
            )
            continue
        keep, *drop = group
        updates.append((keep["data_id"], keep["ts"], anchor))
        if drop:
            deletes.extend((r["data_id"], r["ts"]) for r in drop)
            notes.append(
                f"{symbol} {timeframe} {anchor:%Y-%m-%d}: {len(group)} rows on one date, "
                f"keeping {keep['ts']:%H:%M}Z, dropping {len(drop)}"
            )
    return updates, deletes, notes


def snapshot(session, bak: str, pairs: list[tuple], batch: int = 500) -> int:
    """Copy every row about to change into ``bak`` before it is touched.

    Matches on ``(ts, data_id)`` — the primary key leads on ``ts``, and a bare
    ``data_id`` predicate is a scan of 46M rows that the 30 s statement timeout
    cancels.
    """
    session.execute(text(f"DROP TABLE IF EXISTS {bak}"))
    session.execute(
        text(f"CREATE TABLE {bak} AS SELECT m.* FROM market_data_cache m WHERE FALSE")
    )
    for i in range(0, len(pairs), batch):
        values, params = [], {}
        for j, (data_id, old_ts) in enumerate(pairs[i : i + batch]):
            values.append(f"(CAST(:ts{j} AS timestamptz), :id{j})")
            params[f"ts{j}"] = old_ts
            params[f"id{j}"] = data_id
        session.execute(
            text(
                f"INSERT INTO {bak} SELECT m.* FROM market_data_cache m "
                f"JOIN (VALUES {', '.join(values)}) AS v(ts, data_id) "
                "ON m.ts = v.ts AND m.data_id = v.data_id"
            ),
            params,
        )
    n = session.execute(text(f"SELECT count(*) FROM {bak}")).scalar_one()
    print(f"snapshot: {bak} holds {n} rows")
    return n


_UPDATE_SQL = """
    UPDATE market_data_cache m
    SET ts = v.new_ts
    FROM (VALUES (:data_id, CAST(:old_ts AS timestamptz), CAST(:new_ts AS timestamptz)))
      AS v(data_id, old_ts, new_ts)
    WHERE m.data_id = v.data_id AND m.ts = v.old_ts
"""

_DELETE_SQL = """
    DELETE FROM market_data_cache m
    USING (VALUES (:data_id, CAST(:old_ts AS timestamptz))) AS v(data_id, old_ts)
    WHERE m.data_id = v.data_id AND m.ts = v.old_ts
"""


def apply(engine, updates: list[tuple], deletes: list[tuple]) -> tuple[int, int]:
    """Re-anchor in per-batch transactions: one long transaction would hold
    28k row locks on a table the live feed is still writing to."""
    moved = 0
    for i in range(0, len(updates), BATCH):
        with engine.begin() as session:
            moved += session.execute(
                text(_UPDATE_SQL),
                [{"data_id": d, "old_ts": o, "new_ts": n} for d, o, n in updates[i : i + BATCH]],
            ).rowcount
        if (i // BATCH) % 5 == 4:
            print(f"  re-anchored {moved}/{len(updates)}", flush=True)
    dropped = 0
    for i in range(0, len(deletes), BATCH):
        with engine.begin() as session:
            dropped += session.execute(
                text(_DELETE_SQL),
                [{"data_id": d, "old_ts": o} for d, o in deletes[i : i + BATCH]],
            ).rowcount
    print(f"re-anchored {moved} rows, deleted {dropped} duplicate-date rows")
    return moved, dropped


def verify(session) -> int:
    rows = load_daily(session)
    off = [r for r in rows if not is_anchored(r["ts"])]
    dates: set[tuple] = set()
    dupes = 0
    for r in rows:
        key = (r["symbol"], r["exchange"], r["timeframe"], anchor_of(r["ts"]))
        dupes += key in dates
        dates.add(key)
    print(
        f"post-check: {len(off)} rows off the {ANCHOR_LABEL} anchor, "
        f"{dupes} duplicate session dates"
    )
    return len(off) + dupes


def rollback(session, bak: str) -> int:
    """Put the original ts back on every snapshotted row, and re-add any drop."""
    # The repair writes anchor_of(old_ts), so the reverse lookup is index-bounded
    # on (ts, data_id) instead of a scan of the hypertable by data_id.
    moved = session.execute(
        text(
            f"""
            UPDATE market_data_cache m
            SET ts = b.ts
            FROM {bak} b
            WHERE m.data_id = b.data_id
              AND m.ts = (date_trunc('day', b.ts AT TIME ZONE 'Asia/Kolkata')
                          AT TIME ZONE 'Asia/Kolkata') + interval '9 hours 15 minutes'
            """
        )
    ).rowcount
    reinserted = session.execute(
        text(
            f"""
            INSERT INTO market_data_cache
            SELECT b.* FROM {bak} b
            ON CONFLICT (ts, data_id) DO NOTHING
            """
        )
    ).rowcount
    print(f"rollback: restored {moved} timestamps, re-inserted {reinserted} dropped rows")
    return moved + reinserted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="apply the repair")
    ap.add_argument("--rollback", action="store_true", help="restore from --bak-table")
    ap.add_argument("--bak-table", default="", help="snapshot table for --execute/--rollback")
    ap.add_argument("--limit", type=int, default=0, help="act on at most N rows (smoke test)")
    args = ap.parse_args()

    engine = DatabaseManager.from_env().engine
    bak = args.bak_table or f"bak_mdc_daily_ts_{datetime.now(IST):%Y%m%d}"
    if not bak.replace("_", "a").isalnum():
        print(f"refusing unsafe table name {bak!r}")
        return 2

    if args.rollback:
        if not args.bak_table:
            print("--rollback requires the --bak-table written by --execute")
            return 2
        with engine.begin() as session:
            rollback(session, bak)
        return 0

    with engine.connect() as session:
        rows = load_daily(session)
    print(f"daily/weekly rows in cache: {len(rows)}")
    anchored = sum(1 for r in rows if is_anchored(r["ts"]))
    print(f"already at the {ANCHOR_LABEL} anchor: {anchored}")
    updates, deletes, notes = plan(rows)
    print(
        f"=== {len(updates)} rows to re-anchor, {len(deletes)} duplicate rows to drop, "
        f"{len(notes)} collision groups ==="
    )
    for note in notes[:20]:
        print("  " + note)
    if len(notes) > 20:
        print(f"  ... {len(notes) - 20} more")

    if not args.execute:
        print("dry run — nothing changed. Re-run with --execute.")
        return 0

    if args.limit:
        updates, deletes = updates[: args.limit], []
        print(f"--limit {args.limit}: acting on the first {len(updates)} rows only")

    pairs = [(d, o) for d, o, _n in updates] + [(d, o) for d, o in deletes]
    if not pairs:
        print("nothing to do")
        return 0
    # Snapshot first, in its own committed transaction, so the undo data is on
    # disk before any row moves.
    with engine.begin() as session:
        count = snapshot(session, bak, pairs)
    if count < len(pairs):
        print(f"snapshot held {count} of {len(pairs)} rows — refusing to change data")
        return 1
    apply(engine, updates, deletes)
    with engine.connect() as session:
        left = verify(session)
    print("rollback if needed: normalize_daily_bar_ts.py --rollback --bak-table " + bak)
    return 0 if left == 0 or args.limit else 1


if __name__ == "__main__":
    raise SystemExit(main())
