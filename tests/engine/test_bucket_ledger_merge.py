"""U3.2 — Merge manual options book into the bucket ledger

`get_portfolio_summary()` gains `dashboard_book` (the manual options book);
totals = runners + manual book. `emergency_flatten_all(mode)` closes **both**
books (kills the flatten bug). Manual trade rows surface in the new Portfolio
tab (U4.2).

Tests: summary totals include dashboard book; flatten closes a manual
structure (regression for the 2026-09-16 bug); AC-15-style invariant —
Live-page numbers === Overview live-card numbers.
"""

from decimal import Decimal

from backtest.forward.portfolio_manager import reset_portfolio_manager
from backtest.options.paper_trading import OptionPaperBroker
from backtest.options.quote_providers import SyntheticChainGenerator, SyntheticQuoteProvider
from backtest.strategy.intent import Direction, MarketView


def _view(direction, underlying="NIFTY", spot=25000):
    return MarketView(
        direction=direction,
        confidence=0.8,
        underlying=underlying,
        spot_price=Decimal(str(spot)),
    )


def test_summary_totals_include_dashboard_book():
    """Totals = runners + manual book."""
    mgr = reset_portfolio_manager(auto_start_feed=False)

    # Mock dashboard book by injecting into options_api module
    from backtest.web import options_api

    gen = SyntheticChainGenerator()
    provider = SyntheticQuoteProvider(chain_generator=gen)
    broker = OptionPaperBroker(capital=100000)

    # Open a structure in the dashboard broker
    from backtest.forward.options_bridge import OptionsBridge

    bridge = OptionsBridge(capital=100000, expression={"type": "long_call", "strike_selection": "atm", "quantity": 1}, option_broker=broker, quote_provider=provider)
    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    bridge.on_market_view(_view(Direction.BULLISH), "test")

    # Inject as singleton
    options_api._broker = broker
    options_api._quote_provider = provider

    summary = mgr.get_portfolio_summary()
    db = summary["dashboard_book"]
    assert db["exists"] is True
    assert db["open_structures"] == 1
    # Combined totals should include dashboard equity
    # Runner equity 0 (no runners), but dashboard equity should be included in total_equity
    # total_equity = runners equity + dashboard equity
    assert summary["total_equity"] >= db["equity"]
    assert summary["open_positions"] >= db["open_positions"]

    # Cleanup
    options_api._broker = None
    options_api._quote_provider = None
    mgr.shutdown()


def test_flatten_closes_manual_structure():
    """Flatten closes a manual structure — regression for 2026-09-16 bug."""
    mgr = reset_portfolio_manager(auto_start_feed=False)

    from backtest.web import options_api

    gen = SyntheticChainGenerator()
    provider = SyntheticQuoteProvider(chain_generator=gen)
    broker = OptionPaperBroker(capital=100000)

    from backtest.forward.options_bridge import OptionsBridge

    bridge = OptionsBridge(capital=100000, expression={"type": "long_call", "strike_selection": "atm", "quantity": 1}, option_broker=broker, quote_provider=provider)
    bridge.on_bar("NIFTY", 25000, ts="2026-09-16T09:15:00")
    bridge.on_market_view(_view(Direction.BULLISH), "test")

    assert len(broker.get_open_structures()) == 1

    options_api._broker = broker
    options_api._quote_provider = provider

    # Emergency flatten should close dashboard book too
    count = mgr.emergency_flatten_all(reason="test_flatten")
    # At least 1 from dashboard book
    assert count >= 1
    assert len(broker.get_open_structures()) == 0

    options_api._broker = None
    options_api._quote_provider = None
    mgr.shutdown()


def test_ac15_live_numbers_equals_overview():
    """AC-15 invariant — Live-page numbers === Overview live-card numbers.

    Buckets embedded in summary should match per-bucket endpoint.
    """
    mgr = reset_portfolio_manager(auto_start_feed=False)

    summary = mgr.get_portfolio_summary()
    buckets = summary["buckets"]
    # get_bucket_aggregates should equal summary's buckets
    direct_buckets = mgr.get_bucket_aggregates()
    assert buckets["paper"]["equity"] == direct_buckets["paper"]["equity"]
    assert buckets["live"]["equity"] == direct_buckets["live"]["equity"]

    # Scoped summary for live should match bucket live
    live_summary = mgr.get_portfolio_summary(mode="live")
    # When no runners, equity 0, but structure same
    assert live_summary["total_equity"] == buckets["live"]["equity"]

    mgr.shutdown()
