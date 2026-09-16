"""U3.1 — Options rows in Portfolio bucket view (read-only)

The original UX wound: option trades invisible on Portfolio.
Extend GET /api/portfolio/summary (or instance detail) to include per-structure
option rows from the runner books: structure_type, legs, entry/close, P&L,
exit_reason. Render read-only in the existing instance/bucket trade table.

Tests: API returns rows for a runner with an open + a settled structure.
"""

from decimal import Decimal

from backtest.forward.options_bridge import OptionsBridge
from backtest.options.paper_trading import OptionPaperBroker
from backtest.options.quote_providers import SyntheticChainGenerator, SyntheticQuoteProvider
from backtest.strategy.intent import Direction, MarketView
from backtest.forward.portfolio_manager import reset_portfolio_manager
from backtest.forward.paper_runner import RunnerConfig


def _view(direction, underlying="NIFTY", spot=25000):
    return MarketView(
        direction=direction,
        confidence=0.8,
        underlying=underlying,
        spot_price=Decimal(str(spot)),
    )


def test_runner_options_rows_visible():
    """Runner with option instrument returns options summary with open_structures_detail."""
    # Create an option runner via portfolio manager
    mgr = reset_portfolio_manager(auto_start_feed=False)

    config = RunnerConfig(
        name="test option runner",
        strategy_name="rsi_reversion",
        allocated_capital=100000,
        symbols=["NIFTY"],
        target_type="SINGLE_SYMBOL",
        instrument={"type": "option", "expression": {"type": "bull_call_spread", "strike_selection": "atm", "quantity": 1}},
        mode="paper",
    )
    instance_id = mgr.add_runner(config, start=False)
    runner = mgr.get_runner(instance_id)

    assert runner.options_bridge is not None

    # Simulate opening a structure via bridge
    view = _view(Direction.BULLISH)
    runner.options_bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    result = runner.options_bridge.on_market_view(view, "test")

    # Should have opened
    assert runner.options_bridge._has_open_structure()

    # Check summary includes open_structures_detail
    summary = runner.options_bridge.summary()
    assert "open_structures_detail" in summary
    detail = summary["open_structures_detail"]
    assert len(detail) == 1
    row = detail[0]
    # Check required fields per U3.1: structure_type, legs, entry/close, P&L, exit_reason
    assert "structure_type" in row
    assert "legs" in row or "legs_detail" in row
    assert "entry_price" in row
    assert "unrealized_pnl" in row
    # For open, exit_reason not yet, but structure_type present
    assert row["kind"] == "option"

    # Now close it
    view_bear = _view(Direction.BEARISH)
    runner.options_bridge.on_bar("NIFTY", 24900, ts="2026-09-16T10:15:00")
    closed = runner.options_bridge.on_market_view(view_bear, "test")
    assert closed is not None
    assert closed.get("exited") is True
    assert "reason" in closed  # exit_reason

    # Check closed trades visible via runner.closed_trades
    closed_trades = runner.closed_trades
    assert len(closed_trades) >= 1
    opt_trade = [t for t in closed_trades if t.get("kind") == "option"]
    assert len(opt_trade) >= 1
    assert "exit_reason" in opt_trade[0]
    assert "pnl" in opt_trade[0]

    # Check portfolio summary includes runner with options
    portfolio_summary = mgr.get_portfolio_summary()
    assert portfolio_summary["runner_count"] >= 1
    runners = portfolio_summary["runners"]
    opt_runner = [r for r in runners if r["instance_id"] == instance_id][0]
    assert "options" in opt_runner
    assert opt_runner["options"] is not None

    mgr.shutdown()


def test_portfolio_summary_includes_dashboard_book():
    """Portfolio summary includes dashboard_book with positions/structures."""
    mgr = reset_portfolio_manager(auto_start_feed=False)
    summary = mgr.get_portfolio_summary()
    # dashboard_book key should exist even if no broker
    assert "dashboard_book" in summary
    # When no dashboard broker, exists False
    # When broker exists, positions/structures present
    db = summary["dashboard_book"]
    assert "open_positions" in db
    assert "open_structures" in db
    mgr.shutdown()


def test_runner_detail_includes_options_trades():
    """Runner detail includes options trades in trades list."""
    mgr = reset_portfolio_manager(auto_start_feed=False)
    config = RunnerConfig(
        name="test option runner 2",
        strategy_name="rsi_reversion",
        allocated_capital=100000,
        symbols=["NIFTY"],
        target_type="SINGLE_SYMBOL",
        instrument={"type": "option", "expression": {"type": "long_call", "strike_selection": "atm", "quantity": 1}},
        mode="paper",
    )
    instance_id = mgr.add_runner(config, start=False)
    runner = mgr.get_runner(instance_id)

    view = _view(Direction.BULLISH)
    runner.options_bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    runner.options_bridge.on_market_view(view, "test")

    # Close to have settled
    view2 = _view(Direction.BEARISH)
    runner.options_bridge.on_bar("NIFTY", 24900, ts="2026-09-16T10:15:00")
    runner.options_bridge.on_market_view(view2, "test")

    detail = mgr.get_runner_detail(instance_id)
    assert "trades" in detail
    # trades should include option trade with exit_reason
    opt_trades = [t for t in detail["trades"] if t.get("kind") == "option"]
    assert len(opt_trades) >= 1
    assert "exit_reason" in opt_trades[0]
    assert "structure_type" in opt_trades[0]

    mgr.shutdown()
