"""Market data fixtures for testing (Step 24).

Provides sample ticks, bars, corrupted data, and generators for
performance benchmarks and load testing.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import numpy as np
import pandas as pd


def generate_random_ticks(
    symbol: str = "INFY", count: int = 100, start_price: float = 100.0
) -> List[Dict[str, Any]]:
    """Generate random ticks for testing bar aggregation."""
    ticks = []
    price = start_price
    base = datetime(2024, 1, 2, 9, 15, tzinfo=timezone.utc)

    for i in range(count):
        # Random walk
        price += random.uniform(-0.5, 0.5)
        price = max(price, 1.0)

        spread = price * 0.001
        bid = price - spread / 2
        ask = price + spread / 2

        tick = {
            "symbol": symbol,
            "timestamp": base + timedelta(seconds=i * 5),
            "bid": bid,
            "ask": ask,
            "last": price,
            "volume": random.randint(1, 100),
        }
        ticks.append(tick)

    return ticks


def generate_ohlcv_bars(
    symbol: str = "INFY", count: int = 100, start_price: float = 100.0, timeframe: str = "1min"
) -> pd.DataFrame:
    """Generate synthetic OHLCV bars."""

    np.random.seed(42)
    dates = pd.date_range(
        "2024-01-01", periods=count, freq="1min" if timeframe == "1min" else "D", tz="UTC"
    )
    close = start_price + np.cumsum(np.random.randn(count) * 0.5)

    df = pd.DataFrame(
        {
            "open": close,
            "high": close + np.random.uniform(0, 1, count),
            "low": close - np.random.uniform(0, 1, count),
            "close": close,
            "volume": np.random.randint(1000, 10000, count),
        },
        index=dates,
    )

    # Ensure high >= max(open, close) and low <= min(open, close)
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"] = df[["open", "close", "low"]].min(axis=1)

    return df


def generate_corrupted_bars() -> List[Dict[str, Any]]:
    """Generate corrupted bars for validator tests."""

    valid_bar = {
        "symbol": "INFY",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "volume": 1000,
        "timestamp": datetime.now(timezone.utc),
    }

    corrupted = [
        # Missing field
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "volume": 1000},
        # Zero price
        {"symbol": "INFY", "open": 0, "high": 101, "low": 99, "close": 100, "volume": 1000},
        # Negative price
        {"symbol": "INFY", "open": -10, "high": 101, "low": 99, "close": 100, "volume": 1000},
        # High < Low
        {"symbol": "INFY", "open": 100, "high": 98, "low": 99, "close": 100, "volume": 1000},
        # High < Close
        {"symbol": "INFY", "open": 100, "high": 99, "low": 98, "close": 100, "volume": 1000},
        # Negative volume
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": -100},
        # Bid > Ask
        {"symbol": "INFY", "bid": 102, "ask": 101, "last": 100},
        # Future timestamp
        {
            "symbol": "INFY",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
            "timestamp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
    ]

    return [valid_bar] + corrupted


def generate_spike_data(
    symbol: str = "INFY", base_price: float = 100.0, spike_price: float = 200.0
) -> List[Dict[str, Any]]:
    """Generate data with price spike for validator tests."""

    base = datetime(2024, 1, 2, 9, 15, tzinfo=timezone.utc)

    # Normal history
    normal = []
    for i in range(20):
        normal.append(
            {
                "symbol": symbol,
                "timestamp": base + timedelta(minutes=i),
                "last": base_price + random.uniform(-1, 1),
                "volume": 1000,
            }
        )

    # Spike
    spike = {
        "symbol": symbol,
        "timestamp": base + timedelta(minutes=21),
        "last": spike_price,
        "volume": 1000,
    }

    return normal + [spike]


# Mock components as per Step 24 spec


class MockBrokerAPI:
    """Simulates broker API responses for testing."""

    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or ["INFY"]
        self._connected = False

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "bid": 100.0,
            "ask": 101.0,
            "last": 100.5,
            "volume": 1000,
            "timestamp": datetime.now(timezone.utc),
        }

    def get_bars(self, symbol: str, timeframe: str = "1min", count: int = 100) -> pd.DataFrame:
        return generate_ohlcv_bars(symbol=symbol, count=count)


class MockMarketDataFeed:
    """Provides test data for MarketDataHandler tests."""

    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or ["INFY"]
        self.ticks = {s: generate_random_ticks(s, count=50) for s in self.symbols}

    def get_next_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        ticks = self.ticks.get(symbol, [])
        if ticks:
            return ticks.pop(0)
        return None


class MockTimeManager:
    """Controllable time for testing (Step 12)."""

    def __init__(self, start_time: datetime = None):
        self.current_time = start_time or datetime(2024, 1, 2, 9, 15, tzinfo=timezone.utc)

    def get_current_time(self):
        return self.current_time

    def advance(self, delta: timedelta):
        self.current_time += delta

    def is_market_open(self, when: datetime = None) -> bool:
        dt = when or self.current_time
        # Simple: 09:15-15:30 IST
        hour = dt.hour
        minute = dt.minute
        total_minutes = hour * 60 + minute
        return 9 * 60 + 15 <= total_minutes <= 15 * 60 + 30


class MockDatabase:
    """In-memory test DB (Step 24)."""

    def __init__(self):
        self.data = defaultdict(list)

    def save(self, table: str, record: Dict[str, Any]):
        self.data[table].append(record)

    def get(self, table: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        records = self.data.get(table, [])
        if not filters:
            return records
        filtered = []
        for rec in records:
            match = all(rec.get(k) == v for k, v in filters.items())
            if match:
                filtered.append(rec)
        return filtered


from collections import defaultdict
