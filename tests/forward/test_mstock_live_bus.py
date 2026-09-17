"""U7.1 — mStock live bars into the shared bus (Gap #1).

``MStockBarFeed`` rides in ``forward/feed_registry.py`` next to the synthetic
feed and obeys the §5.2 rate-limit rule: ONE poll thread per manager for ALL
mstock symbols — runner count never multiplies API polls. Bars are pushed
through the same ``on_bar`` / ``on_tick_end`` hooks the ``SyntheticFeed``
uses, so the manager's fan-out and every runner are unchanged (C2), and a
runner cannot tell which feed produced a bar.

Pinned here:

* bar normalisation (ISO strings, epoch millis, garbage rows) into the one
  canonical shape the runner's string-compare dedupe relies on;
* dedupe per symbol (a retried poll can never re-feed a bar);
* error-soft client failures — a data hiccup is logged, never fatal;
* the market-hours gate: one catch-up seed per closed-market symbol, then
  idle (a closed API is never hammered);
* ``on_tick_end`` fires exactly once per delivering sweep;
* manager routing: mstock runners' bars arrive through the shared feed under
  key ``(mstock, NIFTY, 1hour)``, synthetic runners stay on the synthetic
  feed, TWO runners on NIFTY cost ONE API call per sweep, and the poll
  thread's lifecycle tracks the mstock runner population
  (spawn → running, stop_all/shutdown → stopped, synthetic-only → never).

All tests drive ``_poll_once`` directly — no real threads, no sleeps.
"""

from __future__ import annotations

import pytest

