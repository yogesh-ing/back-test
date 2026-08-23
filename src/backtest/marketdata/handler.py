"""MarketDataHandler — the live data hub (Step 10).

Wires a :class:`~backtest.marketdata.feed.DataFeed` to everything downstream:

* normalizes raw broker payloads into standard :class:`Tick` objects,
* aggregates ticks into aligned bars per configured timeframe,
* keeps bounded in-memory buffers of recent ticks and bars,
* notifies observers (``on_tick_received`` / ``on_bar_closed``),
* reconnects with exponential backoff when the feed fails,
* persists closed bars to the ``market_data_cache`` table, idempotently.

Polling, not looping
--------------------
The handler deliberately owns **no event loop** — :meth:`poll_once` does one
round trip, and the Step 20 orchestrator decides cadence, threading and
lifecycle. This keeps the handler synchronous and fully unit-testable.

Layering
--------
Pure in-memory except two edges: the injected feed, and
:meth:`persist_closed_bars`, which talks to the database only through
:class:`backtest.db.DatabaseManager` (same rule as ``simulator/``).
"""

from __future__ import annotations

import logging
import time as time_module
from collections import deque
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from backtest.marketdata.bars import BarAggregator, Timeframe, _coerce_timeframe
from backtest.marketdata.errors import (
    FeedConnectionError,
    FeedError,
    MarketDataError,
    NormalizationError,
)
from backtest.marketdata.feed import DataFeed
from backtest.marketdata.ticks import Bar, Tick, normalize_tick

if TYPE_CHECKING:  # pragma: no cover
    from backtest.db.manager import DatabaseManager

logger = logging.getLogger("backtest.marketdata.handler")

__all__ = [
    "MarketDataConfig",
    "MarketDataHandler",
    "load_marketdata_config",
    "DEFAULT_MARKETDATA_CONFIG_PATH",
]

DEFAULT_MARKETDATA_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "marketdata.yaml"
)


def _parse_anchor(value: Any) -> dtime | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, dtime):
        return value
    try:
        hour, minute = str(value).strip().split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"session_anchor must look like '09:15', got {value!r}") from exc


@dataclass(frozen=True)
class MarketDataConfig:
    """Handler settings. Defaults suit NSE via mStock."""

    #: Timeframes to aggregate for every subscribed symbol.
    timeframes: tuple[str, ...] = ("1min", "5min", "15min", "60min", "day")
    #: Exchange timezone for bar boundary alignment.
    timezone: str = "Asia/Kolkata"
    #: Intraday session anchor — NSE hourly candles run 09:15–10:15.
    session_anchor: dtime | None = dtime(9, 15)
    #: Exchange code written to the market data cache.
    exchange: str = "NSE"
    #: Bounded buffer sizes (buffer management — prevents memory leaks).
    tick_buffer_size: int = 1000
    bar_buffer_size: int = 500
    #: Late/gap policy (see BarAggregator).
    late_grace_seconds: int = 60
    fill_gaps: bool = False
    max_gap_bars: int = 16
    #: Reconnection policy.
    max_reconnect_attempts: int = 5
    reconnect_backoff_seconds: float = 1.0
    reconnect_backoff_cap_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.timeframes:
            raise ValueError("timeframes must not be empty")
        for tf in self.timeframes:
            _coerce_timeframe(tf)
        ZoneInfo(self.timezone)  # fail fast on a typo'd zone
        if self.tick_buffer_size < 1:
            raise ValueError("tick_buffer_size must be >= 1")
        if self.bar_buffer_size < 1:
            raise ValueError("bar_buffer_size must be >= 1")
        if self.late_grace_seconds < 0:
            raise ValueError("late_grace_seconds must be >= 0")
        if self.max_gap_bars < 0:
            raise ValueError("max_gap_bars must be >= 0")
        if self.max_reconnect_attempts < 1:
            raise ValueError("max_reconnect_attempts must be >= 1")
        if self.reconnect_backoff_seconds < 0:
            raise ValueError("reconnect_backoff_seconds must be >= 0")
        if self.reconnect_backoff_cap_seconds < self.reconnect_backoff_seconds:
            raise ValueError("reconnect_backoff_cap_seconds must be >= reconnect_backoff_seconds")


