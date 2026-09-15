"""Task A2 — the option book must be visible where the equity book is.

Gap **P2** in the remediation PRD: ``StrategyRunner``'s option book lived in a
side-dict (``get_state()["options"]``), so the runner card, the bucket
aggregates, the portfolio totals and the instance circuit breakers all
reported the *untouched equity portfolio* — an option runner could lose 20% of
its allocation and never trip a breaker.

A2 folds ``OptionsBridge.net_pnl`` into ``equity()`` / ``realized_pnl``,
``unrealized_pnl()`` into the open-leg mark, and ``premium_at_risk`` into
``deployed_capital()``. Equity runners must be bit-identical.

See ``docs/OPTIONS-FORWARD-TESTING.md`` → task A2.
"""

from __future__ import annotations

import pytest

from backtest.forward.paper_runner import (
    STATUS_PAUSED,
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(closes, start_day=1):
    return [
        {
            "ts": f"2026-09-{start_day + i:02d}T09:15:00",
            "open": close - 10,
            "high": close + 30,
            "low": close - 30,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _option_config(**overrides):
    kwargs = dict(
        name="opt-runner",
        strategy_name="directional_options",
        allocated_capital=1_000_000,
        symbols=["NIFTY"],
        timeframe="1day",
        instrument={"type": "option"},
    )
    kwargs.update(overrides)
    return RunnerConfig(**kwargs)


def _equity_config(**overrides):
    kwargs = dict(
        name="eq-runner",
        strategy_name="sma_crossover",
        allocated_capital=100_000,
        symbols=["NIFTY"],
        timeframe="1day",
    )
    kwargs.update(overrides)
    return RunnerConfig(**kwargs)


def _feed(runner, bars):
    for bar in bars:
        runner.process_candle_event("NIFTY", bar)


RISING = [24800 + i * 40 for i in range(20)]
CRASH = [RISING[-1] - i * 300 for i in range(1, 25)]


@pytest.fixture()
def holding_runner():
    """Option runner holding one bull call spread through a rally."""
    runner = StrategyRunner(_option_config(), ledger=OrderLedger())
    runner.start()
    _feed(runner, _bars(RISING))
    assert runner.options_summary()["open_structures"] == 1
    return runner


# ---------------------------------------------------------------------------
# Equity carries the book
# ---------------------------------------------------------------------------


class TestEquityIncludesOptionBook:
    def test_option_pnl_is_the_difference_from_capital(self, holding_runner):
        bridge = holding_runner.options_bridge
        assert holding_runner.option_pnl() == pytest.approx(float(bridge.net_pnl))

    def test_equity_is_portfolio_plus_book(self, holding_runner):
        runner = holding_runner
        portfolio_equity = float(runner.portfolio.calculate_total_equity())
        assert runner.equity() == pytest.approx(portfolio_equity + runner.option_pnl())

    def test_state_equity_matches_book_equity(self, holding_runner):
        """A pure option runner's row equity *is* its option book's equity."""
        state = holding_runner.get_state()
        assert state["equity"] == pytest.approx(state["options"]["equity"])
        assert state["equity"] != pytest.approx(state["allocated_capital"])

    def test_equity_falls_with_the_underlying(self, holding_runner):
        before = holding_runner.equity()
        _feed(holding_runner, _bars(CRASH, start_day=21))
        after = holding_runner.equity()
        assert after < before - 1_000

    def test_unrealized_pnl_reports_open_legs(self, holding_runner):
        runner = holding_runner
        assert runner.unrealized_pnl() == pytest.approx(
            float(runner.portfolio.unrealized_pnl)
            + float(runner.options_bridge.unrealized_pnl)
        )
        assert runner.unrealized_pnl() > 0  # rally kept helping the spread

    def test_deployed_capital_is_premium_at_risk(self, holding_runner):
        runner = holding_runner
        structure = runner.options_bridge.option_broker.get_open_structures()[0]
        assert runner.deployed_capital() == pytest.approx(float(structure.total_entry_cost))
        assert runner.deployed_capital() == pytest.approx(
            float(runner.options_bridge.premium_at_risk)
        )

    def test_daily_pnl_uses_the_combined_equity(self, holding_runner):
        baseline = holding_runner._day_start_equity
        _feed(holding_runner, _bars(CRASH, start_day=21))
        assert holding_runner.daily_pnl() == pytest.approx(
            holding_runner.equity() - baseline
        )
        assert holding_runner.daily_pnl() < 0

    def test_realized_pnl_zero_while_open(self, holding_runner):
        assert holding_runner.realized_pnl == 0.0

    def test_closed_structure_lands_in_realized_pnl(self, holding_runner):
        """Close the spread by hand: P&L must move from unrealized to realized."""
        runner = holding_runner
        bridge = runner.options_bridge
        structure_id = bridge.open_structure_id
        broker = bridge.option_broker
        unrealized_before = runner.unrealized_pnl()
        assert unrealized_before > 0

        broker.close_structure(structure_id, bridge.quote_provider, reason="manual")
        assert broker.get_open_structures() == []

        booked = float(broker.total_realized_pnl)
        assert runner.realized_pnl == pytest.approx(booked)
        # Net of the cost stack, and the open-leg mark is gone.
        assert runner.option_pnl() == pytest.approx(
            booked - float(broker.total_costs_paid)
        )
        assert runner.unrealized_pnl() == 0.0
        assert runner.deployed_capital() == 0.0
        # Equity still reconciles: capital + book P&L.
        assert runner.equity() == pytest.approx(1_000_000 + runner.option_pnl())
        assert runner.get_state()["equity"] == pytest.approx(
            runner.get_state()["options"]["equity"]
        )


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------


class TestBreakersSeeOptionDrawdown:
    def test_option_loss_trips_the_instance_drawdown_breaker(self):
        """1% drawdown limit + a 28% NIFTY collapse on a 50-wide spread.

        The spread's max loss is its net debit (~₹2,063, ≈2% of a ₹100k
        allocation), so a 1% limit is breached with room to spare rather than
        sitting on the boundary — this must not turn into a flaky test if the
        synthetic premium moves.
        """
        runner = StrategyRunner(
            _option_config(allocated_capital=100_000, max_drawdown_pct=0.01),
            ledger=OrderLedger(),
        )
        runner.start()
        _feed(runner, _bars(RISING))
        assert runner.status == "RUNNING"

        _feed(runner, _bars(CRASH, start_day=21))

        assert runner.max_drawdown_pct >= 0.01
        assert runner.status == STATUS_PAUSED
        assert "drawdown" in (runner.error or "")
        halts = [s for s in runner.signal_log if s["kind"] == "RISK_HALT"]
        assert halts and "drawdown" in halts[-1]["reason"]

    def test_no_breaker_without_the_fold(self):
        """Same loss, a limit it cannot breach → still running (control case)."""
        runner = StrategyRunner(
            _option_config(allocated_capital=1_000_000, max_drawdown_pct=0.9),
            ledger=OrderLedger(),
        )
        runner.start()
        _feed(runner, _bars(RISING))
        _feed(runner, _bars(CRASH, start_day=21))
        assert runner.status == "RUNNING"
        assert runner.max_drawdown_pct < 0.9

    def test_equity_curve_records_the_combined_equity(self, holding_runner):
        runner = holding_runner
        runner._mark_to_market(record=True)
        assert runner.equity_curve[-1]["equity"] == pytest.approx(runner.equity())


# ---------------------------------------------------------------------------
# Buckets / portfolio aggregates
# ---------------------------------------------------------------------------


class TestBucketAggregates:
    def test_bucket_equity_includes_the_option_book(self):
        manager = PortfolioManager(
            risk_config=GlobalRiskConfig(
                daily_loss_limit=10_000_000, max_drawdown_pct=0.99
            ),
            auto_start_feed=False,
        )
        try:
            option_id = manager.add_runner(_option_config(), start=True)
            equity_id = manager.add_runner(_equity_config(), start=True)

            for bar in _bars(RISING):
                manager._on_bar("NIFTY", bar)
            for bar in _bars(CRASH, start_day=21):
                manager._on_bar("NIFTY", bar)

            option_runner = manager.get_runner(option_id)
            equity_runner = manager.get_runner(equity_id)
            buckets = manager.get_bucket_aggregates()
            paper = buckets["paper"]

            # The option loss is inside the bucket's equity and drawdown.
            assert option_runner.equity() < 1_000_000 - 1_000
            assert paper["equity"] == pytest.approx(
                option_runner.equity() + equity_runner.equity(), rel=1e-6
            )
            assert paper["daily_pnl"] == pytest.approx(
                option_runner.daily_pnl() + equity_runner.daily_pnl(), rel=1e-6
            )
            assert paper["deployed_capital"] == pytest.approx(
                option_runner.deployed_capital() + equity_runner.deployed_capital(),
                rel=1e-6,
            )
            assert paper["drawdown_pct"] > 0
        finally:
            manager.shutdown()

    def test_portfolio_total_includes_the_option_book(self):
        manager = PortfolioManager(
            risk_config=GlobalRiskConfig(
                daily_loss_limit=10_000_000, max_drawdown_pct=0.99
            ),
            auto_start_feed=False,
        )
        try:
            option_id = manager.add_runner(_option_config(), start=True)
            for bar in _bars(RISING):
                manager._on_bar("NIFTY", bar)
            for bar in _bars(CRASH, start_day=21):
                manager._on_bar("NIFTY", bar)

            runner = manager.get_runner(option_id)
            assert manager.get_portfolio_summary()["total_equity"] == pytest.approx(
                runner.equity(), rel=1e-6
            )
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# Equity runners: bit-identical
# ---------------------------------------------------------------------------


class TestEquityRunnersUnchanged:
    def test_equity_numbers_are_pure_portfolio_numbers(self):
        runner = StrategyRunner(_equity_config(), ledger=OrderLedger())
        runner.start()
        _feed(runner, _bars(RISING))
        _feed(runner, _bars(CRASH, start_day=21))

        assert runner.option_pnl() == 0.0
        assert runner.equity() == float(runner.portfolio.calculate_total_equity())
        assert runner.unrealized_pnl() == float(runner.portfolio.unrealized_pnl)
        assert runner.realized_pnl == float(runner.portfolio.realized_pnl)
        assert runner.deployed_capital() == pytest.approx(
            sum(p["qty"] * p["entry_price"] for p in runner.positions.values())
        )
        assert runner.daily_pnl() == pytest.approx(
            runner.equity() - runner._day_start_equity
        )

    def test_option_metrics_are_zero_for_equity_runners(self):
        runner = StrategyRunner(_equity_config(), ledger=OrderLedger())
        runner.start()
        state = runner.get_state()
        assert state["options"] is None
        assert state["option_pnl"] == 0.0
        assert state["deployed_capital"] == 0.0