from backtest.forward.feed_registry import MStockBarFeed, reset_data_bus
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.risk_supervisor import GlobalRiskConfig


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Duck-typed ``latest_bar(symbol)`` client that records every call."""

    def __init__(self, bars=None, fail_symbols=()):
        self.bars = dict(bars or {})
        self.fail_symbols = set(fail_symbols)
        self.calls: list[str] = []

    def latest_bar(self, symbol: str):
        self.calls.append(symbol)
        if symbol in self.fail_symbols:
            raise RuntimeError("simulated API hiccup")
        return self.bars.get(symbol)


ISO_BAR = {
    "ts": "2026-09-17 09:45:00",
    "open": 25_100.0,
    "high": 25_220.0,
    "low": 25_050.0,
    "close": 25_180.0,
    "volume": 120_000,
}


@pytest.fixture()
def bus():
    reset_data_bus()
    yield
    reset_data_bus()


@pytest.fixture()
def manager():
    """A fresh manager on a fresh bus, with a FAKE mstock client.

    The fake matters: the default client is a real ``MStockLiveFeed``, and a
    started poll thread would attempt a live API call for subscribed symbols.
    With the fake, an accidental thread start is harmless.
    """
    reset_data_bus()
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
        mstock_feed_client=FakeClient({"NIFTY": ISO_BAR}),
    )
    yield mgr
    mgr.shutdown()
    reset_data_bus()


def _mstock_runner(name: str, symbol: str = "NIFTY") -> RunnerConfig:
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=100_000,
        symbols=[symbol],
        timeframe="1hour",
        mode="paper",
        source="mstock",
    )


# ---------------------------------------------------------------------------
# Normalisation — every client shape becomes ONE canonical runner bar
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_iso_string_bar_passes_through(self):
        bar = MStockBarFeed._normalize_bar(ISO_BAR)
        assert bar == ISO_BAR

    def test_epoch_millis_ts_is_absorbed(self):
        raw = {"ts": 1_758_105_900_000, "close": 101.5, "volume": 10}
        bar = MStockBarFeed._normalize_bar(raw)
        assert bar is not None
        assert bar["ts"] == "2025-09-17 10:45:00"  # canonical %Y-%m-%d %H:%M:%S
        assert bar["close"] == 101.5

    def test_missing_fields_default_to_close(self):
        bar = MStockBarFeed._normalize_bar({"ts": "2026-09-17 10:00:00", "close": 55.0})
        assert bar["open"] == bar["high"] == bar["low"] == 55.0
        assert bar["volume"] == 0.0

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "not a dict",
            42,
            {"close": 10.0},  # no ts at all
            {"ts": "garbage-ts", "close": 10.0},
            {"ts": "2026-09-17 10:00:00"},  # no close
        ],
    )
    def test_garbage_rows_return_none(self, raw):
        assert MStockBarFeed._normalize_bar(raw) is None

    def test_timestamp_key_alias_accepted(self):
        bar = MStockBarFeed._normalize_bar({"timestamp": "2026-09-17 11:00:00", "close": 9.0})
        assert bar is not None and bar["ts"] == "2026-09-17 11:00:00"


# ---------------------------------------------------------------------------
# Polling: dedupe, error-soft, market gate, tick-end
# ---------------------------------------------------------------------------


class TestPolling:
    def test_empty_symbols_costs_nothing(self):
        feed = MStockBarFeed(feed_client=FakeClient())
        assert feed._poll_once() == 0

    def test_one_bar_per_symbol_is_delivered(self):
        client = FakeClient({"NIFTY": ISO_BAR, "BANKNIFTY": dict(ISO_BAR, close=52_000.0)})
        feed = MStockBarFeed(feed_client=client)
        feed.on_bar = lambda sym, bar: None
        feed.add_symbols(["NIFTY", "BANKNIFTY"])
        delivered = feed._poll_once()
        assert delivered == 2
        assert client.calls == ["NIFTY", "BANKNIFTY"]

    def test_repeated_poll_is_deduped(self, monkeypatch):
        """A poll returning the same candle twice must never re-feed a bar."""
        monkeypatch.setattr(MStockBarFeed, "_market_open", staticmethod(lambda: True))
        client = FakeClient({"NIFTY": ISO_BAR})
        seen: list[tuple[str, dict]] = []
        feed = MStockBarFeed(feed_client=client)
        feed.on_bar = lambda sym, bar: seen.append((sym, bar))
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 1
        assert feed._poll_once() == 0, "same ts again — dropped"
        assert feed._poll_once() == 0
        assert len(seen) == 1

    def test_newer_bar_after_dedupe_is_delivered(self, monkeypatch):
        monkeypatch.setattr(MStockBarFeed, "_market_open", staticmethod(lambda: True))
        client = FakeClient({"NIFTY": ISO_BAR})
        feed = MStockBarFeed(feed_client=client)
        feed.add_symbols(["NIFTY"])
        feed._poll_once()
        newer = dict(ISO_BAR, ts="2026-09-17 09:46:00", close=25_190.0)
        client.bars["NIFTY"] = newer
        seen: list[dict] = []
        feed.on_bar = lambda sym, bar: seen.append(bar)
        assert feed._poll_once() == 1
        assert seen and seen[0]["close"] == 25_190.0

    def test_client_exception_is_error_soft(self):
        """A data hiccup is logged and skipped — the sweep survives."""
        client = FakeClient(
            {"NIFTY": ISO_BAR, "BANKNIFTY": dict(ISO_BAR, close=52_000.0)},
            fail_symbols=["NIFTY"],
        )
        delivered: list[str] = []
        feed = MStockBarFeed(feed_client=client)
        feed.on_bar = lambda sym, bar: delivered.append(sym)
        feed.add_symbols(["NIFTY", "BANKNIFTY"])
        assert feed._poll_once() == 1  # BANKNIFTY still delivered
        assert delivered == ["BANKNIFTY"]

    def test_client_returning_none_is_skipped(self):
        client = FakeClient({})  # symbol unknown → None
        feed = MStockBarFeed(feed_client=client)
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 0

    def test_market_closed_catchup_then_idle(self, monkeypatch):
        """Closed market: one catch-up seed per symbol, then never again."""
        monkeypatch.setattr(MStockBarFeed, "_market_open", staticmethod(lambda: False))
        client = FakeClient({"NIFTY": ISO_BAR})
        seen: list[tuple[str, dict]] = []
        feed = MStockBarFeed(feed_client=client)
        feed.on_bar = lambda sym, bar: seen.append((sym, bar))
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 1, "first closed-market sweep seeds the latest bar"
        calls_after_seed = len(client.calls)
        assert feed._poll_once() == 0
        assert feed._poll_once() == 0
        assert len(client.calls) == calls_after_seed, "a closed API is never hammered"
        assert len(seen) == 1

    def test_market_open_polls_every_sweep(self, monkeypatch):
        """Market open: every sweep polls — distinct candles are delivered."""
        monkeypatch.setattr(MStockBarFeed, "_market_open", staticmethod(lambda: True))
        client = FakeClient({"NIFTY": ISO_BAR})
        counter = {"n": 0}

        def latest_bar(symbol):
            counter["n"] += 1
            client.calls.append(symbol)
            n = counter["n"]
            return dict(ISO_BAR, ts=f"2026-09-17 09:45:0{n % 10}", close=25_000 + n)

        client.latest_bar = latest_bar
        delivered: list[dict] = []
        feed = MStockBarFeed(feed_client=client)
        feed.on_bar = lambda sym, bar: delivered.append(bar)
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 1
        assert feed._poll_once() == 1, "market open → every sweep polls"
        assert feed._poll_once() == 1
        assert len(delivered) == 3

    def test_on_tick_end_fires_once_per_delivering_sweep(self):
        ticks: list[str] = []
        client = FakeClient({"NIFTY": ISO_BAR, "BANKNIFTY": dict(ISO_BAR, close=52_000.0)})
        feed = MStockBarFeed(feed_client=client)
        feed.on_bar = lambda sym, bar: None
        feed.on_tick_end = lambda ts: ticks.append(ts)
        feed.add_symbols(["NIFTY", "BANKNIFTY"])
        feed._poll_once()
        assert len(ticks) == 1, "one sweep = one tick-end, not one per symbol"
        feed._poll_once()  # fully deduped → nothing delivered
        assert len(ticks) == 1, "an empty sweep fires no tick-end"

    def test_dedupe_state_cleared_on_remove(self):
        client = FakeClient({"NIFTY": ISO_BAR})
        seen: list[tuple[str, dict]] = []
        feed = MStockBarFeed(feed_client=client)
        feed.on_bar = lambda sym, bar: seen.append((sym, bar))
        feed.add_symbols(["NIFTY"])
        feed._poll_once()
        feed.remove_symbols(["NIFTY"])
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 1, "fresh subscription re-seeds (last_ts forgotten)"
        assert len(seen) == 2


# ---------------------------------------------------------------------------
# Manager routing + the rate-limit rule
# ---------------------------------------------------------------------------


class TestManagerRouting:
    def test_mstock_registry_key_and_shared_feed(self, manager):
        manager.add_runner(_mstock_runner("LIVE-1"), start=False)
        reg = manager.feed_registry
        assert reg.subscriber_count("mstock", "NIFTY", "1hour") == 1
        assert reg.subscribe("mstock", "NIFTY", "1hour") is manager.mstock_feed

    def test_mstock_runner_bars_arrive_via_the_shared_feed(self, manager):
        """Bars ride the same fan-out — the runner cannot tell (C2)."""
        client = FakeClient({"NIFTY": ISO_BAR})
        mgr = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            tick_seconds=1.0,
            warmup_bars=5,
            auto_start_feed=False,
            mstock_feed_client=client,
        )
        try:
            instance_id = mgr.add_runner(_mstock_runner("LIVE-1"), start=False)
            runner = mgr.get_runner(instance_id)
            runner.start()
            assert mgr.mstock_feed.on_bar == mgr._on_bar  # bound-method equality
            before = runner.bars_processed
            mgr.mstock_feed._poll_once()
            assert runner.bars_processed == before + 1
        finally:
            mgr.shutdown()

    def test_synthetic_runner_untouched_by_mstock_feed(self, manager):
        from backtest.forward.paper_runner import RunnerConfig

        config = RunnerConfig(
            name="PAPER-1",
            strategy_name="sma_crossover",
            allocated_capital=100_000,
            symbols=["RELIANCE"],
            timeframe="1day",
            mode="paper",
            source="synthetic",
        )
        manager.add_runner(config, start=False)
        assert manager.feed_registry.subscriber_count("synthetic", "RELIANCE", "1day") == 1
        assert manager.mstock_feed._symbols == [], "synthetic-only: no live symbols"
        assert manager.mstock_feed._poll_once() == 0

    def test_two_runners_one_symbol_one_api_call_per_sweep(self, manager):
        """THE rate-limit proof: one sweep = ONE API call regardless of runners."""
        client = FakeClient({"NIFTY": ISO_BAR})
        mgr = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            tick_seconds=1.0,
            warmup_bars=5,
            auto_start_feed=False,
            mstock_feed_client=client,
        )
        try:
            mgr.add_runner(_mstock_runner("LIVE-1"), start=False)
            mgr.add_runner(_mstock_runner("LIVE-2"), start=False)
            assert mgr.mstock_feed._symbols == ["NIFTY"], "one subscription, not two"
            mgr.mstock_feed._poll_once()
            assert client.calls.count("NIFTY") == 1, "one sweep = one API call"
        finally:
            mgr.shutdown()

    def test_poll_thread_lifecycle_tracks_mstock_runners(self, manager):
        # start=True (default): the runner is RUNNING, so the sync gate sees it.
        manager.add_runner(_mstock_runner("LIVE-1"))
        manager._sync_mstock_thread()
        assert manager.mstock_feed.running, "thread runs while an mstock runner is live"

        manager.stop_all()
        manager._sync_mstock_thread()
        assert not manager.mstock_feed.running, "stop_all stops the poll thread"

    def test_synthetic_only_manager_never_starts_the_poll_thread(self, manager):
        from backtest.forward.paper_runner import RunnerConfig

        manager.add_runner(
            RunnerConfig(
                name="PAPER-1",
                strategy_name="sma_crossover",
                allocated_capital=100_000,
                symbols=["RELIANCE"],
                timeframe="1day",
                mode="paper",
                source="synthetic",
            )
        )
        manager._sync_mstock_thread()
        assert not manager.mstock_feed.running, "a synthetic-only manager pays zero API cost"

    def test_remove_last_mstock_runner_stops_the_thread(self, manager):
        instance_id = manager.add_runner(_mstock_runner("LIVE-1"))
        manager._sync_mstock_thread()
        assert manager.mstock_feed.running
        manager.remove_runner(instance_id)
        assert not manager.mstock_feed.running

    def test_shutdown_stops_thread_and_drains_registry(self, manager):
        client = FakeClient({"NIFTY": ISO_BAR})
        mgr = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            tick_seconds=1.0,
            warmup_bars=5,
            auto_start_feed=False,
            mstock_feed_client=client,
        )
        mgr.add_runner(_mstock_runner("LIVE-1"), start=False)
        mgr._sync_mstock_thread()
        mgr.shutdown()
        assert not mgr.mstock_feed.running
        assert mgr.feed_registry.stats() == {"feeds": 0, "subscriptions": 0}


# ---------------------------------------------------------------------------
# Feed defaults + lifecycle hygiene
# ---------------------------------------------------------------------------


class TestFeedDefaults:
    def test_default_client_is_lazy(self):
        """No client → MStockLiveFeed, built lazily (import-time is safe)."""
        feed = MStockBarFeed()
        from backtest.data.mstock_live_feed import MStockLiveFeed

        assert isinstance(feed._client, MStockLiveFeed)

    def test_start_is_idempotent_and_stop_joins(self):
        feed = MStockBarFeed(feed_client=FakeClient(), poll_interval_s=0.05)
        feed.start()
        thread = feed._thread
        feed.start()
        assert feed._thread is thread, "double start must not spawn a second thread"
        assert feed.running
        feed.stop()
        assert not feed.running
        feed.stop()  # idempotent
        assert not feed.running

    def test_add_symbols_dedupes_and_uppercases(self):
        feed = MStockBarFeed(feed_client=FakeClient())
        feed.add_symbols(["nifty", "NIFTY", "BANKNIFTY"])
        assert feed._symbols == ["NIFTY", "BANKNIFTY"]
