"""Tests for Step 10: Market Data Handler (normalization, bar aggregation, mock feed)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backtest.live.data_validator import DataValidator
from backtest.live.market_data_handler import (
    BarBuilder,
    MarketDataHandler,
    MockBrokerFeed,
    MStockBrokerFeed,
)
from backtest.live.time_manager import TimeManager

# ---------------------------------------------------------------------------
# MockBrokerFeed
# ---------------------------------------------------------------------------


def test_mock_broker_feed():
    feed = MockBrokerFeed()
    assert feed.is_connected() is False

    feed.connect()
    assert feed.is_connected() is True

    feed.subscribe(["INFY", "TCS"])
    feed.inject_tick({"symbol": "INFY", "last": 100})

    tick = feed.get_latest_tick("INFY")
    assert tick is not None
    assert tick["last"] == 100

    feed.unsubscribe(["INFY"])
    feed.disconnect()
    assert feed.is_connected() is False


def test_mstock_broker_feed():
    feed = MStockBrokerFeed(mstock_source=None)
    feed.connect()
    assert feed.is_connected() is True
    feed.subscribe(["INFY"])
    feed.disconnect()


# ---------------------------------------------------------------------------
# BarBuilder
# ---------------------------------------------------------------------------


def test_bar_builder_single_tick():
    builder = BarBuilder(symbol="INFY", timeframe="1min")

    tick = {"last": 100, "volume": 10, "timestamp": "2024-01-02T09:15:10+05:30"}
    completed = builder.add_tick(tick)
    assert completed is None  # first tick, no previous bar to close

    current = builder.get_current_bar()
    assert current is not None
    assert current["open"] == 100
    assert current["close"] == 100
    assert current["volume"] == 10


def test_bar_builder_multiple_ticks_same_bar():
    builder = BarBuilder(symbol="INFY", timeframe="1min")

    ticks = [
        {"last": 100, "volume": 10, "timestamp": "2024-01-02T09:15:10+05:30"},
        {"last": 101, "volume": 5, "timestamp": "2024-01-02T09:15:20+05:30"},
        {"last": 99, "volume": 8, "timestamp": "2024-01-02T09:15:30+05:30"},
    ]

    for tick in ticks:
        completed = builder.add_tick(tick)
        assert completed is None  # all same minute

    current = builder.get_current_bar()
    assert current["open"] == 100
    assert current["high"] == 101
    assert current["low"] == 99
    assert current["close"] == 99
    assert current["volume"] == 23


def test_bar_builder_new_bar_boundary():
    builder = BarBuilder(symbol="INFY", timeframe="1min")

    tick1 = {"last": 100, "volume": 10, "timestamp": "2024-01-02T09:15:10+05:30"}
    tick2 = {"last": 101, "volume": 5, "timestamp": "2024-01-02T09:16:10+05:30"}  # next minute

    completed = builder.add_tick(tick1)
    assert completed is None

    completed = builder.add_tick(tick2)
    assert completed is not None  # previous bar closed
    assert completed["open"] == 100
    assert completed["close"] == 100
    assert completed["volume"] == 10

    # New bar started
    current = builder.get_current_bar()
    assert current["open"] == 101


def test_bar_builder_timeframe_5min():
    builder = BarBuilder(symbol="INFY", timeframe="5min")

    # 09:15:10 and 09:17:20 should be same 5min bar (09:15-09:20)
    tick1 = {"last": 100, "timestamp": "2024-01-02T09:15:10+05:30"}
    tick2 = {"last": 101, "timestamp": "2024-01-02T09:17:20+05:30"}
    tick3 = {"last": 102, "timestamp": "2024-01-02T09:20:10+05:30"}  # next 5min bar

    assert builder.add_tick(tick1) is None
    assert builder.add_tick(tick2) is None
    completed = builder.add_tick(tick3)
    assert completed is not None
    assert completed["open"] == 100
    assert completed["close"] == 101


def test_bar_builder_add_bar_direct():
    builder = BarBuilder(symbol="INFY", timeframe="1min")

    bar = {
        "symbol": "INFY",
        "timestamp": "2024-01-02T09:15:00+05:30",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 1000,
    }
    normalized = builder.add_bar(bar)

    assert normalized["symbol"] == "INFY"
    assert normalized["open"] == 100
    assert normalized["close"] == 100.5


# ---------------------------------------------------------------------------
# MarketDataHandler - normalization
# ---------------------------------------------------------------------------


def test_normalize_tick():
    handler = MarketDataHandler(provider="mock")

    # Standard format
    tick = {
        "symbol": "INFY",
        "bid": 99,
        "ask": 101,
        "last": 100,
        "volume": 100,
        "timestamp": "2024-01-02T09:15:00+05:30",
    }
    normalized = handler.normalize_tick(tick)
    assert normalized is not None
    assert normalized["symbol"] == "INFY"
    assert normalized["bid"] == 99
    assert normalized["ask"] == 101
    assert normalized["last"] == 100

    # Missing bid/ask – should estimate
    tick2 = {"symbol": "INFY", "last": 100}
    normalized2 = handler.normalize_tick(tick2)
    assert normalized2 is not None
    assert normalized2["bid"] is not None
    assert normalized2["ask"] is not None

    # Alternative keys: tradingsymbol, ltp
    tick3 = {"tradingsymbol": "RELIANCE", "ltp": 1500, "t": "2024-01-02T09:15:00+05:30"}
    normalized3 = handler.normalize_tick(tick3)
    assert normalized3 is not None
    assert normalized3["symbol"] == "RELIANCE"
    assert normalized3["last"] == 1500

    # Missing symbol – should return None
    tick4 = {"last": 100}
    assert handler.normalize_tick(tick4) is None

    # Missing last – should return None
    tick5 = {"symbol": "INFY", "bid": 99, "ask": 101}
    assert handler.normalize_tick(tick5) is None


def test_normalize_bar():
    handler = MarketDataHandler(provider="mock")

    bar = {
        "symbol": "INFY",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 1000,
        "timestamp": "2024-01-02T09:15:00+05:30",
    }
    normalized = handler.normalize_bar(bar)
    assert normalized is not None
    assert normalized["open"] == 100
    assert normalized["high"] == 101
    assert normalized["low"] == 99
    assert normalized["close"] == 100.5

    # Using short keys o,h,l,c
    bar2 = {"symbol": "INFY", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000}
    normalized2 = handler.normalize_bar(bar2)
    assert normalized2 is not None
    assert normalized2["close"] == 100.5


# ---------------------------------------------------------------------------
# MarketDataHandler - core
# ---------------------------------------------------------------------------


def test_handler_init():
    handler = MarketDataHandler(symbols=["INFY", "TCS"], provider="mock")
    assert "INFY" in handler.symbols
    assert handler.provider == "mock"


def test_handler_connect_disconnect():
    handler = MarketDataHandler(provider="mock")
    handler.connect()
    assert handler.is_connected() is True
    handler.disconnect()
    assert handler.is_connected() is False


def test_handler_subscribe_unsubscribe():
    handler = MarketDataHandler(symbols=["INFY"], provider="mock")
    handler.connect()

    handler.subscribe_symbols(["RELIANCE", "TCS"])
    assert "RELIANCE" in handler.symbols
    assert "TCS" in handler.symbols

    handler.unsubscribe_symbols(["INFY"])
    assert "INFY" not in handler.symbols


def test_handler_tick_flow():
    handler = MarketDataHandler(symbols=["INFY"], provider="mock", buffer_size=10)
    handler.connect()

    ticks_received = []
    handler.on_tick_received(lambda tick: ticks_received.append(tick))

    tick = {
        "symbol": "INFY",
        "bid": 99,
        "ask": 101,
        "last": 100,
        "volume": 10,
        "timestamp": "2024-01-02T09:15:10+05:30",
    }
    handler.inject_tick(tick)

    assert len(ticks_received) == 1
    assert ticks_received[0]["symbol"] == "INFY"

    # Check buffers
    recent = handler.get_recent_ticks("INFY", count=5)
    assert len(recent) == 1

    quote = handler.get_current_quote("INFY")
    assert quote is not None
    assert quote["last"] == 100


def test_handler_bar_aggregation():
    handler = MarketDataHandler(
        symbols=["INFY"], provider="mock", timeframe="1min", timeframes=["1min", "5min"]
    )
    handler.connect()

    bars_closed = []
    handler.on_bar_closed(lambda bar: bars_closed.append(bar))

    # Inject ticks within same minute – no bar closed yet
    handler.inject_tick(
        {"symbol": "INFY", "last": 100, "volume": 10, "timestamp": "2024-01-02T09:15:10+05:30"}
    )
    handler.inject_tick(
        {"symbol": "INFY", "last": 101, "volume": 5, "timestamp": "2024-01-02T09:15:20+05:30"}
    )

    assert len(bars_closed) == 0

    # Next minute – should close previous bar
    handler.inject_tick(
        {"symbol": "INFY", "last": 102, "volume": 8, "timestamp": "2024-01-02T09:16:10+05:30"}
    )

    assert len(bars_closed) == 1
    assert bars_closed[0]["open"] == 100
    assert bars_closed[0]["close"] == 101
    assert bars_closed[0]["volume"] == 15


def test_handler_bar_direct():
    handler = MarketDataHandler(symbols=["INFY"], provider="mock")
    handler.connect()

    bars_closed = []
    handler.on_bar_closed(lambda bar: bars_closed.append(bar))

    bar = {
        "symbol": "INFY",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 1000,
        "timestamp": "2024-01-02T09:15:00+05:30",
    }
    handler.inject_bar(bar)

    assert len(bars_closed) == 1
    assert bars_closed[0]["close"] == 100.5


def test_handler_validation_integration():
    # Validator should reject invalid ticks
    validator = DataValidator(config={"check_bid_ask": True})
    handler = MarketDataHandler(symbols=["INFY"], provider="mock", validator=validator)
    handler.connect()

    ticks_received = []
    handler.on_tick_received(lambda tick: ticks_received.append(tick))

    # Invalid: bid > ask
    handler.inject_tick({"symbol": "INFY", "bid": 102, "ask": 101, "last": 100})

    # Should be rejected, no callback
    assert len(ticks_received) == 0
    assert handler.get_stats()["validation_failures"] == 1


def test_handler_buffer_management():
    handler = MarketDataHandler(symbols=["INFY"], provider="mock", buffer_size=5)
    handler.connect()

    # Inject 10 ticks, buffer should only keep last 5
    for i in range(10):
        handler.inject_tick(
            {
                "symbol": "INFY",
                "last": 100 + i,
                "volume": 1,
                "timestamp": f"2024-01-02T09:15:{i:02d}+05:30",
            }
        )

    recent = handler.get_recent_ticks("INFY", count=10)
    assert len(recent) == 5  # bounded

    # Clear buffers
    handler.clear_buffers("INFY")
    assert len(handler.get_recent_ticks("INFY")) == 0


def test_handler_get_latest_data():
    handler = MarketDataHandler(symbols=["INFY", "TCS"], provider="mock", timeframe="1min")
    handler.connect()

    handler.inject_bar(
        {
            "symbol": "INFY",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
            "timestamp": "2024-01-02T09:15:00+05:30",
        }
    )
    handler.inject_bar(
        {
            "symbol": "TCS",
            "open": 200,
            "high": 201,
            "low": 199,
            "close": 200,
            "volume": 1000,
            "timestamp": "2024-01-02T09:15:00+05:30",
        }
    )

    latest = handler.get_latest_data()
    assert "INFY" in latest
    assert "TCS" in latest


def test_handler_stats():
    handler = MarketDataHandler(symbols=["INFY"], provider="mock")
    handler.connect()

    handler.inject_tick({"symbol": "INFY", "last": 100})
    handler.inject_bar(
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )

    stats = handler.get_stats()
    assert stats["ticks_received"] == 1
    assert stats["bars_built"] == 1
    assert stats["connected"] is True


def test_handler_observer_pattern():
    handler = MarketDataHandler(symbols=["INFY"], provider="mock")
    handler.connect()

    tick_count = []
    bar_count = []

    handler.on_tick_received(lambda tick: tick_count.append(1))
    handler.on_bar_closed(lambda bar: bar_count.append(1))

    handler.inject_tick({"symbol": "INFY", "last": 100})
    handler.inject_bar(
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )

    assert len(tick_count) == 1
    assert len(bar_count) == 1

    # Callback that raises should not break handler
    handler.on_tick_received(lambda tick: 1 / 0)
    handler.inject_tick({"symbol": "INFY", "last": 101})  # Should not crash
    assert len(tick_count) == 2  # First callback still works


def test_handler_with_mstock_provider():
    # Should not crash even without real mStock creds
    handler = MarketDataHandler(symbols=["INFY"], provider="mstock")
    handler.connect()
    assert handler.is_connected() is True

    # Inject mock bar still works
    handler.inject_bar(
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )
    assert handler.get_current_bar("INFY") is not None
