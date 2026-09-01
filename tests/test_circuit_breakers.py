"""Circuit breaker stress test (PRD Task 7.2).

Simulates a rapid market crash that pushes aggregate portfolio loss past the
daily-loss / drawdown limits and verifies:

* the :class:`RiskSupervisor` latches a halt within < 500 ms of breach;
* emergency-flatten mode emits exit orders for **all** open positions across
  **all** runners;
* once halted, no new entries are taken.
"""

from __future__ import annotations

import logging
import time

import pytest

from backtest.forward.paper_runner import (
    SIDE_BUY,
    STATUS_PAUSED,
    TARGET_POOL,
    TARGET_SINGLE,
    RunnerConfig,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import (
    HALT_FLATTEN,
    HALT_PAUSE,
    GlobalRiskConfig,
    RiskSupervisor,
)


# Silence the engine's chatty per-bar logs for this module only — never call
# logging.disable() at import time, as it is process-global and would leak
# into other test modules (caplog would capture nothing).
@pytest.fixture(autouse=True)
def _quiet_logs():
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def _build_50_runners() -> PortfolioManager:
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(
            daily_loss_limit=200_000, max_drawdown_pct=0.10, breach_mode=HALT_FLATTEN
        ),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    for i in range(30):
        mgr.add_runner(
            RunnerConfig(
                name=f"S{i:02d}",
                strategy_name="rsi_reversion",
                allocated_capital=100_000,
                target_type=TARGET_SINGLE,
                symbols=[f"S{i}"],
                timeframe="1hour",
            )
        )
    for i in range(20):
        mgr.add_runner(
            RunnerConfig(
                name=f"P{i:02d}",
                strategy_name="donchian_breakout",
                allocated_capital=100_000,
                target_type=TARGET_POOL,
                symbols=[f"P{i}_{j}" for j in range(8)],
                timeframe="1day",
                max_pool_positions=3,
            )
        )
    return mgr


def _seed_positions(mgr: PortfolioManager, per_runner: int = 2) -> int:
    """Open *per_runner* positions in every runner via the paper broker."""
    total = 0
    for rid, runner in mgr._runners.items():
        syms = runner.config.symbols[:per_runner]
        for sym in syms:
            price = 500.0
            runner.last_price[sym] = price
            mgr.broker.submit_market(rid, sym, SIDE_BUY, 50, price)
            total += 1
    return total


def test_supervisor_rule_logic_is_instant():
    """The supervisor evaluation itself must be near-instant across 50 runners."""
    sup = RiskSupervisor(GlobalRiskConfig(daily_loss_limit=10_000, max_drawdown_pct=0.10))
    mgr = _build_50_runners()
    try:
        mgr.feed.warmup()
        runners = list(mgr._runners.values())
        t0 = time.perf_counter()
        for _ in range(100):
            report = sup.evaluate(
                runners=runners, total_equity=4_000_000, peak_equity=5_000_000, daily_pnl=-1_000_000
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000 / 100
        assert report.halted
        assert report.halt_mode == HALT_FLATTEN  # drawdown forces flatten
        assert elapsed_ms < 50, f"supervisor eval too slow: {elapsed_ms:.1f}ms"
    finally:
        mgr.shutdown()


def test_crash_halts_all_runners_under_500ms():
    mgr = _build_50_runners()
    try:
        mgr.feed.warmup()
        positions = _seed_positions(mgr)
        # 30 single-symbol runners (1 pos each) + 20 pool runners (2 each)
        assert positions == 30 * 1 + 20 * 2
        assert mgr.get_portfolio_summary()["open_positions"] == positions

        # Crash enough to breach both daily loss and drawdown.
        t0 = time.perf_counter()
        mgr.simulate_crash(crash_pct=0.40)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        summary = mgr.get_portfolio_summary()
        assert summary["halted"] is True
        assert elapsed_ms < 500, f"halt took {elapsed_ms:.0f}ms (> 500ms budget)"

        # All running instances paused by the halt.
        assert summary["running"] == 0
        assert summary["paused"] + summary["stopped"] == 50
    finally:
        mgr.shutdown()


def test_flatten_mode_exits_all_positions():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(
            daily_loss_limit=10_000, max_drawdown_pct=0.05, breach_mode=HALT_FLATTEN
        ),
        warmup_bars=12,
        auto_start_feed=False,
    )
    try:
        rid = mgr.add_runner(
            RunnerConfig(
                name="P",
                strategy_name="donchian_breakout",
                allocated_capital=1_000_000,
                target_type=TARGET_POOL,
                symbols=[f"X{j}" for j in range(6)],
                timeframe="1day",
                max_pool_positions=4,
            )
        )
        runner = mgr.get_runner(rid)
        mgr.feed.warmup()
        for sym in runner.config.symbols[:4]:
            runner.last_price[sym] = 400.0
            mgr.broker.submit_market(rid, sym, SIDE_BUY, 100, 400.0)
        assert len(runner.positions) == 4

        mgr.simulate_crash(crash_pct=0.30)

        assert mgr.get_portfolio_summary()["halted"]
        # Flatten closes everything across all runners.
        assert all(len(r.positions) == 0 for r in mgr._runners.values())
        assert mgr.get_portfolio_summary()["open_positions"] == 0
    finally:
        mgr.shutdown()


def test_pause_mode_holds_positions_but_blocks_new_entries():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(
            daily_loss_limit=10_000, max_drawdown_pct=0.99, breach_mode=HALT_PAUSE
        ),
        warmup_bars=12,
        auto_start_feed=False,
    )
    try:
        rid = mgr.add_runner(
            RunnerConfig(
                name="P",
                strategy_name="donchian_breakout",
                allocated_capital=1_000_000,
                target_type=TARGET_POOL,
                symbols=[f"X{j}" for j in range(6)],
                timeframe="1day",
                max_pool_positions=4,
            )
        )
        runner = mgr.get_runner(rid)
        mgr.feed.warmup()
        for sym in runner.config.symbols[:3]:
            runner.last_price[sym] = 400.0
            mgr.broker.submit_market(rid, sym, SIDE_BUY, 100, 400.0)
        held = len(runner.positions)
        assert held == 3

        mgr.simulate_crash(crash_pct=0.10)  # enough for the 10k daily loss
        assert mgr.get_portfolio_summary()["halted"]
        assert mgr.halt_mode == HALT_PAUSE
        # Positions are HELD (not flattened) in pause mode.
        assert len(runner.positions) == held
        # But new entries are blocked — all runners are paused.
        assert all(r.status == STATUS_PAUSED for r in mgr._runners.values())
    finally:
        mgr.shutdown()


def test_no_new_entries_after_halt():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=5_000, max_drawdown_pct=0.05),
        warmup_bars=12,
        auto_start_feed=False,
    )
    try:
        rid = mgr.add_runner(
            RunnerConfig(
                name="P",
                strategy_name="donchian_breakout",
                allocated_capital=1_000_000,
                target_type=TARGET_POOL,
                symbols=[f"X{j}" for j in range(6)],
                timeframe="1day",
                max_pool_positions=4,
            )
        )
        runner = mgr.get_runner(rid)
        mgr.feed.warmup()
        for sym in runner.config.symbols[:3]:
            runner.last_price[sym] = 400.0
            mgr.broker.submit_market(rid, sym, SIDE_BUY, 100, 400.0)
        fills_before = mgr.ledger.fill_count
        mgr.simulate_crash(crash_pct=0.30)
        fills_at_halt = mgr.ledger.fill_count
        # Further ticks must not create NEW long entries (exits only).
        for _ in range(5):
            mgr.tick()
        summary = mgr.get_portfolio_summary()
        assert summary["halted"]
        # No position can be (re)opened while halted/paused.
        assert all(r.status == STATUS_PAUSED for r in mgr._runners.values())
    finally:
        mgr.shutdown()
