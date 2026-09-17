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
    """The authenticated order broker, or a clean failure."""
    from backtest.brokers.session_manager import get_session_manager

    mgr = get_session_manager()
    if not mgr.is_authenticated():
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
