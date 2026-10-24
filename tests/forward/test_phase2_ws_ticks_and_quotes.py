"""Phase 2 regression suite: MStockTickBus, WS Freshness (F5), HotStrikeRing & Zero-TTL WS Quotes.

Covers:
1. ``MStockWebSocketWrapper`` / ``MStockTickBus``: monotonic ``recv_mono`` stamp,
   socket-wide ``last_any_tick_mono()``, 150ms coalesced background dispatch off
   Twisted's reactor thread, and listener exception isolation.
2. ``MStockBroker.get_ws_quote``: distinguishes live quiet OTM strikes on a
   healthy socket from post-15:30 frozen marks (Finding F5) and closed sockets;
   batch ``subscribe_tokens_ws`` / ``unsubscribe_tokens_ws`` + ref-counted pins;
   eliminates blocking ``time.sleep(0.5)`` on cold leg lookups.
3. ``LiveQuoteProvider`` & ``CachedQuoteProvider``: zero-TTL bypass for live WS
   depth quotes (``source="ws"``), immediate upgrade from cached 1-sided HTTP
   fallback once WS depth arrives, ``numeric_token`` forwarding, and zero/error
   cache exclusion.
4. ``HotStrikeRing`` + ``ChainBus`` + ``MStockBarFeed.ingest_ws_bar``: ``ATM ± 6``
   (26 tokens) ``MODE_FULL`` pre-subscription for NIFTY (step 50) & BANKNIFTY
   (step 100), strangle wing support, ``ATM ± 8`` eviction hysteresis, pinned
   open-position protection, bus release cleanup, and WS/REST bar deduplication.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import threading
import time
from typing import Any
from unittest.mock import MagicMock

from backtest.brokers.mstock import MStockBroker
from backtest.data.mstock_ws_wrapper import MStockTickBus, MStockWebSocketWrapper
from backtest.forward.feed_registry import ChainBus, MStockBarFeed
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    CachedQuoteProvider,
    HotStrikeRing,
    LiveChainProvider,
    LiveQuoteProvider,
)


class _FakeMTicker:
    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self) -> None:
        self.on_ticks: Any = None
        self.on_connect: Any = None
        self.on_close: Any = None
        self.subscribe_calls: list[list[int]] = []
        self.unsubscribe_calls: list[list[int]] = []
        self.mode_calls: list[tuple[str, list[int]]] = []
        self.closed = False

    def subscribe(self, tokens: list[int]) -> bool:
        self.subscribe_calls.append(list(tokens))
        return True

    def unsubscribe(self, tokens: list[int]) -> bool:
        self.unsubscribe_calls.append(list(tokens))
        return True

    def set_mode(self, mode: str, tokens: list[int]) -> bool:
        self.mode_calls.append((mode, list(tokens)))
        return True

    def close(self) -> None:
        self.closed = True


def _full_tick(
    token: int,
    ltp: float,
    bid: float,
    ask: float,
    *,
    exch_dt: datetime | None = None,
) -> dict[str, Any]:
    return {
        "instrument_token": token,
        "last_price": ltp,
        "volume_traded": 1200,
        "open_interest": 45000,
        "ohlc": {"open": ltp, "high": ltp + 2, "low": ltp - 2, "close": ltp},
        "depth": {
            "bid": [{"price": bid, "quantity": 75, "orders": 2}],
            "ask": [{"price": ask, "quantity": 75, "orders": 3}],
        },
        "exchange_timestamp": exch_dt or datetime.now(),
        "mode": "full",
    }


def _build_chain(
    underlying: str,
    center: int,
    step: int,
    strikes_each_side: int = 12,
    expiry: date | None = None,
) -> list[OptionContract]:
    exp = expiry or (date.today() + timedelta(days=7))
    lot = 75 if underlying == "NIFTY" else 30
    contracts: list[OptionContract] = []
    next_tok = 50000 if underlying == "NIFTY" else 70000
    for offset in range(-strikes_each_side, strikes_each_side + 1):
        strike = center + offset * step
        for opt_type in (OptionType.CE, OptionType.PE):
            next_tok += 1
            suffix = "CE" if opt_type is OptionType.CE else "PE"
            contracts.append(
                OptionContract(
                    instrument_token=str(next_tok),
                    trading_symbol=f"{underlying}26OCT{strike}{suffix}",
                    underlying=underlying,
                    expiry=exp,
                    strike=Decimal(strike),
                    option_type=opt_type,
                    lot_size=lot,
                    metadata={},
                )
            )
    return contracts


# ---------------------------------------------------------------------------
# 1. MStockTickBus & Coalesced Push Dispatcher
# ---------------------------------------------------------------------------


class TestMStockTickBus:
    def test_normalize_tick_stamps_recv_mono_and_ws_source(self) -> None:
        raw = _full_tick(51338, 125.0, 124.5, 125.5)
        norm = MStockWebSocketWrapper._normalize_tick(raw)
        assert norm["ltp"] == 125.0
        assert norm["bid"] == 124.5
        assert norm["ask"] == 125.5
        assert norm["oi"] == 45000.0
        assert norm["source"] == "ws"
        assert isinstance(norm["recv_mono"], float) and norm["recv_mono"] > 0
        assert isinstance(norm["recv_utc"], str) and "T" in norm["recv_utc"]
        assert MStockTickBus is MStockWebSocketWrapper

    def test_coalesced_dispatcher_batches_burst_off_caller_thread_and_isolates_errors(
        self,
    ) -> None:
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=40)
        try:
            ticker.on_connect(ticker, {"status": "ok"})
            assert wrapper.is_connected() is True

            received_batches: list[dict[str, dict]] = []
            callback_threads: list[str] = []
            got_batch = threading.Event()

            def _bad_listener(batch: dict[str, dict]) -> None:
                raise RuntimeError("boom inside downstream listener")

            def _good_listener(batch: dict[str, dict]) -> None:
                callback_threads.append(threading.current_thread().name)
                received_batches.append(batch)
                got_batch.set()

            wrapper.register_listener(_bad_listener)
            wrapper.register_listener(_good_listener)

            caller_thread = threading.current_thread().name
            # Fire 5 rapid frames within a few milliseconds — must coalesce into 1 batch
            for px in (101.0, 102.0, 103.0, 104.0, 105.5):
                ticker.on_ticks(ticker, [_full_tick(51338, px, px - 0.5, px + 0.5)])
            ticker.on_ticks(ticker, [_full_tick(26000, 25012.0, 0.0, 0.0)])

            # Synchronous read is already at latest value (0 ms lag)
            sync_q = wrapper.get_quote("51338")
            assert sync_q is not None and sync_q["ltp"] == 105.5

            assert got_batch.wait(timeout=2.0)
            assert len(received_batches) == 1
            assert received_batches[0]["51338"]["ltp"] == 105.5
            assert received_batches[0]["26000"]["ltp"] == 25012.0
            assert callback_threads[0] != caller_thread
            assert callback_threads[0] == "mstock-tick-bus"
        finally:
            wrapper.close()


# ---------------------------------------------------------------------------
# 2. WS Freshness Fix (Finding F5) + Non-Blocking Batch Subscribe + Pins
# ---------------------------------------------------------------------------


class TestMStockWsFreshnessAndSubscriptions:
    @staticmethod
    def _broker_with_ws() -> tuple[MStockBroker, _FakeMTicker, MStockWebSocketWrapper]:
        broker = MStockBroker()
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=20)
        ticker.on_connect(ticker, {})
        broker._ws_feed = wrapper
        broker._subscribe_default_indices()
        return broker, ticker, wrapper

    def test_quiet_otm_strike_stays_valid_while_socket_receives_index_ticks(self) -> None:
        broker, ticker, wrapper = self._broker_with_ws()
        try:
            broker.subscribe_tokens_ws([59999], mode="full")
            now_mono = time.monotonic()
            # OTM strangle leg last printed 35s ago (> max_age_seconds=10s)
            with wrapper._lock:
                wrapper._quotes["59999"] = MStockWebSocketWrapper._normalize_tick(
                    _full_tick(59999, 18.5, 18.0, 19.0),
                    recv_mono=now_mono - 35.0,
                )
                # Index heartbeat arrived 0.4s ago -> socket is demonstrably live!
                wrapper._quotes["26000"] = MStockWebSocketWrapper._normalize_tick(
                    _full_tick(26000, 25005.0, 0.0, 0.0),
                    recv_mono=now_mono - 0.4,
                )
                wrapper._last_any_tick_mono = now_mono - 0.4

            q = broker.get_ws_quote(59999, max_age_seconds=10.0)
            assert q != {}, "quiet OTM leg on a live socket must stay valid"
            assert q["bid"] == 18.0 and q["ask"] == 19.0
            assert q["source"] == "ws"
        finally:
            wrapper.close()

    def test_post_close_frozen_ticks_rejected_fixing_finding_f5(self) -> None:
        """At 15:31 IST the socket stays open, but no index or option frame has
        arrived for >10s — get_ws_quote must reject the frozen 15:29:59 tick."""
        broker, ticker, wrapper = self._broker_with_ws()
        try:
            broker.subscribe_tokens_ws([51338], mode="full")
            frozen_mono = time.monotonic() - 18.0
            with wrapper._lock:
                wrapper._quotes["51338"] = MStockWebSocketWrapper._normalize_tick(
                    _full_tick(
                        51338,
                        140.0,
                        139.5,
                        140.5,
                        exch_dt=datetime.now() - timedelta(seconds=18),
                    ),
                    recv_mono=frozen_mono,
                )
                wrapper._last_any_tick_mono = frozen_mono

            assert broker.get_ws_quote(51338, max_age_seconds=10.0) == {}
        finally:
            wrapper.close()

    def test_disconnected_socket_and_legacy_datetime_timestamps_fail_closed(self) -> None:
        broker, ticker, wrapper = self._broker_with_ws()
        try:
            ticker.on_ticks(ticker, [_full_tick(51338, 120.0, 119.5, 120.5)])
            assert broker.get_ws_quote(51338, max_age_seconds=10.0)["ltp"] == 120.0

            # Socket closes -> must reject immediately instead of serving frozen cache
            ticker.on_close(ticker, 1006, "network drop")
            assert broker.get_ws_quote(51338, max_age_seconds=10.0) == {}

            # Reconnect and inject legacy quote with stale datetime (no recv_mono)
            ticker.on_connect(ticker, {})
            with wrapper._lock:
                wrapper._quotes["51338"] = {
                    "ltp": 120.0,
                    "bid": 119.5,
                    "ask": 120.5,
                    "timestamp": datetime.now() - timedelta(seconds=25),
                }
            assert broker.get_ws_quote(51338, max_age_seconds=10.0) == {}
        finally:
            wrapper.close()

    def test_cold_try_ws_quote_has_zero_blocking_sleep_and_respects_pins(self) -> None:
        broker, ticker, wrapper = self._broker_with_ws()
        try:
            t0 = time.monotonic()
            miss = broker._try_ws_quote("61234")
            elapsed = time.monotonic() - t0
            assert miss == {}
            assert elapsed < 0.1, "cold WS lookup must not sleep 500ms"
            assert 61234 in broker._ws_subscribed_tokens

            # Pinning + default index protection
            broker.pin_ws_tokens([61234])
            removed = broker.unsubscribe_tokens_ws([26000, 61234])
            assert removed == [], "indices and pinned tokens must never be unsubscribed"
            assert 26000 in broker._ws_subscribed_tokens
            assert 61234 in broker._ws_subscribed_tokens

            broker.unpin_ws_tokens([61234])
            removed_after = broker.unsubscribe_tokens_ws([61234])
            assert removed_after == [61234]
            assert 61234 not in broker._ws_subscribed_tokens
        finally:
            wrapper.close()


# ---------------------------------------------------------------------------
# 3. Zero-TTL Live WS Quotes & CachedQuoteProvider Numeric Forwarding
# ---------------------------------------------------------------------------


class TestZeroTtlWsQuotes:
    def test_live_and_cached_providers_bypass_ttl_for_ws_and_upgrade_http_fallback(
        self,
    ) -> None:
        broker = MStockBroker()
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=20)
        ticker.on_connect(ticker, {})
        broker._ws_feed = wrapper
        broker.subscribe_tokens_ws([51338], mode="full")

        http_calls = 0

        def _fake_http(symbol_or_contract: Any) -> dict[str, Any]:
            nonlocal http_calls
            http_calls += 1
            return {"ltp": 110.0}

        broker._http_option_quote = _fake_http  # type: ignore[method-assign]

        contract = OptionContract(
            instrument_token="51338",
            trading_symbol="NIFTY26OCT25000CE",
            underlying="NIFTY",
            expiry=date.today() + timedelta(days=5),
            strike=Decimal(25000),
            option_type=OptionType.CE,
            lot_size=75,
            metadata={},
        )
        live = LiveQuoteProvider(broker, cache_ttl_seconds=60)
        cached = CachedQuoteProvider(live, ttl=60)
        cached.register_contract(contract)

        try:
            # 1. Cold WS: falls back to HTTP once and caches HTTP row for rate-limit safety
            q_http_1 = cached.get_quote("51338")
            q_http_2 = cached.get_quote("51338")
            assert q_http_1["ltp"] == 110.0 and q_http_1["bid"] == 0.0
            assert q_http_2["ltp"] == 110.0
            assert http_calls == 1, "HTTP fallback must still respect rate-limit TTL"

            # 2. First WS depth tick arrives 50ms later -> immediate upgrade (no 60s wait!)
            ticker.on_ticks(ticker, [_full_tick(51338, 122.0, 121.5, 122.5)])
            q_ws_1 = cached.get_quote("51338")
            assert q_ws_1["source"] == "ws"
            assert q_ws_1["ltp"] == 122.0 and q_ws_1["bid"] == 121.5 and q_ws_1["ask"] == 122.5

            # 3. Sub-second tick update -> reflected immediately (0-TTL on WS quotes!)
            ticker.on_ticks(ticker, [_full_tick(51338, 134.0, 133.5, 134.5)])
            q_ws_2 = cached.get_quote("51338")
            assert q_ws_2["ltp"] == 134.0 and q_ws_2["bid"] == 133.5
            assert http_calls == 1
        finally:
            wrapper.close()

    def test_cached_quote_provider_skips_caching_zero_or_error_rows(self) -> None:
        inner = MagicMock()
        inner.get_quote.side_effect = [
            {"ltp": 0.0, "error": "transient timeout"},
            {"ltp": 145.0, "bid": 144.5, "ask": 145.5},
            {"ltp": 149.0, "bid": 148.5, "ask": 149.5},
        ]
        cached = CachedQuoteProvider(inner, ttl=60)

        first = cached.get_quote("NIFTY26OCT25000CE")
        assert first["ltp"] == 0.0
        second = cached.get_quote("NIFTY26OCT25000CE")
        assert second["ltp"] == 145.0, "zero/error quote must never stick in TTL cache"
        third = cached.get_quote("NIFTY26OCT25000CE")
        assert third["ltp"] == 145.0
        assert inner.get_quote.call_count == 2


# ---------------------------------------------------------------------------
# 4. HotStrikeRing + ChainBus + MStockBarFeed.ingest_ws_bar
# ---------------------------------------------------------------------------


class TestHotStrikeRingAndBus:
    def test_hot_strike_ring_subscribes_26_tokens_hysteresis_wings_and_pins(self) -> None:
        broker = MStockBroker()
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=20)
        ticker.on_connect(ticker, {})
        broker._ws_feed = wrapper

        nifty_chain = _build_chain("NIFTY", center=25000, step=50, strikes_each_side=15)
        ring = HotStrikeRing(broker)
        try:
            # 1. Center at 25,010 -> ATM=25,000 -> 13 strikes * (CE+PE) = 26 tokens in 1 batch!
            res1 = ring.recenter("NIFTY", 25010.0, nifty_chain)
            assert len(res1["subscribed"]) == 26
            assert res1["unsubscribed"] == []
            assert len(ticker.subscribe_calls) == 1

            # 2. Sub-step wiggle (25,020 -> ATM still 25,000) -> 0 WS traffic
            res_wiggle = ring.recenter("NIFTY", 25020.0, nifty_chain)
            assert res_wiggle == {"subscribed": [], "unsubscribed": []}

            # 3. Pin the lowest strike CE/PE (24,700 = ATM - 6*50) as an open position
            strike_24700_tokens = [
                int(c.instrument_token)
                for c in nifty_chain
                if int(c.strike) == 24700
            ]
            assert len(strike_24700_tokens) == 2
            ring.pin_tokens([strike_24700_tokens[0]])

            # 4. Shift +1 step (25,050): subscribes 25,350 CE/PE (2 tokens),
            #    24,700 is at distance 7*50 <= 8*50 hysteresis -> 0 unsubscribes!
            res_step1 = ring.recenter("NIFTY", 25050.0, nifty_chain)
            assert len(res_step1["subscribed"]) == 2
            assert res_step1["unsubscribed"] == []

            # 5. Shift to 25,200 (24,700 is now 10 steps away > 8*50 hysteresis):
            #    unpinned 24,700 leg is evicted, but PINNED 24,700 leg stays subscribed!
            res_jump = ring.recenter("NIFTY", 25200.0, nifty_chain)
            assert strike_24700_tokens[1] in res_jump["unsubscribed"]
            assert strike_24700_tokens[0] not in res_jump["unsubscribed"]
            assert strike_24700_tokens[0] in broker._ws_subscribed_tokens

            # 6. Register 2% strangle wings around 25,200 (~25,704 CE / ~24,696 PE)
            ring.register_wing_distance("NIFTY", 2.0)
            res_wing = ring.recenter("NIFTY", 25200.0, nifty_chain)
            assert len(res_wing["subscribed"]) >= 2
        finally:
            wrapper.close()

    def test_chain_bus_warms_banknifty_ring_and_releases_on_zero_subscribers(
        self,
    ) -> None:
        broker = MStockBroker()
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=20)
        ticker.on_connect(ticker, {})
        broker._ws_feed = wrapper
        # Seed live BANKNIFTY index spot (token 26009) on WS cache
        ticker.on_ticks(ticker, [_full_tick(26009, 52020.0, 0.0, 0.0)])

        bn_chain = _build_chain("BANKNIFTY", center=52000, step=100, strikes_each_side=10)
        broker.get_option_chain = MagicMock(return_value=bn_chain)  # type: ignore[method-assign]

        bus = ChainBus()
        try:
            feed = bus.acquire("BANKNIFTY", source="mstock", broker=broker, warm_ring=False)
            assert isinstance(feed, BidAskQuoteProvider)
            assert isinstance(feed.inner, LiveChainProvider)
            res = feed.inner.warm_chain_and_ring("BANKNIFTY")
            assert len(res["subscribed"]) == 26
            assert feed.inner.hot_ring.center_strike("BANKNIFTY") == 52000.0

            # Lookup for ATM contract hits WS cache with 0 HTTP calls once tick arrives
            atm_ce = next(
                c
                for c in bn_chain
                if int(c.strike) == 52000 and c.option_type is OptionType.CE
            )
            ticker.on_ticks(
                ticker,
                [_full_tick(int(atm_ce.instrument_token), 410.0, 409.0, 411.0)],
            )
            exec_q = feed.execution_quote(atm_ce.instrument_token, "BUY")
            assert exec_q["ltp"] == 411.0
            assert feed.inner.price_contract(atm_ce) == 410.0

            # Releasing last subscriber unsubscribes all 26 non-pinned BANKNIFTY option tokens
            assert bus.release("BANKNIFTY", source="mstock") == 0
            assert feed.inner.hot_ring.subscribed_tokens("BANKNIFTY") == set()
        finally:
            wrapper.close()

    def test_mstock_bar_feed_ingest_ws_bar_deduplicates_against_rest_poll(self) -> None:
        delivered: list[tuple[str, dict[str, Any]]] = []
        fake_client = MagicMock()
        fake_client.emits_minute_bars = True
        fake_client.latest_bar.return_value = {
            "ts": "2026-10-23 09:16:00",
            "open": 25000.0,
            "high": 25020.0,
            "low": 24995.0,
            "close": 25015.0,
            "volume": 1000,
        }

        feed = MStockBarFeed(feed_client=fake_client, poll_interval_s=60.0)
        feed._market_open = lambda: True  # type: ignore[method-assign]
        feed.on_bar = lambda s, b: delivered.append((s, b))
        feed.add_symbols(["NIFTY"])

        # 1. Push minute bar from WS -> delivered immediately
        ok = feed.ingest_ws_bar(
            "NIFTY",
            {
                "ts": "2026-10-23 09:16:00",
                "open": 25000.0,
                "high": 25020.0,
                "low": 24995.0,
                "close": 25015.0,
                "volume": 1000,
            },
        )
        assert ok is True
        assert len(delivered) == 1
        assert delivered[0][1]["_source"] == "mstock"
        assert delivered[0][1]["_timeframe"] == "1min"

        # 2. Duplicate WS push or 60s REST poll for the same 09:16 bar -> skipped!
        assert (
            feed.ingest_ws_bar(
                "NIFTY",
                {"ts": "2026-10-23 09:16:00", "close": 25015.0},
            )
            is False
        )
        assert feed._poll_once() == 0
        assert fake_client.latest_bar.call_count == 1
        assert len(delivered) == 1


# ---------------------------------------------------------------------------
# 5. Live-Path Review Hardening (TOTP Re-Auth, Pin Drift, Fail-Closed & Cap)
# ---------------------------------------------------------------------------


class _ConnectableFakeMTicker(_FakeMTicker):
    """Simulates real MTicker handshake order: connect -> on_connect -> on_open."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.events: list[str] = []

    def send_login_after_connect(self) -> None:
        self.events.append("LOGIN")

    def subscribe(self, tokens: list[int]) -> bool:
        self.events.append(f"SUB:{len(tokens)}")
        return super().subscribe(tokens)

    def set_mode(self, mode: str, tokens: list[int]) -> bool:
        self.events.append(f"MODE:{mode}:{len(tokens)}")
        return super().set_mode(mode, tokens)

    def connect(self, threaded: bool = True) -> None:
        if self.on_connect:
            self.on_connect(self, {"status": "101"})
        if getattr(self, "on_open", None):
            self.on_open(self)


