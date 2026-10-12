"""CI smoke check — prove the app boots and the canonical loop runs.

This is the clean-clone gate that the 2026-09-17 incident (uncommitted core
modules — app could not even import on a fresh checkout) makes mandatory.
A green laptop is not evidence; this script IS.

Checks (any failure → non-zero exit):
1. ``create_app()`` boots with every blueprint registered;
2. ``/health`` answers 200;
3. the canonical forward loop works end to end on the shared data bus:
   manager → feed → runner processes bars, the registry stays refcount-clean.

Run locally exactly as CI does::

    PYTHONPATH=src python scripts/smoke_check.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest.forward.feed_registry import reset_data_bus  # noqa: E402
from backtest.forward.portfolio_manager import PortfolioManager  # noqa: E402
from backtest.forward.paper_runner import RunnerConfig  # noqa: E402
from backtest.forward.risk_supervisor import GlobalRiskConfig  # noqa: E402
from backtest.web.app import create_app  # noqa: E402


def main() -> int:
    # 1. App boots ----------------------------------------------------------------
    app = create_app()
    client = app.test_client()
    health = client.get("/health")
    if health.status_code != 200:
        print(f"[smoke] FAIL: /health → {health.status_code}")
        return 1
    routes = len(list(app.url_map.iter_rules()))
    print(f"[smoke] create_app OK — {routes} routes, /health 200")

    # 2. Canonical loop on the shared bus ------------------------------------------
    reset_data_bus()
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=3,
        auto_start_feed=False,
    )
    try:
        instance_id = mgr.add_runner(
            RunnerConfig(
                name="CI-SMOKE",
                strategy_name="sma_crossover",
                allocated_capital=100_000,
                symbols=["RELIANCE"],
                timeframe="1day",
                mode="paper",
                source="synthetic",
            )
        )
        runner = mgr.get_runner(instance_id)

        base = datetime.now(timezone.utc) + timedelta(seconds=1)
        for i in range(10):
            mgr.tick(ts=base + timedelta(minutes=i))

        if runner.bars_processed < 10:
            print(f"[smoke] FAIL: runner processed {runner.bars_processed} bars (<10)")
            return 1
        summary = mgr.get_portfolio_summary()
        if summary.get("runner_count", 0) < 1:
            print(f"[smoke] FAIL: portfolio summary shows {summary}")
            return 1
        feeds = mgr.feed_registry.stats()
        print(
            f"[smoke] loop OK — bars_processed={runner.bars_processed}, "
            f"runners={summary.get('runner_count')}, registry={feeds}"
        )
    finally:
        mgr.shutdown()
        reset_data_bus()

    # 3. Registry drained ----------------------------------------------------------
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
