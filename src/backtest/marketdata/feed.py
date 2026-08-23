"""Data feeds: the broker-facing edge of the market data layer (Step 10).

:class:`DataFeed` is the abstract contract; the plan asks for one concrete
broker implementation (this repo trades NSE through mStock — deviation #4 in
the task tracker, the plan says Alpaca) and a mock for testing.

Feeds are **pull-based**: the existing mStock client is REST, so the handler
polls. A future websocket feed can still implement ``poll()`` by draining an
internal queue.

Error contract
--------------
* :class:`FeedError` — this poll failed; the feed may recover. The handler
  responds by reconnecting and retrying.
* :class:`FeedConnectionError` — (re)connection itself failed.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Iterable, Sequence

from backtest.marketdata.errors import FeedConnectionError, FeedError

logger = logging.getLogger("backtest.marketdata.feed")

__all__ = ["DataFeed", "MockFeed", "MStockFeed"]


class DataFeed(ABC):
    """Abstract broker data feed.

    Implementations return **raw broker payloads** from :meth:`poll`; the
    handler owns normalization so every feed benefits from one battle-tested
    parser.
    """

    #: Feed name, recorded as ``source`` on ticks and cached bars.
    name: str = "feed"

    #: Timezone assumed for naive timestamps in this feed's payloads.
    naive_tz: str = "UTC"

    @abstractmethod
    def connect(self) -> None:
        """Establish the connection/session. Raises :class:`FeedConnectionError`."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down the connection. Must be safe to call when not connected."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True when the feed is usable."""

    @abstractmethod
    def poll(self, symbols: Sequence[str]) -> list[dict[str, Any]]:
        """Fetch the latest raw payloads for ``symbols``.

        Raises :class:`FeedError` on transient failure.
        """

    def backfill(
        self, symbol: str, start: str, end: str, timeframe: str = "day"
    ) -> list[dict[str, Any]]:
        """Historical raw bars for warm-up windows. Optional per feed."""
        raise NotImplementedError(f"{self.name} feed does not support backfill")


class MockFeed(DataFeed):
    """Scripted feed for tests and offline replay.

    Batches pushed with :meth:`push` are returned by :meth:`poll` in FIFO
    order, one batch per call. Failures are scripted too, so reconnect
    logic is testable without a network.
    """

    name = "mock"

    def __init__(
        self,
        batches: Iterable[Sequence[dict[str, Any]]] | None = None,
        connect_failures: int = 0,
        naive_tz: str = "UTC",
    ) -> None:
        self._batches: deque[list[dict[str, Any]]] = deque(
            list(batch) for batch in (batches or [])
        )
        self._connected = False
        self._connect_failures = int(connect_failures)
        self._fail_polls = 0
        self.naive_tz = naive_tz
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.poll_calls = 0
        self.polled_symbols: list[tuple[str, ...]] = []

    # -- scripting ----------------------------------------------------------

    def push(self, *payloads: dict[str, Any]) -> None:
        """Queue one batch that a single future :meth:`poll` will return."""
        self._batches.append(list(payloads))

    def fail_next_polls(self, count: int) -> None:
        """Make the next ``count`` polls raise :class:`FeedError`."""
        self._fail_polls = int(count)

    # -- DataFeed -----------------------------------------------------------

    def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_failures > 0:
            self._connect_failures -= 1
            raise FeedConnectionError("mock feed: scripted connect failure")
        self._connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def poll(self, symbols: Sequence[str]) -> list[dict[str, Any]]:
        self.poll_calls += 1
        self.polled_symbols.append(tuple(symbols))
        if not self._connected:
            raise FeedConnectionError("mock feed: poll while disconnected")
        if self._fail_polls > 0:
            self._fail_polls -= 1
            raise FeedError("mock feed: scripted poll failure")
        if not self._batches:
            return []
        return self._batches.popleft()


class MStockFeed(DataFeed):
    """Live NSE data through the existing mStock client (``live/mstock.py``).

    The client is injectable so this class is unit-testable without
    credentials or a network; by default it is built lazily on
    :meth:`connect` (construction triggers session-token lookup).
    """

    name = "mstock"
    naive_tz = "Asia/Kolkata"  # mStock stamps in IST

    #: Local timeframe → mStock historical route interval.
    _INTERVALS = {
        "1min": "minute",
        "3min": "3minute",
        "5min": "5minute",
        "15min": "15minute",
        "30min": "30minute",
        "60min": "60minute",
        "1hour": "60minute",
        "day": "day",
    }

    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._connected = client is not None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise FeedConnectionError("mstock feed is not connected")
        return self._client

    def connect(self) -> None:
        if self._client is None:
            try:
                from backtest.live.mstock import MStockClient

                self._client = MStockClient()
            except FeedError:
                raise
            except Exception as exc:  # auth/env problems surface here
                raise FeedConnectionError(f"mstock connect failed: {exc}") from exc
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    def poll(self, symbols: Sequence[str]) -> list[dict[str, Any]]:
        if not self.is_connected:
            raise FeedConnectionError("mstock feed: poll while disconnected")
        payloads: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                raw = self.client.get_latest(symbol)
            except Exception as exc:  # requests errors, HTTP errors, parsing
                raise FeedError(f"mstock poll failed for {symbol}: {exc}") from exc
            if not isinstance(raw, dict):
                raise FeedError(
                    f"mstock returned {type(raw).__name__} for {symbol}, expected dict"
                )
            payload = dict(raw)
            payload.setdefault("symbol", symbol)
            payloads.append(payload)
        return payloads

    def backfill(
        self, symbol: str, start: str, end: str, timeframe: str = "day"
    ) -> list[dict[str, Any]]:
        """Historical bars via the mStock TypeA historical route."""
        if not self.is_connected:
            raise FeedConnectionError("mstock feed: backfill while disconnected")
        interval = self._INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError(
                f"no mStock interval for timeframe {timeframe!r}; "
                f"supported: {sorted(self._INTERVALS)}"
            )
        try:
            bars = self.client.get_bars(symbol, start, end, interval=interval)
        except Exception as exc:
            raise FeedError(f"mstock backfill failed for {symbol}: {exc}") from exc
        result: list[dict[str, Any]] = []
        for bar in bars:
            if isinstance(bar, dict):
                payload = dict(bar)
            elif isinstance(bar, (list, tuple)) and len(bar) >= 6:
                timestamp, open_v, high_v, low_v, close_v, volume = bar[:6]
                payload = {
                    "timestamp": timestamp,
                    "open": open_v,
                    "high": high_v,
                    "low": low_v,
                    "close": close_v,
                    "volume": volume,
                }
            else:
                raise FeedError(f"unsupported mStock candle shape: {bar!r}")
            payload.setdefault("symbol", symbol)
            result.append(payload)
        return result
