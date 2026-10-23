"""Shared Market Data Bus — U6.2 per docs/archive/UNIFIED-TRADING-TASKS.md.

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

from backtest.data.sources_policy import (
    SourceDisabledError,
    default_broker_source,
    policy_key,
    require_synthetic,
    synthetic_enabled,
)
from backtest.forward.feed_quality import get_quality_monitor
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.options.quote_providers import HotStrikeRing  # noqa: F401

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

    P1.1: ``source="mstock"`` acquires a shared :class:`LiveChainProvider`
    instead — ONE real chain/LTP object per underlying regardless of runner
    count (the §5.2 rate-limit rule; the instrument master is fetched once
    per TTL and quotes share one TTL cache). A live acquire **requires** a
    quote broker client — there is no silent synthetic substitution on the
    live path (the caller decides the fallback and labels it).

    PRD §4.1/§4.2: the live feed is handed out wrapped in
    :class:`BidAskQuoteProvider` (fills cross the spread, held legs mark off
    the far side). ``source_name`` reads ``live:<broker>+bidask``.
    """

    def __init__(self) -> None:
        self._generators: Dict[str, _FeedEntry] = {}
        self._live: Dict[str, _FeedEntry] = {}
        self._lock = threading.RLock()

    def acquire(
        self,
        underlying: str,
        source: str = "",
        broker: Any = None,
        *,
        wing_distance_pct: Optional[float] = None,
        warm_ring: bool = True,
    ) -> Any:
        """Return the shared feed for ``underlying``, +1 subscriber.

        A broker source (mstock/dhan) → the shared :class:`LiveChainProvider`,
        spread-wrapped (``BidAskQuoteProvider``); needs a broker client
        exposing ``get_option_chain`` / ``get_option_quote``, and there is no
        silent synthetic substitution.
        No source named → the data-source policy's default feed, never a
        hard-coded one, so a caller that forgets ``source=`` on a deployment
        with synthetic off cannot quietly end up on generated candles.
        """
        key = str(underlying).upper()
        source_key = self._resolve_source(source)
        live_inner: Any = None
        should_warm = False
        with self._lock:
            if source_key == "synthetic":
                return self._acquire_synthetic(key)
            if broker is None:
                raise ValueError(
                    f"ChainBus.acquire(source={source_key!r}) requires a quote "
                    "broker client — no silent synthetic substitution"
                )
            entry = self._live.get(key)
            if entry is None:
                from backtest.options.quote_providers import LiveChainProvider

                # The spread wrapper is part of the SHARED feed, not something
                # each caller adds on top: wrapping per-acquire would break the
                # one-object-per-underlying contract (identity + refcount), and
                # wrapping only on the runner path would make the first acquirer
                # decide whether the dashboard's unwrapped entry ever crosses.
                live_inner = LiveChainProvider(broker)
                entry = _FeedEntry(
                    feed=BidAskQuoteProvider(live_inner),
                    key=(key, source_key, ""),
                )
                self._live[key] = entry
                should_warm = bool(warm_ring and live_inner.has_ws_ring_support())
                logger.info("live chain provider registered: %s (%s)", key, source_key)
            else:
                live_inner = getattr(entry.feed, "inner", entry.feed)
            entry.subscribers += 1
            feed_out = entry.feed

        if wing_distance_pct is not None and wing_distance_pct > 0 and live_inner is not None:
            reg_wing = getattr(live_inner, "register_wing_distance", None)
            if callable(reg_wing):
                reg_wing(key, float(wing_distance_pct))
        if should_warm and live_inner is not None:
            warm_fn = getattr(live_inner, "warm_chain_and_ring", None)
            if callable(warm_fn):
                warm_fn(
                    key,
                    wing_distance_pct=wing_distance_pct,
                    async_warm=True,
                )
        return feed_out

    @staticmethod
    def _resolve_source(source: str) -> str:
        """Canonical source key for a bus call, answered by the policy.

        ``None``/"" (the caller named nothing) resolves to the deployment's
        default broker feed. When the policy enables nothing at all the answer
        stays synthetic — which is only reachable through
        :meth:`_acquire_synthetic`, and that refuses.
        """
        key = policy_key(source)
        if key:
            return key
        return default_broker_source() or "synthetic"

    def _acquire_synthetic(self, key: str) -> SyntheticChainGenerator:
        # The single place a shared generated chain is ever built, so the
        # policy gate lives here rather than at each call site: with synthetic
        # disabled in config/data_sources.yaml this raises instead of handing
        # back a generator that produces candles nobody asked for.
        require_synthetic(f"chain feed for {key}")
        with self._lock:
            entry = self._generators.get(key)
            if entry is None:
                entry = _FeedEntry(feed=SyntheticChainGenerator(), key=(key, "", ""))
                self._generators[key] = entry
                logger.info("chain generator registered: %s", key)
            entry.subscribers += 1
            return entry.feed

    def release(self, underlying: str, source: str = "") -> int:
        """Drop one subscriber; evict the generator at zero."""
        key = str(underlying).upper()
        is_synthetic = self._resolve_source(source) == "synthetic"
        evicted_feed: Any = None
        with self._lock:
            store = self._generators if is_synthetic else self._live
            entry = store.get(key)
            if entry is None:
                return 0
            entry.subscribers = max(0, entry.subscribers - 1)
            if entry.subscribers == 0:
                store.pop(key, None)
                if not is_synthetic:
                    evicted_feed = entry.feed
                logger.info("chain generator released: %s (%s)", key, source)
                remaining = 0
            else:
                remaining = entry.subscribers

        if evicted_feed is not None:
            rel_ring = getattr(evicted_feed, "release_hot_ring", None)
            if callable(rel_ring):
                try:
                    rel_ring(key)
                except Exception:  # noqa: BLE001
                    logger.debug("release_hot_ring(%s) failed", key, exc_info=True)
        return remaining

    def subscriber_count(self, underlying: str, source: str = "") -> int:
        with self._lock:
            store = (
                self._generators
                if self._resolve_source(source) == "synthetic"
                else self._live
            )
            entry = store.get(str(underlying).upper())
            return entry.subscribers if entry else 0

    def generator_count(self) -> int:
        with self._lock:
            return len(self._generators) + len(self._live)


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


