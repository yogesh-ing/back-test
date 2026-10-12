"""Capture EOD/intraday option-chain snapshots (the P1.1 data asset).

Usage::

    # One shot (cron-friendly — run shortly after market close, 15:30 IST):
    PYTHONPATH=src python scripts/snapshot_option_chains.py \
        --underlyings NIFTY,BANKNIFTY

    # Intraday loop, every 15 minutes while the market is open:
    PYTHONPATH=src python scripts/snapshot_option_chains.py \
        --underlyings NIFTY --interval-seconds 900

Requires an authenticated mStock session (the broker-auth UI flow, or
``backtest.live.auth`` credentials in the environment) and a reachable
database (``config/database.yaml`` / env). Contract terms are one API call
per underlying; quotes are one call per quoted expiry (default: nearest).

Every run appends an immutable batch to ``option_chain_snapshots`` —
see ``src/backtest/options/chain_snapshots.py``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest.logging_config import configure_logging  # noqa: E402
from backtest.options.chain_snapshots import ChainSnapshotRecorder  # noqa: E402

logger = logging.getLogger("backtest.scripts.chain_snapshots")


def _build_client():
    """The authenticated order broker, or a clean failure.

    2026-09-22: the script runs in its OWN process — the web UI's in-memory
    broker session is invisible here, and the ``.mstock_session_token`` file
    can be STALE (a dead token TokenExceptions on every call). Resolution
    order:
    1. the local web app's live session via a localhost-only bridge endpoint
       (fresh token when the user is logged in there — the normal case);
    2. the persisted token file (root of checkout) — accepted as-is, the
       API rejects a dead token fail-closed on first use;
    3. an unauthenticated failure with the standard guidance.
    """
    import os
    from datetime import datetime, timedelta

    import requests as _requests

    from backtest.brokers.session_manager import get_session_manager

    mgr = get_session_manager()
    if not mgr.is_authenticated():
        token = None
        # 1) Fresh token from the local web app (localhost only).
        try:
            resp = _requests.get(
                "http://127.0.0.1:5000/api/broker/session-token", timeout=5
            )
            if resp.status_code == 200:
                token = str(resp.json().get("token") or "")
        except Exception:  # noqa: BLE001 — app not running / endpoint absent
            token = None
        # 2) Persisted token file (may be stale — first API call will tell).
        if not token:
            token_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".mstock_session_token",
            )
            if os.path.exists(token_file):
                with open(token_file) as f:
                    token = f.read().strip()
        if not token or len(token) < 16:
            raise SystemExit(
                "No authenticated mStock session — run the broker-auth flow first "
                "(web UI → Broker Auth) or set credentials per docs/DATA-SOURCES.md"
            )
        broker = mgr.get_active_broker()
        # Restore into the broker's in-memory session. Expiry unknown from a
        # file — assume a same-day session; mStock rejects a dead token
        # fail-closed on the first call.
        broker._session_token = token
        broker._expires_at = datetime.now() + timedelta(hours=8)
        if mgr.is_authenticated():
            return broker
        raise SystemExit(
            "No authenticated mStock session — run the broker-auth flow first "
            "(web UI → Broker Auth) or set credentials per docs/DATA-SOURCES.md"
        )
    return mgr.get_active_broker()


def _build_manager():
    from backtest.db import DatabaseManager

    manager = DatabaseManager.from_env()
    manager.connect()
    return manager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--underlyings",
        default="NIFTY,BANKNIFTY",
        help="Comma-separated underlyings (default: NIFTY,BANKNIFTY)",
    )
    parser.add_argument(
        "--quote-expiries",
        type=int,
        default=1,
        help="How many nearest expiries get quote enrichment (default 1)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=0.0,
        help=">0: loop forever, snapshotting every N seconds (0 = one shot)",
    )
    args = parser.parse_args()
    configure_logging()

    underlyings = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    client = _build_client()
    manager = _build_manager()
    recorder = ChainSnapshotRecorder(client=client, manager=manager)
    recorder.ensure_schema()

    while True:
        counts = recorder.record_all(
            underlyings, quote_expiries=args.quote_expiries
        )
        logger.info("snapshot batch complete: %s", counts)
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