def load_marketdata_config(
    path: str | Path | None = None,
    profile: str | None = None,
) -> MarketDataConfig:
    """Load :class:`MarketDataConfig` from YAML (``config/marketdata.yaml``).

    Same layout as the other simulator configs: a ``default`` section plus
    named ``profiles`` that override individual keys; ``active_profile``
    picks one when ``profile`` is not given.
    """
    import yaml

    config_path = Path(path) if path is not None else DEFAULT_MARKETDATA_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh) or {}

    settings: dict[str, Any] = dict(document.get("default") or {})
    profiles: Mapping[str, Any] = document.get("profiles") or {}
    chosen = profile or document.get("active_profile")
    if chosen:
        if chosen not in profiles:
            raise ValueError(
                f"unknown marketdata profile {chosen!r}; available: {sorted(profiles)}"
            )
        settings.update(profiles[chosen] or {})

    if "timeframes" in settings:
        settings["timeframes"] = tuple(str(tf) for tf in settings["timeframes"])
    if "session_anchor" in settings:
        settings["session_anchor"] = _parse_anchor(settings["session_anchor"])

    valid = {f.name for f in MarketDataConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(settings) - valid
    if unknown:
        raise ValueError(f"unknown marketdata config keys: {sorted(unknown)}")
    return MarketDataConfig(**settings)


class MarketDataHandler:
    """Normalizes, aggregates, buffers and publishes live market data."""

    def __init__(
        self,
        config: MarketDataConfig | None = None,
        feed: DataFeed | None = None,
    ) -> None:
        self.config = config or MarketDataConfig()
        self.feed: DataFeed | None = None
        self._subscribed: set[str] = set()
        self._quotes: dict[str, Tick] = {}
        self._ticks: dict[str, deque[Tick]] = {}
        self._bars: dict[tuple[str, str], deque[Bar]] = {}
        self._pending_db: list[Bar] = []
        self._tick_callbacks: list[Callable[[Tick], None]] = []
        self._aggregator = BarAggregator(
            timeframes=self.config.timeframes,
            tz=self.config.timezone,
            anchor=self.config.session_anchor,
            fill_gaps=self.config.fill_gaps,
            max_gap_bars=self.config.max_gap_bars,
            late_grace_seconds=self.config.late_grace_seconds,
        )
        self._aggregator.on_bar_closed(self._on_aggregator_bar)
        #: Injectable for tests — reconnect backoff must not really sleep there.
        self._sleep: Callable[[float], None] = time_module.sleep
        self._stats: dict[str, int] = {
            "ticks_received": 0,
            "invalid_payloads": 0,
            "ignored_unsubscribed": 0,
            "poll_errors": 0,
            "reconnects": 0,
        }
        if feed is not None:
            self.connect_to_feed(feed)

    # ------------------------------------------------------------------
    # Feed lifecycle
    # ------------------------------------------------------------------

    def connect_to_feed(self, feed: DataFeed) -> None:
        """Attach ``feed`` and connect it (with the retry policy)."""
        if not isinstance(feed, DataFeed):
            raise TypeError(f"feed must be a DataFeed, got {type(feed).__name__}")
        self.feed = feed
        if not feed.is_connected:
            self._reconnect(first_connect=True)

    def disconnect(self) -> None:
        if self.feed is not None:
            try:
                self.feed.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.exception("feed disconnect failed")

    def _reconnect(self, first_connect: bool = False) -> None:
        """Try to (re)connect the feed, backing off exponentially."""
        assert self.feed is not None
        attempts = self.config.max_reconnect_attempts
        delay = self.config.reconnect_backoff_seconds
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.feed.connect()
            except FeedError as exc:
                last_error = exc
                logger.warning(
                    "feed connect attempt %d/%d failed: %s", attempt, attempts, exc
                )
                if attempt < attempts and delay > 0:
                    self._sleep(min(delay, self.config.reconnect_backoff_cap_seconds))
                    delay = min(delay * 2, self.config.reconnect_backoff_cap_seconds)
                continue
            if not first_connect:
                self._stats["reconnects"] += 1
            return
        raise FeedConnectionError(
            f"could not connect feed {self.feed.name!r} after {attempts} attempt(s): {last_error}"
        )

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    @property
    def subscribed_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._subscribed))

    def subscribe_symbols(self, symbols: Iterable[str] | str) -> None:
        for symbol in self._as_symbols(symbols):
            self._subscribed.add(symbol)

    def unsubscribe_symbols(self, symbols: Iterable[str] | str) -> None:
        """Stop updating a symbol. Buffered history is kept for inspection."""
        for symbol in self._as_symbols(symbols):
            self._subscribed.discard(symbol)

    @staticmethod
    def _as_symbols(symbols: Iterable[str] | str) -> list[str]:
        if isinstance(symbols, str):
            symbols = [symbols]
        cleaned = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not cleaned:
            raise ValueError("no symbols given")
        return cleaned

    # ------------------------------------------------------------------
    # Observers
    # ------------------------------------------------------------------

    def on_tick_received(self, callback: Callable[[Tick], None]) -> Callable[[Tick], None]:
        """Register a per-tick observer. Returns the callback for removal."""
        if not callable(callback):
            raise ValueError("callback must be callable")
        self._tick_callbacks.append(callback)
        return callback

    def remove_tick_callback(self, callback: Callable[[Tick], None]) -> None:
        try:
            self._tick_callbacks.remove(callback)
        except ValueError:
            pass

    def on_bar_closed(self, callback: Callable[[Bar], None]) -> Callable[[Bar], None]:
        """Register a per-closed-bar observer. Returns the callback for removal."""
        return self._aggregator.on_bar_closed(callback)

    def remove_bar_callback(self, callback: Callable[[Bar], None]) -> None:
        self._aggregator.remove_bar_callback(callback)

    # ------------------------------------------------------------------
    # Polling and ingestion
    # ------------------------------------------------------------------

    def poll_once(self) -> list[Tick]:
        """One poll round trip: fetch, normalize, buffer, aggregate, publish.

        On a transient :class:`FeedError` the handler reconnects (with
        backoff) and retries the poll once. Returns the ticks accepted this
        round; invalid payloads are counted, logged and skipped without
        breaking the batch.
        """
        if self.feed is None:
            raise MarketDataError("no feed attached; call connect_to_feed() first")
        if not self._subscribed:
            return []
        symbols = self.subscribed_symbols
        try:
            payloads = self.feed.poll(symbols)
        except FeedError as exc:
            self._stats["poll_errors"] += 1
            logger.warning("poll failed (%s); reconnecting", exc)
            self._reconnect()
            payloads = self.feed.poll(symbols)
        return self.ingest(payloads)

    def ingest(self, payloads: Sequence[Mapping[str, Any]]) -> list[Tick]:
        """Normalize and process raw payloads (also used for replay/backfill)."""
        accepted: list[Tick] = []
        source = self.feed.name if self.feed is not None else ""
        naive_tz = self.feed.naive_tz if self.feed is not None else "UTC"
        for raw in payloads:
            try:
                tick = normalize_tick(raw, naive_tz=naive_tz, source=source)
            except NormalizationError as exc:
                self._stats["invalid_payloads"] += 1
                logger.warning("dropped invalid payload (%s): %r", exc.code, raw)
                continue
            if tick.symbol not in self._subscribed:
                self._stats["ignored_unsubscribed"] += 1
                continue
            self._accept(tick)
            accepted.append(tick)
        return accepted

    def _accept(self, tick: Tick) -> None:
        self._stats["ticks_received"] += 1
        self._quotes[tick.symbol] = tick
        buffer = self._ticks.get(tick.symbol)
        if buffer is None:
            buffer = deque(maxlen=self.config.tick_buffer_size)
            self._ticks[tick.symbol] = buffer
        buffer.append(tick)
        for callback in list(self._tick_callbacks):
            try:  # a broken observer must not break ingestion
                callback(tick)
            except Exception:  # noqa: BLE001
                logger.exception("tick callback %r failed for %s", callback, tick.symbol)
        self._aggregator.add_tick(tick)

    def _on_aggregator_bar(self, bar: Bar) -> None:
        key = (bar.symbol, bar.timeframe)
        buffer = self._bars.get(key)
        if buffer is None:
            buffer = deque(maxlen=self.config.bar_buffer_size)
            self._bars[key] = buffer
        buffer.append(bar)
        if not bar.synthetic:  # fabrications never reach the database
            self._pending_db.append(bar)

    def flush_bars(
        self,
        symbol: str | None = None,
        timeframe: str | Timeframe | None = None,
    ) -> list[Bar]:
        """Force-close in-progress bars (end of session / shutdown)."""
        return self._aggregator.force_close(symbol=symbol, timeframe=timeframe)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_current_quote(self, symbol: str) -> Tick | None:
        """Latest accepted tick for ``symbol``, or None."""
        return self._quotes.get(symbol.strip().upper())

    def get_current_bar(self, symbol: str, timeframe: str | Timeframe) -> Bar | None:
        """The in-progress bar if one is building, else the latest closed bar."""
        symbol = symbol.strip().upper()
        current = self._aggregator.current_bar(symbol, timeframe)
        if current is not None:
            return current
        buffer = self._bars.get((symbol, _coerce_timeframe(timeframe)))
        return buffer[-1] if buffer else None

    def get_recent_bars(
        self, symbol: str, timeframe: str | Timeframe, count: int = 100
    ) -> list[Bar]:
        """Up to ``count`` most recent *closed* bars, oldest first."""
        if count < 1:
            raise ValueError("count must be >= 1")
        buffer = self._bars.get((symbol.strip().upper(), _coerce_timeframe(timeframe)))
        if not buffer:
            return []
        return list(buffer)[-count:]

    def get_recent_ticks(self, symbol: str, count: int = 100) -> list[Tick]:
        """Up to ``count`` most recent ticks, oldest first."""
        if count < 1:
            raise ValueError("count must be >= 1")
        buffer = self._ticks.get(symbol.strip().upper())
        if not buffer:
            return []
        return list(buffer)[-count:]

    @property
    def stats(self) -> dict[str, int]:
        """Ingestion + aggregation counters (copies; safe to mutate)."""
        merged = dict(self._stats)
        merged.update(self._aggregator.stats.to_dict())
        merged["pending_db_bars"] = len(self._pending_db)
        return merged

    # ------------------------------------------------------------------
    # Persistence — market_data_cache
    # ------------------------------------------------------------------

    def persist_closed_bars(self, db: "DatabaseManager") -> int:
        """Write pending closed bars to ``market_data_cache``. Idempotent.

        Bars already present (same symbol/exchange/timeframe/ts — the
        ``uq_mdc_bar`` key) are skipped, so replays after a restart are
        safe. Synthetic gap-fill bars are never persisted. Returns the
        number of rows written; pending bars are kept on failure.
        """
        if not self._pending_db:
            return 0
        from backtest.db.models import MarketDataCache  # deferred: keep imports I/O-free

        source = self.feed.name if self.feed is not None else "unknown"
        exchange = self.config.exchange
        written = 0
        with db.session() as session:
            for bar in self._pending_db:
                exists = (
                    session.query(MarketDataCache.data_id)
                    .filter_by(
                        symbol=bar.symbol,
                        exchange=exchange,
                        timeframe=bar.timeframe,
                        ts=bar.ts,
                    )
                    .first()
                )
                if exists:
                    continue
                session.add(
                    MarketDataCache(
                        symbol=bar.symbol,
                        exchange=exchange,
                        timeframe=bar.timeframe,
                        ts=bar.ts,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        bid=bar.bid,
                        ask=bar.ask,
                        source=bar.source or source,
                    )
                )
                written += 1
        self._pending_db.clear()  # only on success — the session commits on exit
        return written
