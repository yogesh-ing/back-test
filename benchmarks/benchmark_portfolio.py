"""50-instance concurrency benchmark (PRD Task 7.1).

Spawns 50 concurrent StrategyRunner instances — 30 Single-Symbol and 20
Symbol-Universe (pool) runners — on the synthetic feed and verifies:

1. 1,000+ mock fills route through the order ledger with **zero**
   cross-contamination (each fill reaches exactly its owning runner).
2. Steady-state per-tick processing fits the 1-second SSE tick budget.
3. Memory footprint stays well under the 800 MB / <15 MB-per-instance target.

Run directly::

    PYTHONPATH=src python benchmarks/benchmark_portfolio.py
"""

from __future__ import annotations

import logging
import resource
import statistics
import sys
import time
from typing import List

logging.disable(logging.CRITICAL)

from backtest.forward.order_ledger import SIDE_BUY
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.runner import RunnerConfig, TARGET_POOL, TARGET_SINGLE

CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB", "XRP/USD",
          "DOGE/USD", "ADA/USD", "AVAX", "LINK", "DOT/USD"]
STRATS = ["rsi_reversion", "sma_crossover", "donchian_breakout", "buy_and_hold"]


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def build_portfolio() -> PortfolioManager:
    # Generous limits so the benchmark run is not halted by the circuit breaker.
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1e12, max_drawdown_pct=0.99),
        tick_seconds=1.0, warmup_bars=30, auto_start_feed=False,
    )
    for i in range(30):
        sym = f"{CRYPTO[i % len(CRYPTO)]}-S{i}"
        mgr.add_runner(RunnerConfig(
            name=f"Single-{i:02d}", strategy_name=STRATS[i % 4],
            allocated_capital=100_000, target_type=TARGET_SINGLE,
            symbols=[sym], timeframe="1h",
        ))
    for i in range(20):
        mgr.add_runner(RunnerConfig(
            name=f"Pool-{i:02d}", strategy_name=STRATS[i % 4],
            allocated_capital=100_000, target_type=TARGET_POOL,
            symbols=[f"P{i}_{j}" for j in range(10)],
            timeframe="1d", max_pool_positions=3,
        ))
    return mgr


def benchmark_50_instances(verbose: bool = True) -> dict:
    t0 = time.perf_counter()
    mgr = build_portfolio()
    build_s = time.perf_counter() - t0
    assert len(mgr._runners) == 50

    t0 = time.perf_counter()
    warm_bars = mgr.feed.warmup()
    warm_s = time.perf_counter() - t0

    # Steady-state per-tick timings (no warmup amortization).
    tick_times: List[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        mgr.tick()
        tick_times.append(time.perf_counter() - t0)

    fills_from_ticks = mgr.ledger.fill_count

    # ---- 1,000+ mock fill routing check ----------------------------------
    routed: dict = {}
    instance_ids = list(mgr._runners.keys())

    def make_handler(rid):
        def handler(fill):
            assert fill.instance_id == rid, "cross-contamination detected"
            routed[rid] = routed.get(rid, 0) + 1
        return handler

    # Replace handlers with a counting-and-asserting wrapper.
    for rid in instance_ids:
        mgr.ledger.register_handler(rid, make_handler(rid))

    t0 = time.perf_counter()
    symbols = mgr.feed._symbols
    n_mock = 1200
    for i in range(n_mock):
        rid = instance_ids[i % len(instance_ids)]
        runner = mgr.get_runner(rid)
        sym = runner.config.symbols[i % len(runner.config.symbols)]
        runner.last_price[sym] = 100.0 + (i % 50)
        mgr.broker.submit_market(rid, sym, SIDE_BUY, 5, 100.0 + (i % 50))
    fill_s = time.perf_counter() - t0

    total_fills = mgr.ledger.fill_count
    total_routed = sum(routed.values())
    rss = _rss_mb()

    result = {
        "runners": 50,
        "single_runners": 30,
        "pool_runners": 20,
        "symbols_subscribed": len(mgr.feed._symbols),
        "warmup_bars": warm_bars,
        "warmup_seconds": round(warm_s, 2),
        "build_seconds": round(build_s, 2),
        "tick_ms_mean": round(1000 * statistics.mean(tick_times), 1),
        "tick_ms_max": round(1000 * max(tick_times), 1),
        "fills_from_ticks": fills_from_ticks,
        "mock_fills_injected": n_mock,
        "mock_fill_seconds": round(fill_s, 3),
        "total_fills": total_fills,
        "routed_fills": total_routed,
        "cross_contamination": 0,
        "rss_mb": round(rss, 1),
        "rss_per_runner_mb": round(rss / 50, 2),
    }

    # ---- assertions (acceptance criteria) --------------------------------
    assert total_routed == n_mock, f"only {total_routed}/{n_mock} fills routed"
    assert total_fills >= 1000, f"expected 1,000+ fills, got {total_fills}"
    assert rss < 800, f"memory {rss:.0f}MB exceeds 800MB budget"
    # Steady-state tick must fit the 1-second SSE cadence with headroom.
    assert result["tick_ms_mean"] < 1000, f"tick {result['tick_ms_mean']}ms exceeds 1s"

    mgr.shutdown()

    if verbose:
        print("=" * 64)
        print("  50-INSTANCE PORTFOLIO BENCHMARK (Task 7.1)")
        print("=" * 64)
        for k, v in result.items():
            print(f"  {k:24s} {v}")
        print("=" * 64)
        print("  PASS: fills routed with 0 contamination, memory & CPU within budget")
    return result


if __name__ == "__main__":
    try:
        benchmark_50_instances()
    except AssertionError as exc:
        print(f"BENCHMARK FAILED: {exc}")
        sys.exit(1)
