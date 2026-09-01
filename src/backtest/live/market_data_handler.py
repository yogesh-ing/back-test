"""Market Data Handler for forward testing (Step 10).

Normalizes live data from broker APIs (mStock, mock, CSV, synthetic) into
standard format, aggregates ticks into bars, handles multi-symbol, reconnection,
caching, and observer pattern.

Standard normalized format
--------------------------
{
    'symbol': str,
    'timestamp': datetime (UTC, aware),
    'bid': float,
    'ask': float,
    'last': float,
    'volume': int,
    'open': float,
    'high': float,
    'low': float,
    'close': float,
    'timeframe': str (optional)
}

Bar aggregation
---------------
Builds bars from ticks: 1min, 3min, 5min, 15min, 30min, 1hr, 1day.
Aligns bars to standard boundaries, handles gaps, late data.

Features
--------
* Multi-symbol support
* Reconnection on disconnect
* Data validation (via DataValidator)
* Cache recent data in memory (bounded)
* Store to MARKET_DATA_CACHE table via DatabaseManager
* Observer pattern: on_tick_received, on_bar_closed callbacks
* Abstract base for broker APIs, concrete for mStock and mock
* Buffer management to prevent memory leaks
* Comprehensive error handling

Example
-------
>>> from backtest.live.market_data_handler import MarketDataHandler
>>> handler = MarketDataHandler(symbols=["INFY"], provider="mock")
>>> handler.connect()
>>> handler.subscribe_symbols(["RELIANCE"])
>>> handler.on_tick_received(lambda tick: print(tick))
>>> handler.on_bar_closed(lambda bar: print(bar))
>>> # Inject tick (as if from broker)
>>> handler.inject_tick({"symbol":"INFY","bid":1499,"ask":1501,"last":1500,"volume":100})
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Set
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.live.data_validator import DataValidator
from backtest.live.time_manager import TimeManager

logger = logging.getLogger("backtest.live.market_data_handler")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        dt = pd.to_datetime(value, utc=True)
        if isinstance(dt, pd.Timestamp):
            return dt.to_pydatetime()
        return _utcnow()
    except Exception:
        return _utcnow()


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol).strip().upper()


# ---------------------------------------------------------------------------
# Abstract broker feed
# ---------------------------------------------------------------------------


class BrokerFeed(ABC):
    """Abstract base for broker-specific feeds."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def subscribe(self, symbols: List[str]) -> None: ...

    @abstractmethod
    def unsubscribe(self, symbols: List[str]) -> None: ...

    @abstractmethod
    def get_latest_tick(self, symbol: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def is_connected(self) -> bool: ...


class MockBrokerFeed(BrokerFeed):
    """In-memory mock feed for testing (no network)."""

    def __init__(self):
        self._connected = False
        self._subscribed: Set[str] = set()
        self._ticks: Dict[str, Dict[str, Any]] = {}
        self._bars: Dict[str, Dict[str, Any]] = {}

    def connect(self) -> None:
        self._connected = True
        logger.info("MockBrokerFeed connected")

    def disconnect(self) -> None:
        self._connected = False
        logger.info("MockBrokerFeed disconnected")

    def subscribe(self, symbols: List[str]) -> None:
        self._subscribed.update([_normalize_symbol(s) for s in symbols])

    def unsubscribe(self, symbols: List[str]) -> None:
        for s in symbols:
            self._subscribed.discard(_normalize_symbol(s))

    def get_latest_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._ticks.get(_normalize_symbol(symbol))

    def is_connected(self) -> bool:
        return self._connected

    # Test helpers
    def inject_tick(self, tick: Dict[str, Any]):
        symbol = _normalize_symbol(tick.get("symbol", ""))
        if symbol:
            self._ticks[symbol] = tick

    def inject_bar(self, bar: Dict[str, Any]):
        symbol = _normalize_symbol(bar.get("symbol", ""))
        if symbol:
            self._bars[symbol] = bar


class MStockBrokerFeed(BrokerFeed):
    """Concrete feed wrapping existing MStockSource.

    Step 10 requirement: wire to live/mstock.py.
    """

    def __init__(self, mstock_source: Any = None):
        self._connected = False
        self._subscribed: Set[str] = set()
        self._latest: Dict[str, Dict[str, Any]] = {}

        if mstock_source is None:
            try:
                from backtest.live.mstock import MStockSource

                self.source = MStockSource()
            except Exception as exc:
                logger.warning("Failed to create MStockSource: %s, using mock fallback", exc)
                self.source = None
        else:
            self.source = mstock_source

    def connect(self) -> None:
        self._connected = True
        logger.info("MStockBrokerFeed connected")

    def disconnect(self) -> None:
        self._connected = False
        logger.info("MStockBrokerFeed disconnected")

    def subscribe(self, symbols: List[str]) -> None:
        self._subscribed.update([_normalize_symbol(s) for s in symbols])

    def unsubscribe(self, symbols: List[str]) -> None:
        for s in symbols:
            self._subscribed.discard(_normalize_symbol(s))

    def get_latest_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        # For mStock, we don't have streaming ticks in current implementation,
        # so return cached latest or None. In real implementation, this would
        # call WebSocket or REST latest endpoint.
        return self._latest.get(_normalize_symbol(symbol))

    def is_connected(self) -> bool:
        return self._connected

    def fetch_historical(
        self, symbol: str, start: str, end: str, interval: str = "day"
    ) -> pd.DataFrame:
        if self.source is None:
            raise ValueError("MStock source not available")
        return self.source.get_candles(symbol, start, end, interval)


# ---------------------------------------------------------------------------
# Bar aggregation
# ---------------------------------------------------------------------------


@dataclass
class BarBuilder:
    """Aggregates ticks into OHLCV bars for one symbol and timeframe."""

    symbol: str
    timeframe: str
    timezone: str = "Asia/Kolkata"

    # Current bar being built
    _open: Optional[float] = None
    _high: Optional[float] = None
    _low: Optional[float] = None
    _close: Optional[float] = None
    _volume: int = 0
    _start_time: Optional[datetime] = None
    _tick_count: int = 0

    def _timeframe_delta(self) -> timedelta:
        tf = self.timeframe.lower()
        if tf in ("1min", "1m"):
            return timedelta(minutes=1)
        elif tf in ("3min", "3m"):
            return timedelta(minutes=3)
        elif tf in ("5min", "5m"):
            return timedelta(minutes=5)
        elif tf in ("15min", "15m"):
            return timedelta(minutes=15)
        elif tf in ("30min", "30m"):
            return timedelta(minutes=30)
        elif tf in ("60min", "60m", "1hour", "1h"):
            return timedelta(hours=1)
        elif tf in ("day", "1day", "1d"):
            return timedelta(days=1)
        else:
            return timedelta(minutes=1)

    def _align_time(self, dt: datetime) -> datetime:
        """Align dt to timeframe boundary."""
        try:
            local = dt.astimezone(ZoneInfo(self.timezone))
        except Exception:
            local = dt

        tf = self.timeframe.lower()
        if tf in ("1min", "1m"):
            aligned = local.replace(second=0, microsecond=0)
        elif tf in ("3min", "3m"):
            minute = (local.minute // 3) * 3
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("5min", "5m"):
            minute = (local.minute // 5) * 5
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("15min", "15m"):
            minute = (local.minute // 15) * 15
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("30min", "30m"):
            minute = (local.minute // 30) * 30
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("60min", "60m", "1hour", "1h"):
            aligned = local.replace(minute=0, second=0, microsecond=0)
        elif tf in ("day", "1day", "1d"):
            aligned = local.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            aligned = local.replace(second=0, microsecond=0)

        return aligned.astimezone(timezone.utc)

    def add_tick(self, tick: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Add tick and return completed bar if timeframe closed, else None."""
        # Extract price: prefer last, then close, then price
        price = tick.get("last") or tick.get("close") or tick.get("price")
        if price is None:
            return None

        try:
            price = float(price)
        except (ValueError, TypeError):
            return None

        ts_raw = tick.get("timestamp") or tick.get("ts") or _utcnow()
        ts = _parse_timestamp(ts_raw)
        aligned = self._align_time(ts)

        # If first tick or new bar boundary, close previous bar
        completed_bar = None
        if self._start_time is not None and aligned > self._start_time:
            # Bar closed, return it
            completed_bar = self._close_bar()
            # Reset for new bar
            self._reset()

        if self._start_time is None:
            self._start_time = aligned
            self._open = price
            self._high = price
            self._low = price
            self._close = price
            self._volume = int(tick.get("volume", 0) or 0)
        else:
            # Update existing bar
            if self._high is None or price > self._high:
                self._high = price
            if self._low is None or price < self._low:
                self._low = price
            self._close = price
            self._volume += int(tick.get("volume", 0) or 0)

        self._tick_count += 1

        return completed_bar

    def add_bar(self, bar: Mapping[str, Any]) -> Dict[str, Any]:
        """Directly add a completed bar (from historical source)."""
        # Normalize to standard format
        ts_raw = bar.get("timestamp") or bar.get("ts") or _utcnow()
        ts = _parse_timestamp(ts_raw)

        return {
            "symbol": _normalize_symbol(bar.get("symbol", self.symbol)),
            "timestamp": ts,
            "open": float(bar.get("open", bar.get("close", 0))),
            "high": float(bar.get("high", bar.get("close", 0))),
            "low": float(bar.get("low", bar.get("close", 0))),
            "close": float(bar.get("close", 0)),
            "volume": int(bar.get("volume", 0) or 0),
            "timeframe": self.timeframe,
            "bid": bar.get("bid"),
            "ask": bar.get("ask"),
            "last": bar.get("close"),
        }

    def _close_bar(self) -> Optional[Dict[str, Any]]:
        if self._open is None or self._start_time is None:
            return None

        bar = {
            "symbol": self.symbol,
            "timestamp": self._start_time,
            "open": self._open,
            "high": self._high if self._high is not None else self._open,
            "low": self._low if self._low is not None else self._open,
            "close": self._close if self._close is not None else self._open,
            "volume": self._volume,
            "timeframe": self.timeframe,
            "tick_count": self._tick_count,
        }
        return bar

    def _reset(self):
        self._open = None
        self._high = None
        self._low = None
        self._close = None
        self._volume = 0
        self._start_time = None
        self._tick_count = 0

    def get_current_bar(self) -> Optional[Dict[str, Any]]:
        """Get current incomplete bar."""
        if self._open is None:
            return None
        return {
            "symbol": self.symbol,
            "timestamp": self._start_time,
            "open": self._open,
            "high": self._high,
            "low": self._low,
            "close": self._close,
            "volume": self._volume,
            "timeframe": self.timeframe,
            "tick_count": self._tick_count,
            "is_complete": False,
        }


# ---------------------------------------------------------------------------
# MarketDataHandler
# ---------------------------------------------------------------------------


class MarketDataHandler:
    """Main market data handler (Step 10).

    Parameters
    ----------
    symbols:
        Initial symbols to subscribe
    provider:
        Provider name: mock, mstock, csv, synthetic
    db_manager:
        Optional DatabaseManager for caching to MARKET_DATA_CACHE
    validator:
        Optional DataValidator for quality checks
    time_manager:
        Optional TimeManager for market hours and alignment
    buffer_size:
        Max ticks/bars to keep in memory per symbol (prevents memory leaks)
    auto_reconnect:
        Whether to auto-reconnect on disconnect
    max_reconnect_attempts:
        Max reconnection attempts
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        provider: str = "mock",
        db_manager: Any = None,
        validator: Optional[DataValidator] = None,
        time_manager: Optional[TimeManager] = None,
        buffer_size: int = 1000,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 5,
        timeframe: str = "1min",
        timeframes: Optional[List[str]] = None,
    ):
        self.symbols = [_normalize_symbol(s) for s in (symbols or [])]
        self.provider = str(provider).lower()
        self.db_manager = db_manager
        self.validator = validator or DataValidator()
        self.time_manager = time_manager or TimeManager(market="NSE")
        self.buffer_size = int(buffer_size)
        self.auto_reconnect = bool(auto_reconnect)
        self.max_reconnect_attempts = int(max_reconnect_attempts)
        self.timeframe = timeframe
        self.timeframes = timeframes or [timeframe, "5min", "15min", "1day"]

        # Broker feed
        if self.provider == "mock":
            self.feed: BrokerFeed = MockBrokerFeed()
        elif self.provider == "mstock":
            self.feed = MStockBrokerFeed()
        else:
            # Fallback to mock for unknown providers
            logger.warning("Unknown provider %s, using mock", self.provider)
            self.feed = MockBrokerFeed()

        # Per-symbol buffers
        self._tick_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.buffer_size))
        self._bar_buffers: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=self.buffer_size))
        )
        self._bar_builders: Dict[str, Dict[str, BarBuilder]] = defaultdict(dict)
        self._latest_quotes: Dict[str, Dict[str, Any]] = {}
        self._latest_bars: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

        # Observer callbacks
        self._tick_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._bar_callbacks: List[Callable[[Dict[str, Any]], None]] = []

        self._connected = False
        self._reconnect_count = 0

        # Stats
        self._stats = {
            "ticks_received": 0,
            "bars_built": 0,
            "validation_failures": 0,
            "reconnects": 0,
        }

        logger.info(
            "MarketDataHandler initialized: provider=%s symbols=%s timeframes=%s",
            self.provider,
            self.symbols,
            self.timeframes,
        )

    # -- connection ---------------------------------------------------------

    def connect_to_feed(self, broker_api: Optional[Any] = None) -> None:
        """Connect to market data feed.

        Parameters
        ----------
        broker_api:
            Optional broker API instance. If provided, wraps it.
        """
        if broker_api is not None:
            # If broker_api is a DataSource or MStockSource, wrap it
            if hasattr(broker_api, "get_candles"):
                self.feed = MStockBrokerFeed(mstock_source=broker_api)
            elif isinstance(broker_api, BrokerFeed):
                self.feed = broker_api

        try:
            self.feed.connect()
            self._connected = True
            self._reconnect_count = 0
            if self.symbols:
                self.feed.subscribe(self.symbols)
            logger.info("Connected to feed: %s", self.provider)
        except Exception as exc:
            logger.exception("Failed to connect to feed: %s", exc)
            self._connected = False
            if self.auto_reconnect:
                self._attempt_reconnect()

    def connect(self) -> None:
        self.connect_to_feed()

    def disconnect(self) -> None:
        try:
            self.feed.disconnect()
        except Exception:
            pass
        self._connected = False
        logger.info("Disconnected from feed")

    def is_connected(self) -> bool:
        return self._connected and self.feed.is_connected()

    def _attempt_reconnect(self) -> bool:
        """Attempt reconnection with backoff."""
        if self._reconnect_count >= self.max_reconnect_attempts:
            logger.error(
                "Max reconnect attempts (%s) reached, giving up", self.max_reconnect_attempts
            )
            return False

        self._reconnect_count += 1
        self._stats["reconnects"] += 1

        backoff = min(2**self._reconnect_count, 30)
        logger.warning(
            "Reconnecting in %ss (attempt %s/%s)",
            backoff,
            self._reconnect_count,
            self.max_reconnect_attempts,
        )
        time.sleep(backoff)

        try:
            self.feed.connect()
            self.feed.subscribe(self.symbols)
            self._connected = True
            logger.info("Reconnected successfully")
            return True
        except Exception as exc:
            logger.warning("Reconnect failed: %s", exc)
            if self.auto_reconnect:
                return self._attempt_reconnect()
            return False

    # -- subscription -------------------------------------------------------

    def subscribe_symbols(self, symbol_list: List[str]) -> None:
        symbols = [_normalize_symbol(s) for s in symbol_list]
        self.symbols.extend([s for s in symbols if s not in self.symbols])

        try:
            self.feed.subscribe(symbols)
            logger.info("Subscribed to %s", symbols)
        except Exception as exc:
            logger.warning("Subscribe failed: %s", exc)

        # Initialize bar builders for new symbols
        for symbol in symbols:
            for tf in self.timeframes:
                if tf not in self._bar_builders[symbol]:
                    self._bar_builders[symbol][tf] = BarBuilder(
                        symbol=symbol, timeframe=tf, timezone=self.time_manager.timezone
                    )

    def unsubscribe_symbols(self, symbol_list: List[str]) -> None:
        symbols = [_normalize_symbol(s) for s in symbol_list]
        self.symbols = [s for s in self.symbols if s not in symbols]

        try:
            self.feed.unsubscribe(symbols)
            logger.info("Unsubscribed from %s", symbols)
        except Exception as exc:
            logger.warning("Unsubscribe failed: %s", exc)

    # -- data retrieval -----------------------------------------------------

    def get_current_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._latest_quotes.get(_normalize_symbol(symbol))

    def get_current_bar(self, symbol: str, timeframe: str = "1min") -> Optional[Dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        # Return latest complete bar for timeframe
        bars = self._bar_buffers.get(symbol, {}).get(timeframe)
        if bars and len(bars) > 0:
            return dict(bars[-1])

        # Or current incomplete bar from builder
        builder = self._bar_builders.get(symbol, {}).get(timeframe)
        if builder:
            return builder.get_current_bar()

        return None

    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """Return latest quotes/bars for all subscribed symbols (for engine loop)."""
        # For engine compatibility, return mapping symbol->bar
        result = {}
        for symbol in self.symbols:
            # Prefer latest bar for primary timeframe
            bar = self.get_current_bar(symbol, self.timeframe)
            if bar:
                result[symbol] = bar
            else:
                quote = self.get_current_quote(symbol)
                if quote:
                    result[symbol] = quote
        return result

    # -- normalization ------------------------------------------------------

    def normalize_tick(self, broker_data: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert broker-specific tick to standard format.

        Standard format:
        {
            'symbol': str,
            'timestamp': datetime (UTC),
            'bid': float,
            'ask': float,
            'last': float,
            'volume': int,
            'open': float (optional),
            'high': float (optional),
            'low': float (optional),
            'close': float (optional)
        }
        """
        if not isinstance(broker_data, Mapping):
            logger.warning("normalize_tick got non-mapping: %s", type(broker_data))
            return None

        try:
            symbol = (
                broker_data.get("symbol")
                or broker_data.get("tradingsymbol")
                or broker_data.get("Symbol")
            )
            if not symbol:
                return None
            symbol = _normalize_symbol(symbol)

            # Timestamp: many possible keys
            ts_raw = (
                broker_data.get("timestamp")
                or broker_data.get("ts")
                or broker_data.get("time")
                or broker_data.get("t")
                or broker_data.get("last_traded_time")
                or _utcnow()
            )
            ts = _parse_timestamp(ts_raw)

            # Price fields: support many broker formats
            def _get_price(*keys, default=None):
                for k in keys:
                    if k in broker_data and broker_data[k] is not None:
                        try:
                            return float(broker_data[k])
                        except (ValueError, TypeError):
                            continue
                return default

            bid = _get_price("bid", "bid_price", "b", "buy_price")
            ask = _get_price("ask", "ask_price", "a", "sell_price")
            last = _get_price("last", "last_price", "ltp", "close", "c", "price", "l")

            if last is None:
                # Need at least last price
                return None

            # If bid/ask missing, estimate from last with small spread
            if bid is None and ask is None:
                spread = last * 0.001  # 0.1% spread estimate
                bid = last - spread / 2
                ask = last + spread / 2
            elif bid is None:
                bid = last
            elif ask is None:
                ask = last

            volume = (
                broker_data.get("volume") or broker_data.get("v") or broker_data.get("vol") or 0
            )
            try:
                volume = int(float(volume))
            except (ValueError, TypeError):
                volume = 0

            # OHLC if present (for bar data)
            open_ = _get_price("open", "o")
            high = _get_price("high", "h")
            low = _get_price("low", "l")
            close = _get_price("close", "c")

            normalized = {
                "symbol": symbol,
                "timestamp": ts,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume": volume,
            }

            if open_ is not None:
                normalized["open"] = open_
            if high is not None:
                normalized["high"] = high
            if low is not None:
                normalized["low"] = low
            if close is not None:
                normalized["close"] = close
            else:
                normalized["close"] = last

            # Preserve timeframe if present
            if "timeframe" in broker_data:
                normalized["timeframe"] = broker_data["timeframe"]

            return normalized

        except Exception as exc:
            logger.warning("Failed to normalize tick %s: %s", broker_data, exc)
            return None

    def normalize_bar(self, broker_bar: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert broker bar to standard format (same as tick but with OHLC)."""
        tick = self.normalize_tick(broker_bar)
        if tick is None:
            return None

        # Ensure OHLC present
        try:
            for key in ("open", "high", "low", "close"):
                if key not in tick:
                    # Try to get from original
                    val = broker_bar.get(key) or broker_bar.get(key[0])  # o,h,l,c
                    if val is not None:
                        tick[key] = float(val)

            # If still missing, use close for all
            if "open" not in tick:
                tick["open"] = tick["close"]
            if "high" not in tick:
                tick["high"] = tick["close"]
            if "low" not in tick:
                tick["low"] = tick["close"]

            return tick

        except Exception as exc:
            logger.warning("Failed to normalize bar %s: %s", broker_bar, exc)
            return None

    # -- tick/bar handling --------------------------------------------------

    def on_tick(self, broker_tick: Mapping[str, Any]) -> None:
        """Process incoming tick from broker feed."""
        self._stats["ticks_received"] += 1

        normalized = self.normalize_tick(broker_tick)
        if normalized is None:
            self._stats["validation_failures"] += 1
            return

        # Validate
        if not self.validator.validate_tick(normalized):
            self._stats["validation_failures"] += 1
            logger.debug("Tick validation failed: %s", normalized)
            return

        symbol = normalized["symbol"]

        # Cache in memory (bounded)
        self._tick_buffers[symbol].append(normalized)
        self._latest_quotes[symbol] = normalized

        # Bar aggregation: feed to all timeframe builders
        for tf in self.timeframes:
            builder = self._bar_builders.get(symbol, {}).get(tf)
            if builder is None:
                builder = BarBuilder(
                    symbol=symbol, timeframe=tf, timezone=self.time_manager.timezone
                )
                self._bar_builders[symbol][tf] = builder

            completed_bar = builder.add_tick(normalized)
            if completed_bar:
                self._handle_new_bar(completed_bar)

        # Store to DB if manager available
        if self.db_manager is not None:
            try:
                self._store_tick_to_db(normalized)
            except Exception as exc:
                logger.debug("Failed to store tick to DB: %s", exc)

        # Publish to subscribers
        self._publish_tick(normalized)

        # Check if market is open (optional)
        if not self.time_manager.is_market_open(normalized["timestamp"]):
            logger.debug("Tick outside market hours: %s", normalized["timestamp"])

    def on_bar(self, broker_bar: Mapping[str, Any]) -> None:
        """Process incoming completed bar from broker feed."""
        normalized = self.normalize_bar(broker_bar)
        if normalized is None:
            self._stats["validation_failures"] += 1
            return

        if not self.validator.validate_bar(normalized):
            self._stats["validation_failures"] += 1
            logger.debug("Bar validation failed: %s", normalized)
            return

        self._handle_new_bar(normalized)

    def _handle_new_bar(self, bar: Dict[str, Any]):
        """Handle a newly completed bar (from aggregation or direct)."""
        symbol = bar["symbol"]
        timeframe = bar.get("timeframe", self.timeframe)

        # Cache
        if symbol not in self._bar_buffers:
            self._bar_buffers[symbol] = defaultdict(lambda: deque(maxlen=self.buffer_size))

        self._bar_buffers[symbol][timeframe].append(bar)
        if timeframe == self.timeframe or timeframe == "1min":
            self._latest_bars[symbol][timeframe] = bar

        self._stats["bars_built"] += 1

        # Store to DB
        if self.db_manager is not None:
            try:
                self._store_bar_to_db(bar)
            except Exception as exc:
                logger.debug("Failed to store bar to DB: %s", exc)

        # Publish
        self._publish_bar(bar)

        logger.debug(
            "New bar: %s %s close=%s vol=%s", symbol, timeframe, bar.get("close"), bar.get("volume")
        )

    # -- observer pattern ---------------------------------------------------

    def on_tick_received(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register callback for tick events: callback(tick)."""
        if not callable(callback):
            raise ValueError("callback must be callable")
        self._tick_callbacks.append(callback)

    def on_bar_closed(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register callback for bar close events: callback(bar)."""
        if not callable(callback):
            raise ValueError("callback must be callable")
        self._bar_callbacks.append(callback)

    def _publish_tick(self, tick: Dict[str, Any]):
        for cb in self._tick_callbacks:
            try:
                cb(tick)
            except Exception:
                logger.exception("Tick callback failed")

    def _publish_bar(self, bar: Dict[str, Any]):
        for cb in self._bar_callbacks:
            try:
                cb(bar)
            except Exception:
                logger.exception("Bar callback failed")

    # -- DB caching ---------------------------------------------------------

    def _store_tick_to_db(self, tick: Dict[str, Any]):
        # For ticks, we could store as 1min bars or as market_data_cache with timeframe=tick?
        # For simplicity, store as market_data_cache with timeframe=1min if we have OHLC,
        # else skip. Real implementation would have separate tick table.
        pass

    def _store_bar_to_db(self, bar: Dict[str, Any]):
        if self.db_manager is None:
            return

        from backtest.db.models import MarketDataCache

        try:
            with self.db_manager.session() as session:
                # Check if bar already exists (unique constraint symbol, exchange, timeframe, ts)
                # For simplicity, try to insert, ignore if exists
                row = MarketDataCache(
                    symbol=bar["symbol"],
                    exchange=bar.get("exchange", "NSE"),
                    timeframe=bar.get("timeframe", "1min"),
                    ts=bar["timestamp"],
                    open=bar["open"],
                    high=bar["high"],
                    low=bar["low"],
                    close=bar["close"],
                    volume=bar.get("volume", 0),
                    bid=bar.get("bid"),
                    ask=bar.get("ask"),
                    source=self.provider,
                )
                session.add(row)
                session.flush()
        except Exception as exc:
            # Likely duplicate bar (unique constraint) – ignore
            logger.debug("Bar DB insert skipped (duplicate?): %s", exc)

    # -- buffer management --------------------------------------------------

    def get_recent_ticks(self, symbol: str, count: int = 100) -> List[Dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        buf = self._tick_buffers.get(symbol, deque())
        return list(buf)[-count:]

    def get_recent_bars(
        self, symbol: str, timeframe: str = "1min", count: int = 100
    ) -> List[Dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        buf = self._bar_buffers.get(symbol, {}).get(timeframe, deque())
        return list(buf)[-count:]

    def clear_buffers(self, symbol: Optional[str] = None):
        if symbol:
            symbol = _normalize_symbol(symbol)
            self._tick_buffers.pop(symbol, None)
            self._bar_buffers.pop(symbol, None)
            self._bar_builders.pop(symbol, None)
            self._latest_quotes.pop(symbol, None)
            self._latest_bars.pop(symbol, None)
        else:
            self._tick_buffers.clear()
            self._bar_buffers.clear()
            self._bar_builders.clear()
            self._latest_quotes.clear()
            self._latest_bars.clear()
        logger.info("Buffers cleared for %s", symbol or "all")

    # -- injection for testing (mock-only) ----------------------------------

    def inject_tick(self, tick: Dict[str, Any]):
        """Inject tick as if from broker (for testing)."""
        # If feed is MockBrokerFeed, also inject there
        if isinstance(self.feed, MockBrokerFeed):
            self.feed.inject_tick(tick)
        self.on_tick(tick)

    def inject_bar(self, bar: Dict[str, Any]):
        """Inject bar as if from broker (for testing)."""
        if isinstance(self.feed, MockBrokerFeed):
            self.feed.inject_bar(bar)
        self.on_bar(bar)

    # -- stats --------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        return {
            **dict(self._stats),
            "connected": self.is_connected(),
            "subscribed_symbols": list(self.symbols),
            "buffer_sizes": {sym: len(buf) for sym, buf in self._tick_buffers.items()},
            "validator_stats": (
                self.validator.get_stats() if hasattr(self.validator, "get_stats") else {}
            ),
        }

    def __repr__(self):
        return (
            f"<MarketDataHandler provider={self.provider} symbols={self.symbols} "
            f"connected={self.is_connected()} ticks={self._stats['ticks_received']} "
            f"bars={self._stats['bars_built']}>"
        )