def _default_quote_broker(source: str | None = None) -> Any | None:
    """The authenticated broker FOR ``source``, or ``None`` (never raises).

    Resolved through the broker session manager — the same place the web UI's
    broker-auth flow lands its sessions. Multi-broker (Phase A): with concurrent
    sessions this asks for the *named* broker's session (``source="dhan"`` →
    the Dhan session even while mStock is the UI-active broker); with no name
    it falls back to the legacy active-broker resolution. ``None`` means "not
    authenticated": callers decide the fallback (and label it) themselves.
    """
    try:
        from backtest.brokers.session_manager import get_session_manager

        mgr = get_session_manager()
        if source:
            return mgr.get_authenticated_broker(str(source).lower())
        if mgr.is_authenticated():
            return mgr.get_active_broker()
    except Exception:  # noqa: BLE001 — session resolution must never break a spawn
        logger.debug("quote-broker resolution failed", exc_info=True)
    return None


def option_quote_provider_for(
    source: str,
    underlying: str,
    quote_broker: Any | None = None,
    *,
    wing_distance_pct: Optional[float] = None,
) -> tuple[Any, str]:
    """``(quote_provider, label)`` for an option runner's bridge — P1.1.

    * ``source="mstock"``/``"dhan"``/``"upstox"`` + an authenticated broker
      of the same name (explicit or via the session manager) → the shared
      :class:`LiveChainProvider` wrapped in :class:`BidAskQuoteProvider`
      (``"live:<broker>+bidask"``): real chains, real LTP, one API budget per
      underlying (ChainBus refcount) — and fills that cross the spread while
      held legs mark off the far side (PRD §4.1/§4.2).
    * otherwise → the synthetic pair (``"synthetic:bs"``), **but only if the
      data-source policy allows synthetic at all**.

    The bus wraps broker feeds once, at entry creation, rather than leaving it
    to each call site, because "paper filled at LTP" is not evidence of
    anything: a long call bought at the last traded price and marked at the
    last traded price never pays the spread it would have to cross twice in a
    real session, so the paper P&L is systematically better than live would
    have been. The synthetic demo feed stays unwrapped — it is labelled
    ``synthetic:bs`` precisely because nobody trades on it, and its
    Black-Scholes bid/ask is a made-up ₹0.50 half-spread.

    The fallback used to be unconditional: ask for mStock with no session and
    you got Black-Scholes chains, labelled honestly but trading anyway — which
    is how a `paper/MSTOCK` runner ended up showing P&L on prices nobody ever
    quoted (2026-09-30). With ``config/data_sources.yaml`` shipped as it is —
    synthetic off — a runner with no authenticated session now **refuses to
    build a bridge** instead. Operators who genuinely want a synthetic demo
    (or the test suite) get the old labelled fallback back by setting
    ``BACKTEST_DATA_PROFILE=testing``; nothing else about this function changes.
    """
    underlying = str(underlying).upper()
    source_key = str(source).lower()
    if source_key in ("mstock", "dhan", "upstox"):
        broker = quote_broker if quote_broker is not None else _default_quote_broker(source_key)
        # A session-manager-resolved broker always carries broker_name; a
        # duck-typed injected broker may not — accept it for the matching
        # source only (an explicit quote_broker is a deliberate override).
        name = getattr(broker, "broker_name", None)
        if broker is not None and (name is None or name == source_key):
            provider = get_chain_bus().acquire(
                underlying,
                source=source_key,
                broker=broker,
                wing_distance_pct=wing_distance_pct,
            )
            return provider, provider.source_name
        if not synthetic_enabled():
            raise SourceDisabledError(
                f"option runner on {underlying} asked for source={source_key} but there is "
                f"no authenticated {source_key} session, and synthetic chains are disabled "
                f"in config/data_sources.yaml — so there is nothing to fall back to. "
                f"Authenticate {source_key} in Settings, then start or resume the runner "
                f"(it upgrades to the live chain on resume). Refusing to price on "
                f"generated candles is the point: a fake chain shows fake P&L."
            )
        logger.warning(
            "option runner on %s requested source=%s but no authenticated "
            "%s session — using synthetic chain (labelled)",
            underlying,
            source_key,
            source_key,
        )
    elif not synthetic_enabled():
        raise SourceDisabledError(
            f"option runner on {underlying} requested source={source_key!r}, which "
            f"is not a broker feed, and synthetic chains are disabled in "                f"config/data_sources.yaml — option runners must be created on mstock, "
                f"dhan or upstox."
        )
    generator = get_chain_bus().acquire(underlying)
    return option_quote_provider(generator), "synthetic:bs"


