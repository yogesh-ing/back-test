"""Shared Market Data Bus — U6.2 per UNIFIED-TRADING-TASKS.md.

**Rule (architecture §5.2, C2 completion): one feed per
``(source, symbol, timeframe)``, shared by every runner subscribed to it.**

What was duplicated before this module:

1. **Option chains.** Every option runner's ``OptionsBridge`` built a private
   ``SyntheticChainGenerator`` + ``SyntheticQuoteProvider``. Two NIFTY option
   runners therefore held *two different views of NIFTY* (each priced off its
   own last-received bar) and paid double the chain-generation cost. N runners
   on one underlying = N independent chains — the overload the user flagged.

2. **Bar feeds.** The :class:`PortfolioManager` already owns one
   ``SyntheticFeed`` fanned out to all runners, so equity bars were shared.
   But it is hardcoded at construction and per-symbol membership is managed by
   hand in ``add_runner``/``remove_runner`` — nothing refcounts or shares it.

This module fixes both with refcounted registries:

* :class:`FeedRegistry` — maps ``(source, symbol, timeframe)`` to a feed
  instance with a subscriber count. The manager subscribes runners on spawn
  and unsubscribes on removal; a feed stops at zero subscribers.
* :class:`ChainBus` — maps ``underlying`` to ONE
  ``SyntheticChainGenerator`` shared by every option runner on that
  underlying. Spots are set per bar from the shared feed's bars, so all
  runners on NIFTY see the same chain priced off the same spot.
* :func:`option_quote_provider` — returns a provider wired to the shared
  generator (``SyntheticQuoteProvider``), replacing the per-bridge private
  pair inside ``OptionsBridge``.

C2 preserved: strategies never touch this module — the engine (bridge) does.
The strategy still receives data through engine calls; the bus only changes
*where the engine gets it*.

Thread-safety: registries are guarded by an RLock; feeds themselves keep
their own locks (``SyntheticFeed`` already has one).

V1 scope: the manager's single synthetic feed is registered here explicitly
rather than created here — the manager keeps its lifecycle, the registry
keeps the accounting. Gap #1 landed as promised: :class:`MStockBarFeed` is
the live poll thread registered under ``("mstock", symbol, timeframe)`` —
one thread per manager regardless of runner count.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)

logger = logging.getLogger("backtest.forward.feed_registry")

FeedKey = Tuple[str, str, str]  # (source, symbol, timeframe)


@dataclass
class _FeedEntry:
    """One registered feed + its subscriber count."""

    feed: Any
    key: FeedKey
    subscribers: int = 0
    # Called when subscribers hit zero. Feeds that own a thread pass their
    # ``stop``; the portfolio feed is manager-owned, so it passes nothing.
    on_release: Optional[Callable[[], None]] = field(default=None, repr=False)


class FeedRegistry:
    """Refcounted registry of bar feeds keyed by (source, symbol, timeframe).

    ``subscribe`` returns the feed and bumps the count; ``release`` drops it
    and stops the feed at zero (only when the feed owns its own lifecycle —
    manager-owned feeds are simply evicted from the registry).
    """

    def __init__(self) -> None:
        self._feeds: Dict[FeedKey, _FeedEntry] = {}
        self._lock = threading.RLock()

    def subscribe(self, source: str, symbol: str, timeframe: str, feed: Any = None) -> Any:
        """Return the feed for this key, creating it from ``feed`` on first use.

        ``feed`` is required on the first subscribe for a key (the caller
        supplies the concrete feed object); later subscribers get the same
        instance back and their ``feed`` argument (if any) is ignored.
        """
        key = self._key(source, symbol, timeframe)
        with self._lock:
            entry = self._feeds.get(key)
            if entry is None:
                if feed is None:
                    raise KeyError(
                        f"no feed registered for {key} — pass a feed on first subscribe"
                    )
                entry = _FeedEntry(feed=feed, key=key)
                self._feeds[key] = entry
                logger.info("feed registered: %s", key)
            entry.subscribers += 1
            logger.debug("feed %s → %d subscribers", key, entry.subscribers)
            return entry.feed

    def release(self, source: str, symbol: str, timeframe: str) -> int:
        """Drop one subscriber; stop/evict the feed at zero.

        Returns the remaining subscriber count (0 once released fully).
        Releasing an unknown key is a no-op returning 0 — idempotent, so
        remove_runner paths never need to check.
        """
        key = self._key(source, symbol, timeframe)
        with self._lock:
            entry = self._feeds.get(key)
            if entry is None:
                return 0
            entry.subscribers = max(0, entry.subscribers - 1)
            remaining = entry.subscribers
            if remaining == 0:
                self._feeds.pop(key, None)
                if entry.on_release is not None:
                    try:
                        entry.on_release()
                    except Exception:  # noqa: BLE001 — a bad stop must not kill removal
                        logger.exception("feed release callback failed for %s", key)
                logger.info("feed released (0 subscribers): %s", key)
            else:
                logger.debug("feed %s → %d subscribers", key, remaining)
            return remaining

    def subscriber_count(self, source: str, symbol: str, timeframe: str) -> int:
        """Current subscriber count for a key (0 when unknown)."""
        with self._lock:
            entry = self._feeds.get(self._key(source, symbol, timeframe))
            return entry.subscribers if entry else 0

    def stats(self) -> Dict[str, int]:
        """Registry health snapshot: distinct feeds and total subscriptions."""
        with self._lock:
            return {
                "feeds": len(self._feeds),
                "subscriptions": sum(e.subscribers for e in self._feeds.values()),
            }

    @staticmethod
    def _key(source: str, symbol: str, timeframe: str) -> FeedKey:
        return (str(source).lower(), str(symbol).upper(), str(timeframe).lower())


class ChainBus:
    """One chain generator per option underlying, shared by all its runners.

    Every option runner on NIFTY reads the same ``SyntheticChainGenerator``,
    so strikes, expiry and premiums are identical across runners and priced
    off the one shared spot. Generators are created lazily and refcounted —
    when the last option runner on an underlying detaches, the entry goes.
    """

    def __init__(self) -> None:
        self._generators: Dict[str, _FeedEntry] = {}
        self._lock = threading.RLock()

    def acquire(self, underlying: str) -> SyntheticChainGenerator:
        """Return the shared generator for ``underlying``, +1 subscriber."""
        key = str(underlying).upper()
        with self._lock:
            entry = self._generators.get(key)
            if entry is None:
                entry = _FeedEntry(feed=SyntheticChainGenerator(), key=(key, "", ""))
                self._generators[key] = entry
                logger.info("chain generator registered: %s", key)
            entry.subscribers += 1
            return entry.feed

    def release(self, underlying: str) -> int:
        """Drop one subscriber; evict the generator at zero."""
        key = str(underlying).upper()
        with self._lock:
            entry = self._generators.get(key)
            if entry is None:
                return 0
            entry.subscribers = max(0, entry.subscribers - 1)
            if entry.subscribers == 0:
                self._generators.pop(key, None)
                logger.info("chain generator released: %s", key)
                return 0
            return entry.subscribers

    def subscriber_count(self, underlying: str) -> int:
        with self._lock:
            entry = self._generators.get(str(underlying).upper())
            return entry.subscribers if entry else 0

    def generator_count(self) -> int:
        with self._lock:
            return len(self._generators)


def option_quote_provider(
    generator: SyntheticChainGenerator,
) -> SyntheticQuoteProvider:
    """A quote provider priced off the shared generator (per-subscriber wrapper).

    The provider itself is cheap and keeps per-book state (contract registry,
    pricing reference), so each runner gets its own provider — but the heavy,
    market-defining object (the generator: spots, strikes, expiry math) is the
    shared one. Same contract for every runner, one source of spots.
    """
    return SyntheticQuoteProvider(chain_generator=generator)


class MStockBarFeed:
    """Live bar feed for ``source=mstock`` runners (Gap #1, lands in the bus).

    ONE poll thread per manager, round-robin over every subscribed mstock
    symbol — runner count never multiplies API polls (the §5.2 rule; mStock
    rate limits die at ~1 req/s). Bars are pushed through the *same*
    ``on_bar`` / ``on_tick_end`` hooks the :class:`SyntheticFeed` uses, so
    the manager's fan-out and every runner are unchanged (C2: the engine
    hands data in — runners cannot tell which feed produced a bar).

    Polling is delegated to a duck-typed client with ``latest_bar(symbol)
    -> dict | None`` — by default :class:`MStockLiveFeed` (which owns the
    market-hours gate, credentials, and scriptmaster resolution). Bars are
    normalized to the runner bar shape and deduped per symbol (a poll that
    returns the same candle twice is dropped, so retries never re-feed).
    Feed errors are logged and skipped — a data hiccup must never take the
    trading loop down.

    While the exchange is closed, each symbol is polled at most once (a
    startup catch-up so runners are seeded with the latest real bar) and
    then the loop idles until market open.
    """

    def __init__(
        self,
        feed_client: Any = None,
        poll_interval_s: float = 60.0,
    ) -> None:
        self.on_bar: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.on_tick_end: Optional[Callable[[str], None]] = None
        self.poll_interval_s = float(poll_interval_s)
        # Duck-typed: anything with ``latest_bar(symbol) -> dict | None``.
        if feed_client is None:
            from backtest.data.mstock_live_feed import MStockLiveFeed

            feed_client = MStockLiveFeed(poll_interval_s=self.poll_interval_s)
        self._client = feed_client
        self._symbols: List[str] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_ts: Dict[str, str] = {}  # symbol → last pushed bar ts

    # -- subscription (same shape as SyntheticFeed) ------------------------

    def add_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            for symbol in symbols:
                symbol = str(symbol).upper()
                if symbol not in self._symbols:
                    self._symbols.append(symbol)
                    logger.info("mstock feed subscribed %s", symbol)

    def remove_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            for symbol in symbols:
                symbol = str(symbol).upper()
                if symbol in self._symbols:
                    self._symbols.remove(symbol)
                self._last_ts.pop(symbol, None)

    # -- lifecycle ----------------------------------------------------------

    def start(self, warmup: bool = False) -> None:
        """Start the poll thread (idempotent). ``warmup`` accepted for API
        parity with :meth:`SyntheticFeed.start` — real bars need no warmup."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="mstock-bar-feed", daemon=True
            )
            self._thread.start()
            logger.info(
                "mstock feed started: %d symbols, %.0fs polls",
                len(self._symbols),
                self.poll_interval_s,
            )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        logger.info("mstock feed stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- polling ------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — the loop survives anything
                logger.exception("mstock feed poll failed")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.poll_interval_s - elapsed))

    def _poll_once(self) -> int:
        """One round-robin sweep: fetch + push a bar per symbol.

        Returns how many bars were actually delivered. Split out of the loop
        body so tests can drive polls without threads.
        """
        with self._lock:
            symbols = list(self._symbols)
        if not symbols:
            return 0

        delivered = 0
        market_open = self._market_open()
        for symbol in symbols:
            if self._stop.is_set():
                break
            # Market closed + already seeded → idle (never hammer a closed API).
            if not market_open and symbol in self._last_ts:
                continue
            bar = self._fetch_bar(symbol)
            if bar is None:
                continue
            with self._lock:
                if bar["ts"] <= self._last_ts.get(symbol, ""):
                    continue  # dedupe: same candle as last push
                self._last_ts[symbol] = bar["ts"]
            if self.on_bar is not None:
                self.on_bar(symbol, bar)
                delivered += 1
        if delivered and self.on_tick_end is not None:
            self.on_tick_end(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        return delivered

    def _fetch_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Latest bar for ``symbol``, normalized + error-soft."""
        try:
            raw = self._client.latest_bar(symbol)
        except Exception as exc:  # noqa: BLE001 — feed hiccups are logged, not fatal
            logger.warning("mstock feed: latest_bar(%s) failed: %s", symbol, exc)
            return None
        return self._normalize_bar(raw)

    @staticmethod
    def _market_open() -> bool:
        try:
            from backtest.data.mstock_live_feed import _market_open

            return _market_open()
        except Exception:  # noqa: BLE001 — a gate failure must not stop polling
            return True

    @staticmethod
    def _normalize_bar(raw: Any) -> Optional[Dict[str, Any]]:
        """Client row → runner bar shape (str ts the whole stack agrees on).

        The runner de-dupes by string comparison (``ts <= last``), so every
        feed must emit one canonical format. ``pd.to_datetime`` absorbs both
        mStock shapes (ISO strings and epoch millis — the latter detected by
        magnitude and read as milliseconds).
        """
        if not isinstance(raw, dict):
            return None
        try:
            raw_ts = raw.get("ts") or raw.get("timestamp")
            if isinstance(raw_ts, (int, float)) and raw_ts > 1_000_000_000_000:
                ts = pd.to_datetime(raw_ts, unit="ms")
            else:
                ts = pd.to_datetime(raw_ts)
            close = float(raw["close"])
            return {
                "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(raw.get("open", close)),
                "high": float(raw.get("high", close)),
                "low": float(raw.get("low", close)),
                "close": close,
                "volume": float(raw.get("volume", 0) or 0),
            }
        except (KeyError, TypeError, ValueError):
            logger.debug("mstock feed: unparseable bar row skipped: %r", raw)
            return None


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------

_FEED_REGISTRY: Optional[FeedRegistry] = None
_CHAIN_BUS: Optional[ChainBus] = None
_BUS_LOCK = threading.Lock()


def get_feed_registry() -> FeedRegistry:
    global _FEED_REGISTRY
    with _BUS_LOCK:
        if _FEED_REGISTRY is None:
            _FEED_REGISTRY = FeedRegistry()
        return _FEED_REGISTRY


def get_chain_bus() -> ChainBus:
    global _CHAIN_BUS
    with _BUS_LOCK:
        if _CHAIN_BUS is None:
            _CHAIN_BUS = ChainBus()
        return _CHAIN_BUS


def reset_data_bus() -> None:
    """Tear down both registries (tests / manager restart)."""
    global _FEED_REGISTRY, _CHAIN_BUS
    with _BUS_LOCK:
        _FEED_REGISTRY = None
        _CHAIN_BUS = None
