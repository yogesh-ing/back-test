"""Unit tests for the multi-strategy portfolio forward-testing engine.

Covers PRD Phases 1–4:
* StrategyRunner isolated container + PnL/order isolation (Tasks 1.1, 1.2)
* Symbol universe registry + pool scanning/ranking (Tasks 2.1, 2.2)
* PortfolioManager orchestration + aggregation (Task 3.1)
* Circuit breaker supervisor (Task 3.2)
* Order tagging ledger fill routing (Task 4.1)
"""

from __future__ import annotations

import threading

import pandas as pd
import pytest

from backtest.data.universe import (
    correlation_group_for,
    get_universe,
    get_universe_symbols,
    is_universe,
    list_universes,
)
from backtest.forward.paper_runner import (
    MAX_BARS_PER_SYMBOL,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    TARGET_POOL,
    TARGET_SINGLE,
    FillEvent,
    OrderLedger,
    OrderRequest,
    PaperBroker,
    RunnerConfig,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import (
    HALT_FLATTEN,
    HALT_PAUSE,
    STATE_HALTED,
    GlobalRiskConfig,
    RiskSupervisor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_bar(ts, price, volume=1000):
    return {
        "ts": ts,
        "open": price,
        "high": price * 1.01,
        "low": price * 0.99,
        "close": price,
        "volume": volume,
    }


@pytest.fixture
def ledger():
    return OrderLedger()


@pytest.fixture
def manager():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=20,
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Phase 4 — Order ledger
# ---------------------------------------------------------------------------


class TestOrderLedger:
    def test_client_order_id_format(self, ledger):
        order = ledger.submit(
            "abcdef123456", OrderRequest(symbol="BTC/USD", side=SIDE_BUY, quantity=1)
        )
        assert order.client_order_id.startswith("PRT-abcdef12-")
        assert order.status == "PENDING"

    def test_fill_routes_to_owning_runner(self, ledger):
        received = []
        ledger.register_handler("runner-a", lambda f: received.append(("a", f)))
        ledger.register_handler("runner-b", lambda f: received.append(("b", f)))

        broker = PaperBroker(ledger)
        fa = broker.submit_market("runner-a", "AAA", SIDE_BUY, 10, 100.0)
        fb = broker.submit_market("runner-b", "BBB", SIDE_BUY, 5, 200.0)

        assert [r[0] for r in received] == ["a", "b"]
        assert received[0][1].instance_id == "runner-a"
        assert received[1][1].price == 200.0
        assert ledger.fill_count == 2
        assert ledger.owner_of(fa.client_order_id) == "runner-a"

    def test_unknown_client_order_id_rejected(self, ledger):
        with pytest.raises(KeyError):
            ledger.apply_fill("PRT-deadbeef-0-0", 100.0)

    def test_zero_cross_contamination_1000_fills(self, ledger):
        """1,000 fills across 50 runners route with 0 contamination (Task 7.1)."""
        counts = {f"r{i:03d}": 0 for i in range(50)}
        for rid in counts:
            ledger.register_handler(
                rid, lambda f, owner=rid: counts.__setitem__(owner, counts[owner] + 1)
            )
        broker = PaperBroker(ledger)
        for i in range(1000):
            owner = f"r{i % 50:03d}"
            broker.submit_market(owner, f"S{i % 20}", SIDE_BUY, 1, 100.0)
        assert ledger.fill_count == 1000
        assert all(c == 20 for c in counts.values())  # 1000 / 50

    def test_invalid_order_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.submit("r", OrderRequest(symbol="X", side="SIDEWAYS", quantity=1))
        with pytest.raises(ValueError):
            ledger.submit("r", OrderRequest(symbol="X", side=SIDE_BUY, quantity=0))


# ---------------------------------------------------------------------------
# Phase 2 — Universe registry
# ---------------------------------------------------------------------------


class TestUniverse:
    def test_nifty50_resolves(self):
        syms = get_universe_symbols("NIFTY_50")
        assert len(syms) == 50
        assert "RELIANCE" in syms
        assert syms == get_universe_symbols("NIFTY50")  # alias

    def test_crypto_pools(self):
        top10 = get_universe_symbols("TOP_10_CRYPTO")
        top20 = get_universe_symbols("TOP_20_CRYPTO")
        assert len(top10) == 10 and "BTC/USD" in top10
        assert len(top20) == 20

    def test_listing_and_membership(self):
        ids = [u["id"] for u in list_universes()]
        assert "NIFTY_50" in ids
        assert is_universe("nifty_50")
        assert not is_universe("NOT_A_POOL")

    def test_unknown_universe_raises(self):
        with pytest.raises(KeyError):
            get_universe_symbols("DOES_NOT_EXIST")

    def test_correlation_group(self):
        assert correlation_group_for("BTC/USD") == "crypto"
        assert correlation_group_for("ETH/USD") == "crypto"
        assert correlation_group_for("RELIANCE") is None


# ---------------------------------------------------------------------------
# Phase 1 — StrategyRunner container + isolation
# ---------------------------------------------------------------------------


class TestStrategyRunner:
    def _runner(self, ledger, **overrides):
        cfg = RunnerConfig(
            name="R1",
            strategy_name="rsi_reversion",
            allocated_capital=100_000,
            target_type=TARGET_SINGLE,
            symbols=["AAA"],
            timeframe="1hour",
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return StrategyRunner(cfg, ledger=ledger, broker=PaperBroker(ledger))

    def test_state_machine(self, ledger):
        r = self._runner(ledger)
        assert r.status == STATUS_STOPPED
        r.start()
        assert r.status == STATUS_RUNNING
        r.pause()
        assert r.status == STATUS_PAUSED
        r.resume()
        assert r.status == STATUS_RUNNING
        r.stop()
        assert r.status == STATUS_STOPPED

    def test_buffer_is_ring_fenced(self, ledger):
        from datetime import datetime, timedelta

        r = self._runner(ledger)
        r.start()
        base = datetime(2026, 1, 1, 10, 0)
        for i in range(MAX_BARS_PER_SYMBOL + 100):
            ts = (base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")
            r.process_candle_event("AAA", make_bar(ts, 100 + i * 0.1))
        assert len(r._bars["AAA"]) == MAX_BARS_PER_SYMBOL

    def test_pnl_isolation_between_runners(self, ledger):
        r1 = self._runner(ledger, symbols=["AAA"])
        r2 = self._runner(ledger)
        r2.config.symbols = ["BBB"]
        r2._bars = {"BBB": r2._bars.pop("AAA")}
        r1.start()
        r2.start()

        # r1 buys AAA at 100, r2 buys BBB at 200 via fills
        ledger.register_handler(r1.instance_id, r1.on_fill)
        ledger.register_handler(r2.instance_id, r2.on_fill)
        broker = PaperBroker(ledger)
        broker.submit_market(r1.instance_id, "AAA", SIDE_BUY, 100, 100.0)
        broker.submit_market(r2.instance_id, "BBB", SIDE_BUY, 50, 200.0)

        # r1's loss must not touch r2's books
        assert r1.positions["AAA"]["qty"] == 100
        assert "AAA" not in r2.positions
        assert "BBB" not in r1.positions
        assert r1.cash == pytest.approx(90_000)
        assert r2.cash == pytest.approx(90_000)

    def test_trade_round_trip_records_pnl(self, ledger):
        r = self._runner(ledger)
        r.start()
        broker = PaperBroker(ledger)
        broker.submit_market(r.instance_id, "AAA", SIDE_BUY, 100, 100.0)
        broker.submit_market(r.instance_id, "AAA", SIDE_SELL, 100, 110.0)
        assert len(r.closed_trades) == 1
        assert r.realized_pnl == pytest.approx(1000.0)
        assert r.wins == 1
        assert r.win_rate() == 1.0
        assert "AAA" not in r.positions
        assert r.cash == pytest.approx(101_000.0)

    def test_win_rate_and_equity(self, ledger):
        r = self._runner(ledger)
        r.start()
        broker = PaperBroker(ledger)
        # win
        broker.submit_market(r.instance_id, "AAA", SIDE_BUY, 10, 100.0)
        broker.submit_market(r.instance_id, "AAA", SIDE_SELL, 10, 110.0)
        # loss
        broker.submit_market(r.instance_id, "AAA", SIDE_BUY, 10, 100.0)
        broker.submit_market(r.instance_id, "AAA", SIDE_SELL, 10, 90.0)
        assert r.wins == 1 and r.losses == 1
        assert r.win_rate() == pytest.approx(0.5)
        assert r.equity() == pytest.approx(100_000.0)  # +100 -100

    def test_paused_runner_blocks_new_entries(self, ledger):
        r = self._runner(ledger)
        r.start()
        assert r.status == STATUS_RUNNING
        r.pause()
        # Direct entry attempt while paused must be blocked at the action layer.
        r._act_on_signal("AAA", 1, 100.0, "2026-02-01 10:00:00")
        assert "AAA" not in r.positions
        # Resume → the same signal now executes.
        r.resume()
        r._act_on_signal("AAA", 1, 100.0, "2026-02-01 10:00:00")
        assert "AAA" in r.positions

    def test_pool_runner_caps_positions(self, ledger):
        cfg = RunnerConfig(
            name="Pool",
            strategy_name="donchian_breakout",
            allocated_capital=500_000,
            target_type=TARGET_POOL,
            symbols=[f"S{j}" for j in range(8)],
            timeframe="1day",
            max_pool_positions=3,
        )
        r = StrategyRunner(cfg, ledger=ledger, broker=PaperBroker(ledger))
        r.start()
        # Uptrend on all 8 → entries capped at 3
        for i in range(40):
            ts = f"2026-01-{i+1:02d} 00:00:00"
            for j in range(8):
                r.process_candle_event(f"S{j}", make_bar(ts, 100 + i * 2 + j))
            r.on_tick_end(ts)
        assert len(r.positions) <= 3

    def test_get_state_and_detail_shape(self, ledger):
        r = self._runner(ledger)
        r.start()
        state = r.get_state()
        for key in (
            "instance_id",
            "name",
            "strategy_name",
            "target_type",
            "target_label",
            "allocated_capital",
            "equity",
            "daily_pnl",
            "open_pnl",
            "status",
            "open_positions",
            "win_rate",
            "max_drawdown_pct",
            "bars_processed",
        ):
            assert key in state
        detail = r.get_detail()
        for key in (
            "positions",
            "trades",
            "signals",
            "equity_curve",
            "params",
            "universe_symbols",
            "cash",
        ):
            assert key in detail


# ---------------------------------------------------------------------------
# Phase 3 — PortfolioManager + supervisor
# ---------------------------------------------------------------------------


class TestPortfolioManager:
    def _add_demo_runners(self, mgr):
        id1 = mgr.add_runner(
            RunnerConfig(
                name="RSI",
                strategy_name="rsi_reversion",
                allocated_capital=1_000_000,
                target_type=TARGET_SINGLE,
                symbols=["BTC/USD"],
                timeframe="1hour",
            )
        )
        id2 = mgr.add_runner(
            RunnerConfig(
                name="Pool",
                strategy_name="donchian_breakout",
                allocated_capital=2_500_000,
                target_type=TARGET_POOL,
                universe_id="NIFTY_50",
                timeframe="1day",
                max_pool_positions=5,
            )
        )
        id3 = mgr.add_runner(
            RunnerConfig(
                name="MACD",
                strategy_name="sma_crossover",
                allocated_capital=800_000,
                target_type=TARGET_SINGLE,
                symbols=["ETH/USD"],
                timeframe="15min",
            )
        )
        return id1, id2, id3

    def test_add_and_aggregate(self, manager):
        ids = self._add_demo_runners(manager)
        summary = manager.get_portfolio_summary()
        assert summary["runner_count"] == 3
        assert summary["total_capital"] == pytest.approx(4_300_000)
        assert summary["running"] == 3

    def test_pause_all_resume_all(self, manager):
        self._add_demo_runners(manager)
        assert manager.pause_all() == 3
        assert manager.get_portfolio_summary()["paused"] == 3
        assert manager.resume_all() == 3
        assert manager.get_portfolio_summary()["running"] == 3

    def test_stop_all_and_remove(self, manager):
        id1, id2, id3 = self._add_demo_runners(manager)
        assert manager.stop_all() == 3
        assert manager.remove_runner(id1) is True
        assert manager.get_portfolio_summary()["runner_count"] == 2
        assert manager.remove_runner("nonexistent") is False

    def test_emergency_flatten(self, manager):
        self._add_demo_runners(manager)
        manager.feed.warmup()
        for _ in range(10):
            manager.tick()
        # force some positions
        broker = manager.broker
        rid = next(iter(manager._runners))
        r = manager._runners[rid]
        broker.submit_market(rid, "BTC/USD", SIDE_BUY, 10, 100.0)
        assert len(r.positions) >= 1
        count = manager.emergency_flatten_all()
        assert count >= 1
        assert manager.halted
        for runner in manager._runners.values():
            assert len(runner.positions) == 0

    def test_deployed_capital_aggregation(self, manager):
        self._add_demo_runners(manager)
        manager.feed.warmup()
        for _ in range(20):
            manager.tick()
        summary = manager.get_portfolio_summary()
        # Deployed can only be between 0 and total capital
        assert 0 <= summary["deployed_capital"] <= summary["total_capital"]


class TestRiskSupervisor:
    def test_daily_loss_breach(self):
        sup = RiskSupervisor(GlobalRiskConfig(daily_loss_limit=10_000, breach_mode=HALT_PAUSE))
        msg = sup.check_portfolio_daily_loss(-12_000)
        assert msg is not None
        assert sup.check_portfolio_daily_loss(-5_000) is None

    def test_drawdown_breach(self):
        sup = RiskSupervisor(GlobalRiskConfig(max_drawdown_pct=0.10))
        assert sup.check_portfolio_max_drawdown(89_000, 100_000) is not None
        assert sup.check_portfolio_max_drawdown(95_000, 100_000) is None

    def test_evaluate_daily_loss_pause_mode(self):
        sup = RiskSupervisor(
            GlobalRiskConfig(daily_loss_limit=10_000, max_drawdown_pct=0.5, breach_mode=HALT_PAUSE)
        )
        report = sup.evaluate(
            runners=[], total_equity=90_000, peak_equity=100_000, daily_pnl=-15_000
        )
        assert report.halted
        assert report.halt_mode == HALT_PAUSE
        assert report.state == STATE_HALTED

    def test_evaluate_drawdown_forces_flatten(self):
        sup = RiskSupervisor(GlobalRiskConfig(daily_loss_limit=10_000, max_drawdown_pct=0.10))
        report = sup.evaluate(
            runners=[], total_equity=80_000, peak_equity=100_000, daily_pnl=-5_000
        )
        assert report.halted
        assert report.halt_mode == HALT_FLATTEN

    def test_concentration_warning(self, manager):
        """3+ LONG positions in crypto group → HIGH_CONCENTRATION warning."""
        rid = manager.add_runner(
            RunnerConfig(
                name="Crypto",
                strategy_name="rsi_reversion",
                allocated_capital=1_000_000,
                target_type=TARGET_POOL,
                universe_id="TOP_10_CRYPTO",
                timeframe="1hour",
                max_pool_positions=5,
            )
        )
        r = manager.get_runner(rid)
        broker = manager.broker
        for sym in ("BTC/USD", "ETH/USD", "SOL/USD"):
            broker.submit_market(rid, sym, SIDE_BUY, 1, 100.0)
        warnings = manager.supervisor.check_correlation_concentration([r])
        assert len(warnings) == 1
        assert warnings[0]["kind"] == "HIGH_CONCENTRATION"
        assert set(["BTC/USD", "ETH/USD", "SOL/USD"]).issubset(set(warnings[0]["symbols"]))


class TestCircuitBreakerIntegration:
    def test_crash_trips_and_flattens(self):
        mgr = PortfolioManager(
            risk_config=GlobalRiskConfig(
                daily_loss_limit=50_000, max_drawdown_pct=0.08, breach_mode=HALT_FLATTEN
            ),
            warmup_bars=20,
            auto_start_feed=False,
        )
        try:
            mgr.add_runner(
                RunnerConfig(
                    name="Pool",
                    strategy_name="donchian_breakout",
                    allocated_capital=1_000_000,
                    target_type=TARGET_POOL,
                    symbols=[f"S{j}" for j in range(8)],
                    timeframe="1day",
                    max_pool_positions=4,
                )
            )
            mgr.feed.warmup()
            for _ in range(10):
                mgr.tick()

            pre = mgr.get_portfolio_summary()
            assert pre["halted"] is False

            import time

            t0 = time.perf_counter()
            mgr.simulate_crash(crash_pct=0.25)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            post = mgr.get_portfolio_summary()
            assert post["halted"] is True
            assert elapsed_ms < 500  # Task 7.2: halt within 500ms
            # Flatten mode closes all positions
            assert post["open_positions"] == 0
            assert all(r.status == STATUS_PAUSED for r in mgr._runners.values())
        finally:
            mgr.shutdown()

    def test_reset_clears_latch(self):
        mgr = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=10_000, max_drawdown_pct=0.05),
            warmup_bars=15,
            auto_start_feed=False,
        )
        try:
            rid = mgr.add_runner(
                RunnerConfig(
                    name="P",
                    strategy_name="donchian_breakout",
                    allocated_capital=1_000_000,
                    target_type=TARGET_POOL,
                    symbols=[f"S{j}" for j in range(6)],
                    timeframe="1day",
                    max_pool_positions=3,
                )
            )
            mgr.feed.warmup()
            for _ in range(5):
                mgr.tick()
            # Guarantee an open position so the crash moves the mark.
            runner = mgr.get_runner(rid)
            for sym in list(runner.config.symbols)[:3]:
                mgr.broker.submit_market(rid, sym, SIDE_BUY, 100, 500.0)
            assert runner.equity() > 0 and len(runner.positions) >= 3
            mgr.simulate_crash(crash_pct=0.4)
            assert mgr.get_portfolio_summary()["halted"]
            mgr.reset_circuit_breaker()
            assert not mgr.get_portfolio_summary()["halted"]
        finally:
            mgr.shutdown()