class _BrokerBarFeedBase:
    """Shared plumbing for broker poll threads (mStock/Dhan/...).

    ONE poll thread per manager per broker, round-robin over subscribed
    symbols; bars pushed through the same ``on_bar``/``on_tick_end`` hooks
    as :class:`SyntheticFeed` (C2: runners cannot tell which feed produced a
    bar). Subclasses set ``broker_name`` and default ``_feed_client_class``.
    Every delivered bar is observed by the feed-quality monitor for the
    (broker, symbol) pair — the multi-broker comparison data source.
    """

    broker_name = "unset"
    _feed_client_class: Optional[type] = None

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
            assert self._feed_client_class is not None  # noqa: S101 — subclass contract
            feed_client = self._feed_client_class()
        self._client = feed_client
        self._symbols: List[str] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_ts: Dict[str, str] = {}  # symbol → last pushed bar ts
        self._last_rest_logged_day: Optional[str] = None

    def add_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            for symbol in symbols:
                symbol = str(symbol).upper()
                if symbol not in self._symbols:
                    self._symbols.append(symbol)
                    logger.info("%s feed subscribed %s", self.broker_name, symbol)

    def remove_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            for symbol in symbols:
                symbol = str(symbol).upper()
                if symbol in self._symbols:
                    self._symbols.remove(symbol)
                self._last_ts.pop(symbol, None)

    def start(self, warmup: bool = False) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"{self.broker_name}-bar-feed", daemon=True
            )
            self._thread.start()
            logger.info(
                "%s feed started: %d symbols, %.0fs polls",
                self.broker_name,
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
        logger.info("%s feed stopped", self.broker_name)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — the loop survives anything
                logger.exception("%s feed poll failed", self.broker_name)
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.poll_interval_s - elapsed))

    def _poll_once(self) -> int:
        with self._lock:
            symbols = list(self._symbols)
        if not symbols:
            return 0

        market_open = self._market_open()

        # Market-status awareness (§5): skip polling fully on holidays and weekends
        if not market_open:
            day_state, reason = self._market_day_state()
            if day_state in ("HOLIDAY", "WEEKEND"):
                from backtest.live.market_status import IST

                today_str = datetime.now(IST).strftime("%Y-%m-%d")
                if self._last_rest_logged_day != today_str:
                    self._last_rest_logged_day = today_str
                    logger.info("%s feed resting — %s", self.broker_name, reason)
                return 0

        delivered = 0
        for symbol in symbols:
            if self._stop.is_set():
                break
            if not market_open and symbol in self._last_ts:
                continue
            bar = self._fetch_bar(symbol)
            if bar is None:
                continue
            with self._lock:
                if bar["ts"] <= self._last_ts.get(symbol, ""):
                    continue
                self._last_ts[symbol] = bar["ts"]
            if self.on_bar is not None:
                self.on_bar(symbol, bar)
                delivered += 1
        if delivered and self.on_tick_end is not None:
            self.on_tick_end(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        return delivered

    def ingest_ws_bar(
        self,
        symbol: str,
        raw_bar: Dict[str, Any],
        *,
        timeframe: str = "1min",
        emit_tick_end: bool = True,
    ) -> bool:
        """Deliver a WebSocket-aggregated bar with shared ``_last_ts`` dedupe.

        Shares ``self._last_ts`` with the REST fallback poller so a 60s REST
        failover sweep never re-emits a minute bar already pushed from WS.
        """
        sym = str(symbol).upper()
        bar = self._normalize_bar(raw_bar)
        if bar is None:
            return False
        bar["_source"] = self.broker_name
        if timeframe:
            bar["_timeframe"] = str(timeframe)

        with self._lock:
            if sym not in self._symbols:
                return False
            if bar["ts"] <= self._last_ts.get(sym, ""):
                return False
            self._last_ts[sym] = bar["ts"]

        get_quality_monitor(self.broker_name, sym).observe_bar(bar["ts"])
        if self.on_bar is not None:
            self.on_bar(sym, bar)
        if emit_tick_end and self.on_tick_end is not None:
            self.on_tick_end(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        return True

    @classmethod
    def _market_day_state(cls) -> tuple[str, str]:
        """Resolve current market day state and reason (fail-open to TRADING_DAY)."""
        try:
            from backtest.live.market_status import get_market_day_state

            info = get_market_day_state()
            ds = str(info.get("day_state", "TRADING_DAY"))
            if ds == "HOLIDAY":
                return "HOLIDAY", str(info.get("holiday_name") or "Holiday")
            if ds == "WEEKEND":
                return "WEEKEND", "Weekend"
            return "TRADING_DAY", "Trading day"
        except Exception:  # noqa: BLE001 — fail-open rule: calendar error assumes trading day
            return "TRADING_DAY", "Calendar unreadable (fail-open)"

    def _fetch_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        monitor = get_quality_monitor(self.broker_name, symbol)
        try:
            raw = self._client.latest_bar(symbol)
        except Exception as exc:  # noqa: BLE001 — feed hiccups are logged, not fatal
            logger.warning("%s feed: latest_bar(%s) failed: %s", self.broker_name, symbol, exc)
            monitor.observe_error(str(exc))
            return None
        bar = self._normalize_bar(raw)
        if bar is None:
            monitor.observe_poll_no_data("unparseable bar row")
        else:
            # Source tag for the manager's source-isolated fan-out: a live bar
            # may only drive runners whose config.source matches this feed's
            # broker (2026-09-30) — keeps synthetic bars out of mstock/dhan
            # runners and vice versa.
            bar["_source"] = self.broker_name
            if getattr(self._client, "emits_minute_bars", False):
                bar["_timeframe"] = "1min"
            monitor.observe_bar(bar["ts"])
        return bar

    @staticmethod
    def _market_open() -> bool:
        try:
            from backtest.data.mstock_live_feed import _market_open

            return _market_open()
        except Exception:  # noqa: BLE001 — a gate failure must not stop polling
            return True

    @staticmethod
    def _normalize_bar(raw: Any) -> Optional[Dict[str, Any]]:
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
            logger.debug("broker feed: unparseable bar row skipped: %r", raw)
            return None


class MStockBarFeed(_BrokerBarFeedBase):
    """Live bar feed for ``source=mstock`` runners (Gap #1, lands in the bus).

    ONE poll thread per manager, round-robin over every subscribed mstock
    symbol — runner count never multiplies API polls (the §5.2 rule; mStock
    rate limits die at ~1 req/s). See :class:`_BrokerBarFeedBase` for the
    shared polling/normalization/dedupe machinery (2026-09-25 refactor: the
    per-broker class is now just config + the Dhan sibling reuses it).
    """

    broker_name = "mstock"
    _feed_client_class = None  # set lazily in __init__ via class attr override

    def __init__(
        self,
        feed_client: Any = None,
        poll_interval_s: float = 60.0,
    ) -> None:
        if feed_client is None:
            from backtest.data.mstock_live_feed import MStockLiveFeed

            feed_client = MStockLiveFeed(poll_interval_s=poll_interval_s)
        super().__init__(feed_client=feed_client, poll_interval_s=poll_interval_s)


class DhanBarFeed(_BrokerBarFeedBase):
    """Live bar feed for ``source=dhan`` runners (2026-09-25).

    Same one-thread-per-manager round-robin as :class:`MStockBarFeed`, but
    delegating to :class:`~backtest.data.dhan_live_feed.DhanLiveFeed`.
    Requires an active Dhan broker session (Dhan data APIs are token-gated).
    """

    broker_name = "dhan"

    def __init__(
        self,
        feed_client: Any = None,
        poll_interval_s: float = 60.0,
    ) -> None:
        if feed_client is None:
            from backtest.data.dhan_live_feed import DhanLiveFeed

            feed_client = DhanLiveFeed()
        super().__init__(feed_client=feed_client, poll_interval_s=poll_interval_s)


class UpstoxBarFeed(_BrokerBarFeedBase):
    """Live bar feed for ``source=upstox`` runners (2026-10-13).

    Same one-thread-per-manager round-robin as :class:`MStockBarFeed`,
    delegating to :class:`~backtest.data.upstox_live_feed.UpstoxLiveFeed`.
    Requires an active Upstox broker session (Upstox data APIs are
    Bearer-token gated).
    """

    broker_name = "upstox"

    def __init__(
        self,
        feed_client: Any = None,
        poll_interval_s: float = 60.0,
    ) -> None:
        if feed_client is None:
            from backtest.data.upstox_live_feed import UpstoxLiveFeed

            feed_client = UpstoxLiveFeed()
        super().__init__(feed_client=feed_client, poll_interval_s=poll_interval_s)


# Backward-compat alias for anything importing the old name.
_BrokerBarFeed = MStockBarFeed


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