class TestPhase2LiveReviewHardening:
    def test_totp_reauth_init_websocket_feed_resubscribes_all_26_ring_tokens_after_login(
        self,
        monkeypatch: Any,
    ) -> None:
        """Defect 1 regression: mid-session TOTP re-auth must re-subscribe all
        26 non-pinned HotStrikeRing tokens (after LOGIN), never leaving them cold."""
        created_tickers: list[_ConnectableFakeMTicker] = []

        def _factory(**kwargs: Any) -> _ConnectableFakeMTicker:
            t = _ConnectableFakeMTicker(**kwargs)
            created_tickers.append(t)
            return t

        import backtest.data.mticker as mticker_mod

        monkeypatch.setattr(mticker_mod, "MTicker", _factory)
        monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")

        broker = MStockBroker()
        nifty_chain = _build_chain("NIFTY", center=25000, step=50, strikes_each_side=12)
        ring = HotStrikeRing(broker)
        try:
            # Initial login #1
            broker._init_websocket_feed("access-token-1")
            assert len(created_tickers) == 1
            res1 = ring.recenter("NIFTY", 25010.0, nifty_chain)
            assert len(res1["subscribed"]) == 26
            expected_ring = set(res1["subscribed"])
            assert expected_ring.issubset(broker._ws_subscribed_tokens)

            # Mid-session TOTP re-auth #2 (0 pinned positions!)
            broker._init_websocket_feed("access-token-2")
            assert len(created_tickers) == 2
            assert created_tickers[0].closed is True

            t2 = created_tickers[1]
            assert t2.events[0] == "LOGIN", "LOGIN must precede any resubscribe on re-auth"
            assert expected_ring.issubset(broker._ws_subscribed_tokens)
            assert broker.DEFAULT_INDEX_TOKENS.issubset(broker._ws_subscribed_tokens)
            resubscribed_flat = {tok for call in t2.subscribe_calls for tok in call}
            assert expected_ring.issubset(resubscribed_flat)

            # Belt & suspenders: if socket state ever loses a token without ATM moving,
            # recenter() detects the missing broker subscription instead of skipping.
            dropped_tok = next(iter(expected_ring))
            broker._ws_subscribed_tokens.discard(dropped_tok)
            repair = ring.recenter("NIFTY", 25012.0, nifty_chain)
            assert repair["subscribed"] == [dropped_tok]
            assert dropped_tok in broker._ws_subscribed_tokens
        finally:
            broker.logout()
            assert broker._ws_feed is None
            assert broker._ws_subscribed_tokens == set()

    def test_double_pin_double_unpin_keeps_broker_refcount_zero_and_evicts_drifted_ootm(
        self,
    ) -> None:
        """Defect 2 regression: pin twice / unpin twice must leave _ws_token_pins
        at 0 (no drift) and immediately unsubscribe drifted OOTM ring legs."""
        broker = MStockBroker()
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=20)
        ticker.on_connect(ticker, {})
        broker._ws_feed = wrapper

        nifty_chain = _build_chain("NIFTY", center=25000, step=50, strikes_each_side=15)
        ring = HotStrikeRing(broker)
        try:
            ring.recenter("NIFTY", 25000.0, nifty_chain)
            tok_24700_ce = next(
                int(c.instrument_token)
                for c in nifty_chain
                if int(c.strike) == 24700 and c.option_type is OptionType.CE
            )

            # Pin twice (e.g., two runners holding the same strike)
            ring.pin_tokens([tok_24700_ce])
            ring.pin_tokens([tok_24700_ce])
            assert broker._ws_token_pins[tok_24700_ce] == 2

            # Spot rallies 10 steps to 25,500 (24,700 is 16 steps away > ATM +/- 8)
            ring.recenter("NIFTY", 25500.0, nifty_chain)
            assert tok_24700_ce in broker._ws_subscribed_tokens

            # First unpin (2 -> 1): broker pin MUST decrement to 1, leg stays subscribed
            z1 = ring.unpin_tokens([tok_24700_ce])
            assert z1 == []
            assert broker._ws_token_pins[tok_24700_ce] == 1
            assert broker.unsubscribe_tokens_ws([tok_24700_ce]) == []

            # Second unpin (1 -> 0): broker pin hits 0 AND drifted OOTM leg is evicted!
            z2 = ring.unpin_tokens([tok_24700_ce])
            assert z2 == [tok_24700_ce]
            assert broker._ws_token_pins.get(tok_24700_ce, 0) == 0
            assert tok_24700_ce not in broker._ws_subscribed_tokens
            assert tok_24700_ce not in ring.subscribed_tokens("NIFTY")
        finally:
            wrapper.close()

    def test_missing_both_recv_mono_and_timestamp_fails_closed_and_liveness_matches_max_age(
        self,
    ) -> None:
        """Defect 3 regression: quote lacking both recv_mono and timestamp must
        fail closed ({}), and default socket liveness matches max_age_seconds."""
        broker = MStockBroker()
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=20)
        ticker.on_connect(ticker, {})
        broker._ws_feed = wrapper
        broker.subscribe_tokens_ws([51338, 26000], mode="full")
        try:
            # 1. Row with neither recv_mono nor timestamp -> fail closed!
            with wrapper._lock:
                wrapper._quotes["51338"] = {"ltp": 120.0, "bid": 119.5, "ask": 120.5}
            assert broker.get_ws_quote(51338, max_age_seconds=10.0) == {}

            # 2. Midday 7.5s lull (inside max_age_seconds=10.0): quiet OTM leg stays valid!
            now_m = time.monotonic()
            with wrapper._lock:
                wrapper._quotes["51338"] = MStockWebSocketWrapper._normalize_tick(
                    _full_tick(51338, 22.0, 21.5, 22.5),
                    recv_mono=now_m - 30.0,
                )
                wrapper._last_any_tick_mono = now_m - 7.5
            q_midday = broker.get_ws_quote(51338, max_age_seconds=10.0)
            assert q_midday != {} and q_midday["bid"] == 21.5

            # 3. Silence exceeds max_age_seconds (11.5s > 10.0s) -> rejected!
            with wrapper._lock:
                wrapper._last_any_tick_mono = now_m - 11.5
            assert broker.get_ws_quote(51338, max_age_seconds=10.0) == {}
        finally:
            wrapper.close()

    def test_hot_strike_ring_respects_max_ring_tokens_cap_and_ring_mode_override(
        self,
    ) -> None:
        """Operational safety: ring_mode='quote' + max_ring_tokens cap bounds load
        while pinned legs still upgrade to mode='full'."""
        broker = MStockBroker()
        ticker = _FakeMTicker()
        wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=20)
        ticker.on_connect(ticker, {})
        broker._ws_feed = wrapper

        nifty_chain = _build_chain("NIFTY", center=25000, step=50, strikes_each_side=10)
        bn_chain = _build_chain("BANKNIFTY", center=52000, step=100, strikes_each_side=10)
        ring = HotStrikeRing(broker, ring_mode="quote", max_ring_tokens=30)
        try:
            r1 = ring.recenter("NIFTY", 25000.0, nifty_chain)
            r2 = ring.recenter("BANKNIFTY", 52000.0, bn_chain)
            assert len(r1["subscribed"]) == 26
            assert len(r2["subscribed"]) == 4  # capped at 30 total across underlyings!
            assert len(ring.subscribed_tokens()) == 30
            assert all(m == "quote" for m, _ in ticker.mode_calls)

            # Pinning an active position leg upgrades that leg to full depth!
            held_tok = r1["subscribed"][0]
            ring.pin_tokens([held_tok])
            assert ticker.mode_calls[-1] == ("full", [held_tok])
        finally:
            wrapper.close()
