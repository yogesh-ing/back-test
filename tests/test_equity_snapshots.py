"""On-demand equity snapshot views (owner decision 2026-09-21).

The combined equity curve used to tick per SSE frame; the owner asked for
ask-based views instead. These tests verify the snapshot math synthetically:
per-runner rows, day-close derivation from runner curves, session summary,
mode scoping, and the API endpoint wiring — no live market involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtest.forward.paper_runner import (
    RunnerConfig,
    TARGET_SINGLE,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig


@pytest.fixture
def manager():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        warmup_bars=20,
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


def _seed_curve(runner, days_prices):
    """Append synthetic equity_curve points (ts ISO, equity) to a runner."""
    base = datetime.now(timezone.utc) - timedelta(days=10)
    i = 0
    for day_offset, prices in days_prices:
        for price in prices:
            ts = base + timedelta(days=day_offset, minutes=i)
            runner.equity_curve.append(
                {"ts": ts.isoformat(), "equity": price}
            )
            i += 1


class TestEquitySnapshots:
    def test_empty_book_returns_zeros_not_errors(self, manager):
        snap = manager.get_equity_snapshots()
        assert snap["mode"] == "all"
        assert snap["per_runner"] == []
        assert snap["day_closes"] == []
        assert snap["session"]["equity"] == 0
        assert snap["session"]["win_rate"] is None

    def test_per_runner_rows_carry_the_comparison_fields(self, manager):
        rid = manager.add_runner(
            RunnerConfig(
                name="RSI",
                strategy_name="rsi_reversion",
                allocated_capital=100_000,
                target_type=TARGET_SINGLE,
                symbols=["BTC/USD"],
                timeframe="1hour",
                mode="paper",
            ),
            start=False,
        )
        snap = manager.get_equity_snapshots()
        rows = snap["per_runner"]
        assert len(rows) == 1
        row = rows[0]
        assert row["instance_id"] == rid
        assert row["name"] == "RSI"
        assert row["strategy"] == "rsi_reversion"
        assert row["equity"] == 100_000  # flat runner: equity == allocation
        assert row["trades"] >= 0
        assert snap["session"]["allocated_capital"] == 100_000

    def test_day_closes_keep_last_point_per_day(self, manager):
        rid = manager.add_runner(
            RunnerConfig(
                name="RSI",
                strategy_name="rsi_reversion",
                allocated_capital=100_000,
                target_type=TARGET_SINGLE,
                symbols=["BTC/USD"],
                timeframe="1hour",
                mode="paper",
            ),
            start=False,
        )
        runner = manager._runners[rid]
        _seed_curve(
            runner,
            [
                (0, [100.0, 105.0, 102.0]),  # day 1: closes at 102
                (1, [110.0, 108.0]),  # day 2: closes at 108
            ],
        )
        snap = manager.get_equity_snapshots()
        days = {d["date"]: d["equity"] for d in snap["day_closes"]}
        assert len(days) == 2
        for equity in days.values():
            assert equity in (102.0, 108.0)

    def test_mode_scoping_filters_runners(self, manager):
        manager.add_runner(
            RunnerConfig(
                name="PaperA",
                strategy_name="rsi_reversion",
                allocated_capital=50_000,
                target_type=TARGET_SINGLE,
                symbols=["BTC/USD"],
                timeframe="1hour",
                mode="paper",
            ),
            start=False,
        )
        snap_paper = manager.get_equity_snapshots(mode="paper")
        assert snap_paper["mode"] == "paper"
        assert all(r["mode"] == "paper" for r in snap_paper["per_runner"])

        with pytest.raises(ValueError):
            manager.get_equity_snapshots(mode="bogus")

    def test_intraday_points_carry_timestamps(self, manager):
        rid = manager.add_runner(
            RunnerConfig(
                name="RSI",
                strategy_name="rsi_reversion",
                allocated_capital=100_000,
                target_type=TARGET_SINGLE,
                symbols=["BTC/USD"],
                timeframe="1hour",
                mode="paper",
            ),
            start=False,
        )
        runner = manager._runners[rid]
        today = datetime.now(timezone.utc)
        runner.equity_curve.append(
            {"ts": today.isoformat(), "equity": 100_123.0}
        )
        snap = manager.get_equity_snapshots()
        assert snap["intraday_today"], "today's point should appear intraday"
        assert snap["intraday_today"][-1]["equity"] == 100_123.0


class TestEquitySnapshotEndpoint:
    def test_endpoint_is_registered_and_wired_to_manager(self):
        """Route exists on the blueprint and delegates to get_equity_snapshots."""
        import inspect

        from backtest.api import portfolio as mod

        src = inspect.getsource(mod)
        assert "/api/portfolio/equity/snapshot" in src
        assert "get_equity_snapshots" in src

    def test_manager_method_rejects_bad_mode(self, manager):
        with pytest.raises(ValueError):
            manager.get_equity_snapshots(mode="NOT_A_MODE")

    def test_portfolio_win_rate_is_trade_weighted_not_fraction_summed(self, manager):
        """2026-09-22 regression: session win_rate summed per-runner FRACTIONS
        and divided by total trades (2 runners × 50% → 5%). It must be
        won-trades / total-trades across the whole book."""
        rids = []
        for name in ("A", "B"):
            rids.append(
                manager.add_runner(
                    RunnerConfig(
                        name=name,
                        strategy_name="rsi_reversion",
                        allocated_capital=50_000,
                        target_type=TARGET_SINGLE,
                        symbols=["BTC/USD"],
                        timeframe="1hour",
                        mode="paper",
                    ),
                    start=False,
                )
            )
        # Runner A: 1 win, 1 loss (50%). Runner B: 3 wins, 1 loss (75%).
        # Seed via the Position round-trip (closed_trades is derived from
        # portfolio.closed_positions — inject the same way the state store does).
        from datetime import datetime as _dt

        from backtest.simulator.position import Position

        outcomes = [(120.0, -10.0), (100.0, 30.0, 40.0, -20.0)]
        for rid, pnls in zip(rids, outcomes):
            portfolio = manager._runners[rid].portfolio
            for pnl in pnls:
                pos = Position(
                    symbol="BTC/USD",
                    quantity=1,
                    average_entry_price=100,
                    current_price=100,
                )
                pos.quantity = pos.quantity.__class__(0)  # closed: zero size
                pos.realized_pnl = pos.realized_pnl.__class__(str(pnl))
                pos.closed_at = _dt(2026, 9, 22, 10, 0)
                portfolio.closed_positions.append(pos)
        snap = manager.get_equity_snapshots()
        session = snap["session"]
        assert session["trades_total"] == 6
        assert session["wins"] == 4
        assert session["win_rate"] == round(4 / 6, 4)  # ~0.6667, NOT sum(0.5,0.75)/6=0.2083
