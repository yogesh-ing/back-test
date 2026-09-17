"""U6.2 — the Shared Market Data Bus (``forward/feed_registry.py``).

The overload being fixed: every option runner built a **private**
``SyntheticChainGenerator`` + quote provider, so N runners on NIFTY held N
different views of NIFTY (each priced off its own last bar) and paid N times
the chain cost. The bus gives every runner:

* ONE bar feed per ``(source, symbol, timeframe)`` — :class:`FeedRegistry`,
  refcounted, evicted at zero subscribers;
* ONE chain generator per option underlying — :class:`ChainBus`, so two NIFTY
  runners price **identical** chains off the **same** spot;
* a per-runner (cheap) quote provider wired to the shared generator.

Pinned here:

* registry mechanics — refcounts, eviction, key normalisation, idempotent
  release, first-subscribe-requires-feed;
* the two-runner identity proof — ``bridge1._sync_market`` is visible in
  ``bridge2`` (one generator, one spot);
* manager integration — ``add_runner`` subscribes per symbol, option runners
  take exactly one chain subscription, ``remove_runner``/``stop()``/``shutdown()``
  drain every refcount to zero, and an equity runner never touches the chain bus.

Isolation: the bus is process-wide, so each test runs against a fresh
``reset_data_bus()`` and constructs its own manager AFTER the reset.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtest.forward.feed_registry import (
    ChainBus,
    FeedRegistry,
    get_chain_bus,
    get_feed_registry,
    option_quote_provider,
    reset_data_bus,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)

OPTION_EXPRESSION = {
    "type": "bull_call_spread",
    "strike_selection": "atm",
    "quantity": 1,
    "exit": {"min_days_to_expiry": 2},
}


def _option_runner_config(name: str, symbol: str = "NIFTY") -> RunnerConfig:
    return RunnerConfig(
        name=name,
        strategy_name="directional_options",
        allocated_capital=500_000,
        symbols=[symbol],
        timeframe="1day",
        mode="paper",
        source="synthetic",
        instrument={"type": "option", "expression": dict(OPTION_EXPRESSION)},
    )


def _equity_runner_config(name: str, symbol: str = "RELIANCE") -> RunnerConfig:
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=100_000,
        symbols=[symbol],
        timeframe="1day",
        mode="paper",
        source="synthetic",
    )


@pytest.fixture()
def bus():
    """A fresh process-wide bus per test (the singletons are global state)."""
    reset_data_bus()
    yield
    reset_data_bus()


@pytest.fixture()
def manager():
    """A fresh manager on a fresh bus; no background threads unless asked."""
    reset_data_bus()
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()
    reset_data_bus()


# ---------------------------------------------------------------------------
# FeedRegistry mechanics
# ---------------------------------------------------------------------------


class TestFeedRegistry:
    def test_first_subscribe_requires_feed(self, bus):
        with pytest.raises(KeyError):
            get_feed_registry().subscribe("synthetic", "NIFTY", "1day")

    def test_two_subscribers_same_key_share_one_feed(self, bus):
        reg = get_feed_registry()
        feed = object()  # any duck-typed feed object
        first = reg.subscribe("synthetic", "NIFTY", "1day", feed=feed)
        second = reg.subscribe("synthetic", "NIFTY", "1day", feed=object())
        assert first is feed and second is feed, "same key must return ONE feed"
        assert reg.subscriber_count("synthetic", "NIFTY", "1day") == 2

    def test_release_to_zero_evicts(self, bus):
        reg = get_feed_registry()
        reg.subscribe("synthetic", "NIFTY", "1day", feed=object())
        assert reg.release("synthetic", "NIFTY", "1day") == 0
        assert reg.subscriber_count("synthetic", "NIFTY", "1day") == 0
        # Re-subscribing starts clean (the old entry is gone, not decremented).
        with pytest.raises(KeyError):
            reg.subscribe("synthetic", "NIFTY", "1day")

    def test_release_only_at_zero_keeps_feed_for_others(self, bus):
        reg = get_feed_registry()
        feed = object()
        reg.subscribe("synthetic", "NIFTY", "1day", feed=feed)
        reg.subscribe("synthetic", "NIFTY", "1day")
        assert reg.release("synthetic", "NIFTY", "1day") == 1
        assert reg.subscriber_count("synthetic", "NIFTY", "1day") == 1
        assert reg.subscribe("synthetic", "NIFTY", "1day") is feed

    def test_different_timeframe_is_a_different_feed(self, bus):
        reg = get_feed_registry()
        daily = object()
        hourly = object()
        a = reg.subscribe("synthetic", "NIFTY", "1day", feed=daily)
        b = reg.subscribe("synthetic", "NIFTY", "1hour", feed=hourly)
        assert a is daily and b is hourly and a is not b
        assert reg.stats()["feeds"] == 2

    def test_release_unknown_key_is_idempotent_zero(self, bus):
        assert get_feed_registry().release("synthetic", "NOPE", "1day") == 0

    def test_keys_are_normalised(self, bus):
        reg = get_feed_registry()
        feed = object()
        reg.subscribe("Synthetic", "nifty", "1DAY", feed=feed)
        # Same key by any casing — one entry, count bumps, not a new feed.
        assert reg.subscribe("SYNTHETIC", "NIFTY", "1day") is feed
        assert reg.stats()["feeds"] == 1

    def test_stats_reports_feeds_and_subscriptions(self, bus):
        reg = get_feed_registry()
        reg.subscribe("synthetic", "NIFTY", "1day", feed=object())
        reg.subscribe("synthetic", "BANKNIFTY", "1day", feed=object())
        reg.subscribe("synthetic", "NIFTY", "1day")  # +1 subscription, same feed
        stats = reg.stats()
        assert stats == {"feeds": 2, "subscriptions": 3}

    def test_on_release_callback_fires_once_at_zero(self, bus):
        reg = get_feed_registry()
        stopped = []
        entry = reg._feeds.get(("x", "X", "1d"))
        assert entry is None  # precondition: fresh registry
        feed = object()
        reg.subscribe("synthetic", "TCS", "1day", feed=feed)
        reg._feeds[("synthetic", "TCS", "1day")].on_release = lambda: stopped.append(1)
        reg.release("synthetic", "TCS", "1day")
        reg.release("synthetic", "TCS", "1day")  # unknown now — no second call
        assert stopped == [1], "on_release fires exactly once, at zero subscribers"

    def test_singletons_are_process_wide(self, bus):
        assert get_feed_registry() is get_feed_registry()
        assert get_chain_bus() is get_chain_bus()
        assert isinstance(get_feed_registry(), FeedRegistry)
        assert isinstance(get_chain_bus(), ChainBus)


# ---------------------------------------------------------------------------
# ChainBus + quote provider
# ---------------------------------------------------------------------------


class TestChainBus:
    def test_acquire_is_lazy_and_shared(self, bus):
        cb = get_chain_bus()
        gen1 = cb.acquire("NIFTY")
        gen2 = cb.acquire("NIFTY")
        assert isinstance(gen1, SyntheticChainGenerator)
        assert gen1 is gen2, "one generator per underlying"
        assert cb.subscriber_count("NIFTY") == 2

    def test_release_evicts_at_zero(self, bus):
        cb = get_chain_bus()
        cb.acquire("BANKNIFTY")
        assert cb.release("BANKNIFTY") == 0
        assert cb.generator_count() == 0
        assert cb.release("BANKNIFTY") == 0  # idempotent

    def test_underlyings_do_not_share(self, bus):
        cb = get_chain_bus()
        nifty = cb.acquire("NIFTY")
        bank = cb.acquire("BANKNIFTY")
        assert nifty is not bank
        assert cb.generator_count() == 2

    def test_option_quote_provider_wraps_shared_generator(self, bus):
        gen = get_chain_bus().acquire("NIFTY")
        provider = option_quote_provider(gen)
        assert isinstance(provider, SyntheticQuoteProvider)
        assert provider.generator is gen, "provider must price off the SHARED generator"


# ---------------------------------------------------------------------------
# Manager integration: runners come and go, refcounts stay honest
# ---------------------------------------------------------------------------


class TestManagerBusIntegration:
    def test_two_nifty_option_runners_share_one_generator_and_spot(self, manager):
        """THE U6.2 proof: bridge1._sync_market is visible in bridge2."""
        id1 = manager.add_runner(_option_runner_config("NIFTY-A"))
        id2 = manager.add_runner(_option_runner_config("NIFTY-B"))
        bridge1 = manager.get_runner(id1).options_bridge
        bridge2 = manager.get_runner(id2).options_bridge
        assert bridge1 is not None and bridge2 is not None

        # Both runners hold their own (cheap) provider...
        assert bridge1.quote_provider is not bridge2.quote_provider
        # ...priced off ONE shared generator — exactly one generator exists.
        assert bridge1._generator() is bridge2._generator()
        assert get_chain_bus().generator_count() == 1

        # One runner syncs the market; the other sees the same spot.
        ts = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        bridge1._sync_market("NIFTY", 25_123.45, ts)
        assert bridge2._generator().get_spot("NIFTY") == 25_123.45

    def test_option_runner_takes_exactly_one_chain_subscription(self, manager):
        manager.add_runner(_option_runner_config("NIFTY-A"))
        assert get_chain_bus().subscriber_count("NIFTY") == 1
        manager.add_runner(_option_runner_config("NIFTY-B"))
        assert get_chain_bus().subscriber_count("NIFTY") == 2

    def test_equity_runner_bypasses_the_chain_bus(self, manager):
        manager.add_runner(_equity_runner_config("EQ-1"))
        runner = list(manager._runners.values())[0]
        assert runner.options_bridge is None
        assert get_chain_bus().generator_count() == 0

    def test_bar_feed_subscription_is_refcounted_per_symbol(self, manager):
        manager.add_runner(_equity_runner_config("EQ-1", symbol="RELIANCE"))
        manager.add_runner(_equity_runner_config("EQ-2", symbol="RELIANCE"))
        reg = manager.feed_registry
        assert reg.subscriber_count("synthetic", "RELIANCE", "1day") == 2

    def test_remove_runner_drains_refcounts_to_zero(self, manager):
        id1 = manager.add_runner(_option_runner_config("NIFTY-A"))
        id2 = manager.add_runner(_option_runner_config("NIFTY-B"))
        assert manager.remove_runner(id1) is True
        assert get_chain_bus().subscriber_count("NIFTY") == 1
        assert manager.feed_registry.subscriber_count("synthetic", "NIFTY", "1day") == 1
        manager.remove_runner(id2)
        assert get_chain_bus().subscriber_count("NIFTY") == 0
        assert get_chain_bus().generator_count() == 0
        assert manager.feed_registry.subscriber_count("synthetic", "NIFTY", "1day") == 0
        assert manager.feed_registry.stats() == {"feeds": 0, "subscriptions": 0}

    def test_runner_stop_releases_chain_subscription_and_start_reacquires(self, manager):
        runner = manager.get_runner(manager.add_runner(_option_runner_config("NIFTY-A")))
        assert get_chain_bus().subscriber_count("NIFTY") == 1
        runner.stop()
        assert get_chain_bus().subscriber_count("NIFTY") == 0, "stop() must release"
        runner.start()
        assert get_chain_bus().subscriber_count("NIFTY") == 1, "start() re-acquires"

    def test_shutdown_drains_every_registry_entry(self, manager):
        manager.add_runner(_option_runner_config("NIFTY-A"))
        manager.add_runner(_equity_runner_config("EQ-1"))
        manager.shutdown()
        assert manager.feed_registry.stats() == {"feeds": 0, "subscriptions": 0}
        assert get_chain_bus().generator_count() == 0

    def test_two_option_runners_get_one_feed_and_identical_chain_off_one_bar(
        self, manager
    ):
        """End-to-end over the real fan-out: one bar in, one shared chain out."""
        id1 = manager.add_runner(_option_runner_config("NIFTY-A"))
        id2 = manager.add_runner(_option_runner_config("NIFTY-B"))
        reg = manager.feed_registry
        assert reg.subscriber_count("synthetic", "NIFTY", "1day") == 2

        bridge1 = manager.get_runner(id1).options_bridge
        bridge2 = manager.get_runner(id2).options_bridge

        # Distinct timestamps so neither bar is de-duplicated away.
        base = datetime.now(timezone.utc) + timedelta(seconds=1)
        manager.tick(ts=base)
        manager.tick(ts=base + timedelta(days=1))

        spot1 = bridge1._generator().get_spot("NIFTY")
        spot2 = bridge2._generator().get_spot("NIFTY")
        assert spot1 == spot2
        assert spot1 > 0, "the shared generator never received the fan-out bars"
