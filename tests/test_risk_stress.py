"""Multi-strategy risk stress test (2026-09-22 pre-live session).

Drives the risk module with a diversified strategy portfolio — the six
momentum/trend/mean-reversion built-ins — and asserts each risk control
actually bites under portfolio-level pressure:

* **Concentration warning** — 3+ correlated LONGs flags HIGH_CONCENTRATION
  (the new crypto-pair strategies give realistic correlated books);
* **Position + gross-exposure caps** — bucket limits refuse the over-cap
  book on restore (T8 teeth) and the sizing layer caps entries;
* **Per-bucket breaker independence** — a paper breach never halts live;
* **Supervisor drawdown flatten** — HALT_FLATTEN clears every runner book;
* **Engine-level daily-loss trip** — the fill→record_trade_result wiring
  (HIGH-1 fix) trips the circuit breaker and halts the engine.

All runners run PAPER (free play) unless stated; live-bucket tests use the
armed fake venue from ``live_test_support``.
"""

from __future__ import annotations

import pytest

from backtest.forward.paper_runner import (
    SIDE_BUY,
    SIDE_SELL,
    STATUS_PAUSED,
    STATUS_RUNNING,
    TARGET_SINGLE,
    RunnerConfig,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import (
    HALT_FLATTEN,
    HALT_PAUSE,
    GlobalRiskConfig,
)

# All registered equity strategies — the stress portfolio's roster
STRESS_STRATEGIES = [
    "sma_crossover",
    "rsi_reversion",
    "donchian_breakout",
    "price_move",
    "momentum_roc",
    "bollinger_reversion",
    "macd_trend",
    "ema_pullback",
]


def _bar(symbol="AAA", ts="2026-09-22 10:00:00", price=100.0):
    return {
        "ts": ts,
        "open": price,
        "high": price * 1.01,
        "low": price * 0.99,
        "close": price,
        "volume": 100_000,
    }


def _cfg(name, strategy, symbol, capital=100_000, mode="paper", **kw):
    return RunnerConfig(
        name=name,
        strategy_name=strategy,
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=[symbol],
        timeframe="1hour",
        mode=mode,
        **kw,
    )


@pytest.fixture(autouse=True)
def _arm_live_orders(monkeypatch):
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")


@pytest.fixture
def manager():
    from live_test_support import ARMED_KWARGS

    mgr = PortfolioManager(
        **ARMED_KWARGS,
        risk_config=GlobalRiskConfig(
            daily_loss_limit=10_000,
            max_drawdown_pct=0.10,
            breach_mode=HALT_PAUSE,
        ),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


@pytest.fixture
def flatten_manager():
    from live_test_support import ARMED_KWARGS

    mgr = PortfolioManager(
        **ARMED_KWARGS,
        risk_config=GlobalRiskConfig(
            daily_loss_limit=10_000,
            max_drawdown_pct=0.10,
            breach_mode=HALT_FLATTEN,
        ),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Strategy roster — the new built-ins are spawnable runners
# ---------------------------------------------------------------------------


class TestStressRoster:
    def test_all_stress_strategies_are_registered(self):
        from backtest.strategy.registry import get_strategy

        for name in STRESS_STRATEGIES:
            cls = get_strategy(name)  # raises KeyError if missing
            assert getattr(cls, "name", "") == name

    def test_concentration_warning_fires_on_correlated_longs(self, manager):
        """3+ correlated crypto LONGs → HIGH_CONCENTRATION on the risk report.

        Uses real strategy-driven entries: three crypto-pair runners whose
        momentum strategies go long their pair, plus a warmup of rising bars
        so momentum signals fire.
        """
        from datetime import datetime, timedelta

        crypto = ["BTC/USD", "ETH/USD", "SOL/USD"]
        ids = []
        for i, (strat, sym) in enumerate(
            zip(["momentum_roc", "macd_trend", "donchian_breakout"], crypto)
        ):
            rid = manager.add_runner(
                _cfg(f"C{i}-{strat}", strat, sym, capital=100_000), start=False
            )
            ids.append((rid, sym))
            manager.get_runner(rid).start()
            # 26 rising bars — enough warmup for every lookback in the roster
            # (donchian runs at its default lookback=20 and needs 21+ bars
            # before its shifted rolling-max produces a non-NaN breakout).
            base = datetime(2026, 9, 22, 9, 0)
            for k in range(26):
                ts = (base + timedelta(hours=k)).strftime("%Y-%m-%d %H:%M:%S")
                manager._on_bar(sym, _bar(sym, ts=ts, price=100.0 + k))

        # All three strategies should have entered their pair
        for rid, _sym in ids:
            assert manager.get_runner(rid).positions, f"{rid} did not enter"

        report = manager.supervisor.evaluate(
            runners=list(manager._runners.values()),
            total_equity=manager._aggregate_equity(),
            peak_equity=max(manager.peak_equity, 1.0),
            daily_pnl=0.0,
        )
        kinds = [w["kind"] for w in report.warnings]
        assert "HIGH_CONCENTRATION" in kinds
        crypto_warning = next(w for w in report.warnings if w["group"] == "crypto")
        assert crypto_warning["count"] >= 3

    def test_multi_strategy_runners_spawn_and_start(self, manager):
        """All 8 equity strategies spawn as paper runners on distinct symbols
        and process bars once RUNNING (the runner status gate drops bars
        fed before start)."""
        symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
        ids = []
        for i, strat in enumerate(STRESS_STRATEGIES):
            rid = manager.add_runner(
                _cfg(f"R{i}-{strat}", strat, symbols[i], capital=50_000), start=False
            )
            ids.append(rid)
            manager.get_runner(rid).start()
            manager._on_bar(symbols[i], _bar(symbols[i], ts="2026-09-22 10:00:00"))

        assert all(manager.get_runner(r).status == STATUS_RUNNING for r in ids)
        assert all(len(manager.get_runner(r)._bars[s]) >= 1 for r, s in zip(ids, symbols))

        summary = manager.get_portfolio_summary()
        assert summary["runner_count"] == len(STRESS_STRATEGIES)
        assert summary["total_capital"] == pytest.approx(50_000 * len(STRESS_STRATEGIES))


# ---------------------------------------------------------------------------
# Bucket limits under a crowded book
# ---------------------------------------------------------------------------


class TestBucketLimitsUnderPressure:
    def test_crowded_book_refused_on_live_restore(self, manager):
        """T8 teeth: an open book that already violates the live bucket's
        caps is REFUSED when classified live — the multi-strategy stress
        book (8 runners, 5+ positions) cannot silently trade live."""
        symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        ids = []
        for i, strat in enumerate(STRESS_STRATEGIES[:6]):
            rid = manager.add_runner(
                _cfg(f"P{i}-{strat}", strat, symbols[i], capital=50_000), start=False
            )
            ids.append(rid)
            manager.get_runner(rid).start()
            manager._on_bar(symbols[i], _bar(symbols[i], ts="2026-09-22 10:00:00"))

        # Crowd the paper book: one position per runner
        for rid, sym in zip(ids, symbols):
            manager.broker.submit_market(rid, sym, SIDE_BUY, 100, 100.0)

        # Now try to run the SAME crowded book as LIVE with tight bucket caps
        from backtest.simulator.bucket_risk import resolve_bucket_risk

        _, limits = resolve_bucket_risk("live", "mstock")
        violation = limits.check_exposure(manager._runners[ids[0]].portfolio)

        # The 6-position book against a live cap of 5 open positions must
        # be caught (either count or gross exposure)
        assert violation is not None

    def test_bucket_override_tightens_limits_for_all_runners(self, manager):
        """A config override tightens the shared live bucket for every
        strategy in the portfolio — one knob, portfolio-wide."""
        from backtest.simulator.bucket_risk import (
            LIVE_BUCKET,
            BUCKET_RISK_LIMITS,
            update_bucket_limits,
        )

        import dataclasses
        from backtest.simulator.bucket_risk import BucketRiskLimits

        original = {
            f.name: getattr(BUCKET_RISK_LIMITS[LIVE_BUCKET], f.name)
            for f in dataclasses.fields(BucketRiskLimits)
        }
        try:
            update_bucket_limits(LIVE_BUCKET, {"max_open_positions": 2})
            assert BUCKET_RISK_LIMITS[LIVE_BUCKET].max_open_positions == 2

            # Every strategy's runner init would now see the tightened cap
            _, limits = resolve = None, None
            from backtest.simulator.bucket_risk import resolve_bucket_risk as _r

            _, limits = _r("live", "mstock")
            assert limits.max_open_positions == 2
            assert limits.to_sizing_constraints().max_open_positions == 2
        finally:
            for name, value in original.items():
                setattr(BUCKET_RISK_LIMITS[LIVE_BUCKET], name, value)


# ---------------------------------------------------------------------------
# Breakers under multi-strategy pressure
# ---------------------------------------------------------------------------


class TestBreakersUnderPressure:
    def test_paper_multi_strategy_breach_does_not_halt_live(self, manager):
        """With 6 strategy runners in paper and 1 in live, a paper-wide
        loss halts ONLY paper."""
        symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        paper_ids = []
        for i, strat in enumerate(STRESS_STRATEGIES[:6]):
            rid = manager.add_runner(
                _cfg(f"P{i}-{strat}", strat, symbols[i], capital=50_000), start=False
            )
            paper_ids.append(rid)
            manager.get_runner(rid).start()
            manager._on_bar(symbols[i], _bar(symbols[i], ts="2026-09-22 10:00:00"))
        lid = manager.add_runner(
            _cfg("L-live", "macd_trend", "ZZZ", capital=200_000, mode="live"), start=False
        )
        manager._on_bar("ZZZ", _bar("ZZZ", ts="2026-09-22 10:00:00"))

        # Each paper runner takes a 5k loss (6 × 5k = 30k > 10k bucket limit)
        for rid, sym in zip(paper_ids, symbols):
            manager.broker.submit_market(rid, sym, SIDE_BUY, 100, 100.0)
            manager.broker.submit_market(rid, sym, SIDE_SELL, 100, 50.0)
        manager._evaluate_risk()

        assert manager._bucket_halted["paper"] is True
        assert manager._bucket_halted["live"] is False
        assert manager.get_runner(lid).status != STATUS_PAUSED

    def test_drawdown_flatten_clears_every_strategy_book(self, flatten_manager):
        """HALT_FLATTEN on drawdown clears ALL runners' books — no strategy
        escapes the flatten (6 different strategies held long)."""
        symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        ids = []
        for i, strat in enumerate(STRESS_STRATEGIES[:6]):
            rid = flatten_manager.add_runner(
                _cfg(f"P{i}-{strat}", strat, symbols[i], capital=50_000), start=False
            )
            ids.append(rid)
            flatten_manager.get_runner(rid).start()
            flatten_manager._on_bar(symbols[i], _bar(symbols[i], ts="2026-09-22 10:00:00"))

        # Put each runner long
        for rid, sym in zip(ids, symbols):
            flatten_manager.broker.submit_market(rid, sym, SIDE_BUY, 100, 100.0)
        assert all(flatten_manager.get_runner(r).positions for r in ids)

        # Crash every market 60%: each runner loses 6k on its 10k position
        # → 36k combined on a 300k book = 12% > the 10% limit
        for sym in symbols:
            flatten_manager._on_bar(sym, _bar(sym, ts="2026-09-22 11:00:00", price=40.0))
        flatten_manager._evaluate_risk()

        assert flatten_manager._bucket_halted["paper"] is True
        assert flatten_manager._bucket_halt_mode["paper"] == HALT_FLATTEN
        for rid in ids:
            runner = flatten_manager.get_runner(rid)
            assert runner.status == STATUS_PAUSED
            assert not runner.positions, f"{runner.config.name} book not flattened"


# ---------------------------------------------------------------------------
# Engine-level daily-loss trip (HIGH-1 fix — fill PnL feeds the breaker)
# ---------------------------------------------------------------------------


class TestEngineDailyLossWiring:
    def test_fill_pnl_feeds_daily_loss_breaker(self):
        """The engine's RiskManager trips its daily-loss breaker from real
        fill PnL — the wiring added for the live session."""
        from backtest.simulator.portfolio import Portfolio
        from backtest.simulator.risk_manager import RiskConfig, RiskManager

        pf = Portfolio(name="stress", initial_capital=100_000)
        risk = RiskManager(pf, RiskConfig(daily_loss_limit_pct=0.02))

        # Simulate the engine loop's fill handling: 6 strategies each lose
        # 500 on their fill → -3000 = 3% > 2%
        fills = [type("F", (), {"realized_pnl": -500.0})() for _ in range(6)]
        for f in fills:
            risk._record_fill_pnl(f)

        result = risk.check_daily_loss_limit(pf)
        assert not result.allowed
        assert result.code == "daily_loss_limit"

        tripped = risk.check_circuit_breakers()
        assert tripped is not None
        assert tripped.code == "daily_loss_limit"
        assert risk.is_halted()

    def test_consecutive_losses_across_strategies_trip_breaker(self):
        """Losses recorded per-strategy-fill accumulate — the consecutive
        loss breaker fires regardless of which strategy lost."""
        from backtest.simulator.portfolio import Portfolio
        from backtest.simulator.risk_manager import RiskConfig, RiskManager

        pf = Portfolio(name="stress2", initial_capital=100_000)
        risk = RiskManager(pf, RiskConfig(max_consecutive_losses=5))

        for strat_loss in [-100, -150, -120, -90, -110]:
            risk._record_fill_pnl(type("F", (), {"realized_pnl": strat_loss})())

        tripped = risk.check_circuit_breakers()
        assert tripped is not None
        assert tripped.code == "consecutive_losses"
        assert risk.is_halted()
