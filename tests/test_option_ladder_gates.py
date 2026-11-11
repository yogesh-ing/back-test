"""Ladder perimeter + quote-sanity gates (docs/OPTION-DEPTH-GARBAGE-ROOT-CAUSE-2026-11-03.md).

The 2026-11-03 short straddle "earned" +₹71,639 by buying legs at ₹0.05 and ₹4 on
contracts worth ~₹340. Three independent holes let that through, and this suite
holds each one shut:

1. ``MStockWebSocketWrapper`` — level hygiene and last-valid-write-wins, so a
   packet with a correct header and a blank ladder can neither publish a book nor
   destroy the last real one.
2. ``MStockBroker.get_ws_quote`` — the ladder has its own clock and its own TTL,
   a token must actually be subscribed for the socket-liveness bypass to apply,
   and unsubscribe/reconnect purges the cached row (cold start).
3. ``BidAskQuoteProvider`` — ``price <= 0`` was the whole test; now a side price
   must also sit near its own LTP and above the contract's intrinsic value.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from backtest.brokers.mstock import MStockBroker
from backtest.data.mstock_ws_wrapper import MStockWebSocketWrapper
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    LiveChainProvider,
    NoExecutableQuoteError,
    UntrustedOptionQuoteError,
)


class _FakeMTicker:
    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self) -> None:
        self.on_ticks: Any = None
        self.on_connect: Any = None
        self.on_close: Any = None
        self.closed = False

    def subscribe(self, tokens: list[int]) -> bool:
        return True

    def unsubscribe(self, tokens: list[int]) -> bool:
        return True

    def set_mode(self, mode: str, tokens: list[int]) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _level(price: float, qty: int = 75) -> dict[str, Any]:
    return {"price": price, "quantity": qty, "orders": 1}


def _tick(
    token: int,
    ltp: float,
    bids: list[dict[str, Any]],
    asks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "instrument_token": token,
        "last_price": ltp,
        "volume_traded": 1000,
        "open_interest": 45000,
        "ohlc": {},
        "depth": {"bid": bids, "ask": asks},
        "exchange_timestamp": "12:00:00",
        "mode": "full",
    }


def _broker_with_ws() -> tuple[MStockBroker, _FakeMTicker, MStockWebSocketWrapper]:
    broker = MStockBroker()
    ticker = _FakeMTicker()
    wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=0)
    ticker.on_connect(ticker, {})
    broker._ws_feed = wrapper
    broker._subscribe_default_indices()
    return broker, ticker, wrapper


# ---------------------------------------------------------------------------
# 1. Wrapper: level hygiene (§8a) and last-valid-write-wins (§5.1)
# ---------------------------------------------------------------------------


class TestLadderSanitizing:
    def test_placeholder_zero_quantity_level_is_not_depth(self) -> None:
        """The ₹0.05 leg: a level with a price nobody published is not a quote."""
        depth, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {"bid": [_level(0.05, qty=0)], "ask": [_level(0.10, qty=0)]}
        )
        assert status == "blank"
        assert bid == 0.0 and ask == 0.0
        assert depth["bid"] == [] and depth["ask"] == []

    def test_empty_ladder_is_blank_not_ok(self) -> None:
        depth, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {"bid": [], "ask": []}
        )
        assert (status, bid, ask) == ("blank", 0.0, 0.0)
        assert depth == {"bid": [], "ask": []}

    def test_missing_depth_is_none_status(self) -> None:
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(None)
        assert (status, bid, ask) == ("none", 0.0, 0.0)

    def test_one_sided_book_keeps_the_side_that_exists(self) -> None:
        """"no ask to BUY" is a real venue state — keep the bid, label the row."""
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {"bid": [_level(180.0)], "ask": []}
        )
        assert status == "one_sided"
        assert bid == 180.0 and ask == 0.0

    def test_crossed_ladder_quotes_neither_side(self) -> None:
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {"bid": [_level(190.0)], "ask": [_level(189.0)]}
        )
        assert status == "crossed"
        assert bid == 0.0 and ask == 0.0

    def test_first_surviving_level_prices_the_side_never_a_better_one(self) -> None:
        """Skip dead levels, but do not invent the venue's best price for it."""
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {
                "bid": [_level(180.0), _level(179.0)],
                "ask": [_level(190.0), _level(191.0)],
            }
        )
        assert status == "ok"
        assert (bid, ask) == (180.0, 190.0)

    def test_a_ladder_that_folds_back_towards_the_touch_prices_nothing(self) -> None:
        """Level 0 is the best price only on a ladder the venue ordered.

        This fixture used to assert ``ok`` with ``(180.0, 190.0)`` — the bids
        stepping UP and the asks stepping DOWN, which no book does. The 2026-11-09
        capture proved mStock really sends these (10 of 87,819 packets, none of
        them off Upstox), and pricing such a packet published a two-sided book
        neither side of it was.
        """
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {
                "bid": [_level(180.0), _level(185.0)],
                "ask": [_level(190.0), _level(188.0)],
            }
        )
        assert status == "unsorted"
        assert bid == 0.0 and ask == 0.0

    def test_the_2026_11_09_packet_that_priced_itself_five_times_over(self) -> None:
        """The worst folded ladder on the tape, replayed level for level.

        Token 64442 at 09:34:48 UTC: three bids at ₹330 then two at ₹1,640 with an
        unchanged header and a ₹329.45 last trade, and the row was labelled ``ok``.
        """
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {
                "bid": [
                    _level(330.05), _level(330.00), _level(329.95),
                    _level(1640.62), _level(1640.57),
                ],
                "ask": [
                    _level(1642.42), _level(1642.47), _level(1642.52),
                    _level(1642.67), _level(1642.72),
                ],
            }
        )
        assert status == "unsorted"
        assert bid == 0.0 and ask == 0.0

    def test_equal_adjacent_levels_are_a_book_not_a_fold(self) -> None:
        """Zero false positives on 48,008 clean Upstox ladders depended on this."""
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {"bid": [_level(180.0), _level(180.0)], "ask": [_level(183.0), _level(183.0)]}
        )
        assert status == "ok"
        assert (bid, ask) == (180.0, 183.0)

    def test_a_folded_single_side_is_refused_too(self) -> None:
        """``one_sided`` keeps a real side — unless that side is the broken one."""
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {"bid": [_level(180.0), _level(185.0)], "ask": []}
        )
        assert status == "unsorted"
        assert bid == 0.0 and ask == 0.0

    def test_dead_levels_are_trimmed_before_the_order_is_judged(self) -> None:
        """The ₹0.05-with-no-quantity hole and the order rule must not fight."""
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {
                "bid": [_level(181.0), _level(184.0, qty=0), _level(180.0)],
                "ask": [_level(183.0), _level(182.0, qty=0), _level(185.0)],
            }
        )
        assert status == "ok"
        assert (bid, ask) == (181.0, 183.0)

    def test_zero_price_level_is_skipped_and_the_next_real_level_wins(self) -> None:
        _d, bid, ask, status = MStockWebSocketWrapper._sanitize_depth(
            {"bid": [_level(0.0), _level(181.0)], "ask": [_level(0.0), _level(183.0)]}
        )
        assert status == "ok"
        assert (bid, ask) == (181.0, 183.0)

    def test_honest_two_sided_book_is_ok_and_stamped(self) -> None:
        row = MStockWebSocketWrapper._normalize_tick(
            _tick(7777, 182.0, [_level(181.0)], [_level(183.0)])
        )
        assert row["ladder_status"] == "ok"
        assert row["ladder_mono"] == pytest.approx(row["recv_mono"])
        assert row["ltp_mono"] == pytest.approx(row["recv_mono"])
        assert (row["bid"], row["ask"]) == (181.0, 183.0)

    def test_blank_ladder_row_carries_no_side_price(self) -> None:
        row = MStockWebSocketWrapper._normalize_tick(
            _tick(7777, 182.0, [], [])
        )
        assert row["ladder_status"] == "blank"
        assert "ladder_mono" not in row
        assert row["bid"] == 0.0 and row["ask"] == 0.0


class TestLastValidWriteWins:
    def test_blank_frame_retains_the_last_honest_ladder(self) -> None:
        broker, ticker, wrapper = _broker_with_ws()
        try:
            ticker.on_ticks(
                ticker,
                [_tick(63000, 340.0, [_level(339.5)], [_level(341.0)])],
            )
            honest = wrapper.get_quote("63000")
            assert honest["ladder_status"] == "ok"

            # Same header, correct LTP, no book at all — what mStock sent on 03-Nov.
            time.sleep(0.01)
            ticker.on_ticks(ticker, [_tick(63000, 352.0, [], [])])

            merged = wrapper.get_quote("63000")
            assert merged["ltp"] == 352.0, "the frame's own price still updates"
            assert (merged["bid"], merged["ask"]) == (339.5, 341.0)
            assert merged["ladder_status"] == "kept:blank"
            assert merged["ladder_mono"] == pytest.approx(honest["ladder_mono"])
            assert wrapper.blank_ladder_count("63000") == 1
        finally:
            wrapper.close()

    def test_nothing_is_invented_when_the_first_frame_is_blank(self) -> None:
        broker, ticker, wrapper = _broker_with_ws()
        try:
            ticker.on_ticks(ticker, [_tick(63001, 340.0, [], [])])
            row = wrapper.get_quote("63001")
            assert row["ladder_status"] == "blank"
            assert row["bid"] == 0.0 and row["ask"] == 0.0
        finally:
            wrapper.close()

    def test_folded_frame_keeps_the_last_ordered_book(self) -> None:
        """A packet whose levels fold is retained like a blank one, not published.

        The retained row is labelled ``kept:unsorted`` so the seam, the log and the
        capture all say the book on offer is an older one — never this packet's.
        """
        broker, ticker, wrapper = _broker_with_ws()
        try:
            ticker.on_ticks(
                ticker, [_tick(63004, 340.0, [_level(339.5)], [_level(341.0)])]
            )
            honest = wrapper.get_quote("63004")
            ticker.on_ticks(
                ticker,
                [
                    _tick(
                        63004,
                        344.0,
                        [_level(339.5), _level(1640.62)],
                        [_level(341.0), _level(340.5)],
                    )
                ],
            )
            merged = wrapper.get_quote("63004")
            assert merged["ltp"] == 344.0
            assert (merged["bid"], merged["ask"]) == (339.5, 341.0)
            assert merged["ladder_status"] == "kept:unsorted"
            assert merged["ladder_mono"] == pytest.approx(honest["ladder_mono"])
        finally:
            wrapper.close()

    def test_reconnect_starts_a_new_cache_generation(self) -> None:
        """A row cached on the dead socket must not become servable on the new one."""
        broker, ticker, wrapper = _broker_with_ws()
        try:
            ticker.on_ticks(ticker, [_tick(63002, 340.0, [_level(339.5)], [_level(341.0)])])
            assert wrapper.get_quote("63002") is not None
            ticker.on_close(ticker, 1006, "network drop")
            ticker.on_connect(ticker, {})
            assert wrapper.get_quote("63002") is None
            assert wrapper.last_any_tick_mono() == 0.0
        finally:
            wrapper.close()


# ---------------------------------------------------------------------------
# 2. Serve gate: two clocks, subscription truth, cold start
# ---------------------------------------------------------------------------


class TestServeGate:
    def _inject(self, wrapper, token: str, age_s: float, ladder_age_s: float | None = None):
        now = time.monotonic()
        row = MStockWebSocketWrapper._normalize_tick(
            _tick(int(token), 340.0, [_level(339.5)], [_level(341.0)]),
            recv_mono=now - age_s,
        )
        row["ltp_mono"] = now - age_s
        if ladder_age_s is not None:
            row["ladder_mono"] = now - ladder_age_s
        with wrapper._lock:
            wrapper._quotes[token] = row
            wrapper._last_any_tick_mono = now - 0.2
        return row

    def test_quiet_leg_on_a_live_subscribed_socket_still_serves_depth(self) -> None:
        broker, _t, wrapper = _broker_with_ws()
        try:
            broker.subscribe_tokens_ws([63010], mode="full")
            self._inject(wrapper, "63010", age_s=40.0)
            q = broker.get_ws_quote(63010, max_age_seconds=10.0)
            assert (q.get("bid"), q.get("ask")) == (339.5, 341.0)
        finally:
            wrapper.close()

    def test_ladder_past_its_ttl_serves_price_without_depth(self) -> None:
        broker, _t, wrapper = _broker_with_ws()
        try:
            broker.subscribe_tokens_ws([63011], mode="full")
            self._inject(wrapper, "63011", age_s=1.0, ladder_age_s=400.0)
            q = broker.get_ws_quote(63011, max_age_seconds=10.0)
            assert q["ltp"] == 340.0
            assert q["bid"] == 0.0 and q["ask"] == 0.0
            assert q["ladder_status"].startswith("expired:")
            assert q["depth"] == {}
        finally:
            wrapper.close()

    def test_unsubscribed_token_cannot_use_the_socket_liveness_bypass(self) -> None:
        """§5.2: a token outside the registry is not "fresh because the socket is"."""
        broker, _t, wrapper = _broker_with_ws()
        try:
            self._inject(wrapper, "63012", age_s=40.0)
            assert broker.get_ws_quote(63012, max_age_seconds=10.0) == {}
        finally:
            wrapper.close()

    def test_unsubscribe_purges_the_cached_row(self) -> None:
        broker, ticker, wrapper = _broker_with_ws()
        try:
            broker.subscribe_tokens_ws([63013], mode="full")
            ticker.on_ticks(
                ticker, [_tick(63013, 340.0, [_level(339.5)], [_level(341.0)])]
            )
            assert wrapper.get_quote("63013") is not None
            broker.unsubscribe_tokens_ws([63013])
            assert wrapper.get_quote("63013") is None
            broker.subscribe_tokens_ws([63013], mode="full")
            assert broker.get_ws_quote(63013, max_age_seconds=10.0) == {}
        finally:
            wrapper.close()


class _FakeUpstoxFeed:
    """The upstox_ws_wrapper read API, minus the socket: rows on demand."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.connected = True
        self.last_any = time.monotonic()
        self.keys: set[str] = set()

    def is_connected(self) -> bool:
        return self.connected

    def last_any_tick_mono(self) -> float:
        return self.last_any

    def get_quote(self, instrument_key: str) -> dict[str, Any] | None:
        row = self.rows.get(str(instrument_key))
        return dict(row) if row is not None else None

    def subscribed_keys(self) -> list[str]:
        return list(self.keys)


class TestServeGateUpstoxTwin:
    """The same gate table as ``TestServeGate``, for the Upstox caller (T0.4).

    Rows are Upstox-wrapper-shaped (opaque ``NSE_FO|…`` keys, aware-UTC stamps);
    the gate must refuse exactly what it refuses for mStock. The quiet-leg row
    is refused under ``UPSTOX_SERVE_POLICY`` as shipped (fail-closed provisional
    until T0.5-2 measures deliberate quiet tokens) and served only under a
    ``liveness_rescue=True`` policy.
    """

    KEY = "NSE_FO|NIFTY|26NOV2026|25050|PE"

    def _broker(self) -> tuple[Any, _FakeUpstoxFeed]:
        from backtest.brokers.upstox import UpstoxBroker

        broker = UpstoxBroker()
        feed = _FakeUpstoxFeed()
        feed.keys.add(self.KEY)
        broker._ws_feed = feed  # Phase 1 item 2 attaches this for real
        return broker, feed

    def _inject(
        self, feed: _FakeUpstoxFeed, age_s: float, ladder_age_s: float | None = None
    ) -> None:
        now = time.monotonic()
        feed.rows[self.KEY] = {
            "ltp": 493.65,
            "bid": 493.60,
            "ask": 493.70,
            "depth": {"bid": [[493.60, 65]], "ask": [[493.70, 65]]},
            "ladder_status": "ok",
            "timestamp": "2026-11-11T03:49:14.812000+00:00",
            "recv_mono": now - age_s,
            "ltp_mono": now - age_s,
            "ladder_mono": now - (ladder_age_s if ladder_age_s is not None else age_s),
        }

    def test_unattached_broker_serves_nothing(self) -> None:
        """Phase 0 acceptance: no _ws_feed ⇒ {} from every caller, fail-closed."""
        from backtest.brokers.upstox import UpstoxBroker

        assert UpstoxBroker().get_ws_quote(self.KEY) == {}

    def test_fresh_row_is_served(self) -> None:
        broker, feed = self._broker()
        self._inject(feed, age_s=1.0)
        q = broker.get_ws_quote(self.KEY)
        assert (q.get("bid"), q.get("ask")) == (493.60, 493.70)

    def test_stale_row_is_refused(self) -> None:
        broker, feed = self._broker()
        self._inject(feed, age_s=40.0)
        assert broker.get_ws_quote(self.KEY) == {}

    def test_disconnected_socket_is_refused(self) -> None:
        broker, feed = self._broker()
        self._inject(feed, age_s=1.0)
        feed.connected = False
        assert broker.get_ws_quote(self.KEY) == {}

    def test_unparseable_stamp_is_refused(self) -> None:
        broker, feed = self._broker()
        self._inject(feed, age_s=1.0)
        feed.rows[self.KEY].pop("recv_mono")
        feed.rows[self.KEY].pop("ltp_mono")
        feed.rows[self.KEY]["timestamp"] = "not-a-stamp"
        assert broker.get_ws_quote(self.KEY) == {}

    def test_future_skewed_stamp_is_refused(self) -> None:
        broker, feed = self._broker()
        self._inject(feed, age_s=1.0)
        feed.rows[self.KEY].pop("recv_mono")
        feed.rows[self.KEY].pop("ltp_mono")
        feed.rows[self.KEY]["timestamp"] = "2126-11-11T09:19:14+00:00"
        assert broker.get_ws_quote(self.KEY) == {}

    def test_expired_ladder_serves_price_without_depth(self) -> None:
        broker, feed = self._broker()
        self._inject(feed, age_s=1.0, ladder_age_s=400.0)
        q = broker.get_ws_quote(self.KEY)
        assert q["ltp"] == 493.65
        assert q["bid"] == 0.0 and q["ask"] == 0.0
        assert q["depth"] == {}
        assert q["ladder_status"].startswith("expired:")

    def test_quiet_leg_refused_under_the_shipped_upstox_policy(self) -> None:
        """liveness_rescue=False (T0.5-2 decides otherwise) ⇒ no rescue."""
        broker, feed = self._broker()
        self._inject(feed, age_s=40.0)
        assert broker.get_ws_quote(self.KEY) == {}

    def test_quiet_leg_serves_when_the_rescue_is_enabled(self) -> None:
        from backtest.data.depth_serve_gate import ServeGatePolicy, serve_ws_row

        broker, feed = self._broker()
        self._inject(feed, age_s=40.0)
        row = feed.get_quote(self.KEY)
        rescue = ServeGatePolicy(
            venue="upstox", max_age_seconds=10.0, ladder_ttl_seconds=120.0, liveness_rescue=True
        )
        q = serve_ws_row(
            row=row,
            key=self.KEY,
            now_mono=time.monotonic(),
            is_connected=True,
            last_any_tick_mono=feed.last_any,
            token_subscribed=True,
            policy=rescue,
        )
        assert (q.get("bid"), q.get("ask")) == (493.60, 493.70)

    def test_unsubscribed_token_cannot_use_the_socket_liveness_bypass(self) -> None:
        """Mirror of the mStock test: a token outside the registry is not
        "fresh because the socket is"."""
        broker, feed = self._broker()
        feed.keys.clear()
        self._inject(feed, age_s=40.0)
        assert broker.get_ws_quote(self.KEY) == {}


# ---------------------------------------------------------------------------
# 3. Execution/mark seam: intrinsic floor + LTP distance (§8b, §8d)
# ---------------------------------------------------------------------------


def _contract(token: str, strike: float, option_type: OptionType) -> OptionContract:
    return OptionContract(
        instrument_token=token,
        trading_symbol=f"NIFTY{token}",
        underlying="NIFTY",
        expiry=date.today() + timedelta(days=7),
        strike=Decimal(str(strike)),
        option_type=option_type,
        lot_size=75,
        tick_size=Decimal("0.05"),
    )


class _ScriptedProvider:
    """A broker-shaped inner: fixed rows, a real contract registry, a live spot."""

    source_name = "scripted"

    def __init__(self, spot: float = 24340.0) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._contracts: dict[str, Any] = {}
        self._spot = spot

    def add(self, token: str, contract: OptionContract, **row: Any) -> None:
        self._contracts[token] = contract
        self.rows[token] = row

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        return dict(self.rows.get(str(instrument_token), {}))

    def get_spot(self, underlying: str) -> float:
        return self._spot


class TestQuoteSanityGates:
    def _provider(self) -> tuple[BidAskQuoteProvider, _ScriptedProvider]:
        inner = _ScriptedProvider(spot=24340.0)
        provider = BidAskQuoteProvider(inner)
        return provider, inner

    def test_fabricated_cheap_ask_is_refused(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        # Near-ATM 24350 CE at spot 24340: intrinsic ~0, so only the packet's own
        # LTP can prove ₹0.05 is not an ask. mStock's blank ladder printed exactly
        # this shape on 2026-11-03.
        inner.add("9001", _contract("9001", 24350.0, OptionType.CE),
                  ltp=340.0, bid=0.04, ask=0.05, source="ws")
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("9001", "BUY")
        assert "ltp_distance" in str(exc.value)
        assert provider.sanity_stats  # counted, for the soak to read

    def test_itm_ask_below_intrinsic_is_refused(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        # 24000 CE at spot 24340 is 340 ITM — a ₹0.05 offer is a free gift nobody
        # gives, which is what the intrinsic floor is for.
        inner.add("9012", _contract("9012", 24000.0, OptionType.CE),
                  ltp=340.0, bid=0.04, ask=0.05, source="ws")
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("9012", "BUY")
        assert "intrinsic_floor" in str(exc.value)

    def test_honest_itm_ask_is_accepted(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        inner.add("9002", _contract("9002", 24350.0, OptionType.CE),
                  ltp=340.0, bid=339.5, ask=341.0, source="ws")
        quote = provider.execution_quote("9002", "BUY")
        assert quote["ltp"] == 341.0
        assert quote["execution_side"] == "BUY"
        assert provider.sanity_stats == {}

    def test_far_from_its_own_ltp_is_refused_even_above_intrinsic(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        # OTM strike (intrinsic 0) so only the LTP-distance gate can see it.
        inner.add("9003", _contract("9003", 25500.0, OptionType.CE),
                  ltp=48.0, bid=47.5, ask=4.0, source="ws")
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("9003", "BUY")
        assert "ltp_distance" in str(exc.value)

    def test_wide_but_real_deep_otm_spread_survives(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        inner.add("9004", _contract("9004", 26000.0, OptionType.CE),
                  ltp=1.2, bid=0.45, ask=2.9, source="ws")
        quote = provider.execution_quote("9004", "BUY")
        assert quote["ltp"] == 2.9

    def test_mark_of_an_impossible_bid_keeps_the_last_mark(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        inner.add("9005", _contract("9005", 24000.0, OptionType.PE),
                  ltp=360.0, bid=5.0, ask=372.0, source="ws")
        marked = provider.mark_quote("9005", "BUY")
        assert marked.get("error"), "a long leg is never marked off a fake bid"

    def test_shadow_mode_logs_and_allows(self, monkeypatch) -> None:
        monkeypatch.setenv("OPTION_QUOTE_SANITY", "shadow")
        provider, inner = self._provider()
        inner.add("9006", _contract("9006", 24000.0, OptionType.CE),
                  ltp=340.0, bid=0.04, ask=0.05, source="ws")
        quote = provider.execution_quote("9006", "BUY")
        assert quote["ltp"] == 0.05
        assert provider.sanity_stats.get("intrinsic_floor") == 1

    def test_off_mode_restores_the_old_seam(self, monkeypatch) -> None:
        monkeypatch.setenv("OPTION_QUOTE_SANITY", "off")
        provider, inner = self._provider()
        inner.add("9007", _contract("9007", 24000.0, OptionType.CE),
                  ltp=340.0, bid=0.04, ask=0.05, source="ws")
        assert provider.execution_quote("9007", "BUY")["ltp"] == 0.05
        assert provider.sanity_stats == {}

    def test_intrinsic_gate_abstains_when_the_spot_is_unusable(self, monkeypatch) -> None:
        """A spot hiccup must not become a mass exit refusal across every book."""
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        inner = _ScriptedProvider()
        inner.get_spot = lambda _u: (_ for _ in ()).throw(ValueError("no live spot yet"))
        provider = BidAskQuoteProvider(inner)
        inner.add("9008", _contract("9008", 24350.0, OptionType.CE),
                  ltp=340.0, bid=339.5, ask=341.0, source="ws")
        assert provider.execution_quote("9008", "BUY")["ltp"] == 341.0

    def test_unknown_contract_still_gets_the_ltp_gate(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        inner.rows["9009"] = {"ltp": 340.0, "bid": 0.04, "ask": 0.05, "source": "ws"}
        with pytest.raises(UntrustedOptionQuoteError):
            provider.execution_quote("9009", "BUY")

    def test_stale_but_consistent_row_is_not_refused_as_fabricated(self, monkeypatch) -> None:
        """Spot moved, the whole leg lagged: that is staleness, not a fake price.

        Refusing here would lock the exit of a held ITM leg during a fast
        market, which is a worse outage than the one being fixed. The age gates
        own lag; this gate owns contradiction.
        """
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        inner.add("9013", _contract("9013", 24000.0, OptionType.CE),
                  ltp=45.15, bid=45.0, ask=45.15, source="ws")
        inner._spot = 24500.0  # intrinsic 500 — the LTP itself is far below it
        assert provider.execution_quote("9013", "BUY")["ltp"] == 45.15
        assert provider.sanity_stats == {}

    def test_zero_side_still_raises_the_original_reason(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        inner.add("9010", _contract("9010", 24350.0, OptionType.CE),
                  ltp=340.0, bid=339.5, ask=0.0, source="ws")
        with pytest.raises(NoExecutableQuoteError) as exc:
            provider.execution_quote("9010", "BUY")
        assert "NO_EXECUTION_QUOTE" in str(exc.value)

    def test_put_intrinsic_floor_uses_the_put_formula(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        provider, inner = self._provider()
        # 24400 PE at spot 24340 is 60 ITM; ₹4 is not a price for it.
        inner.add("9011", _contract("9011", 24400.0, OptionType.PE),
                  ltp=70.0, bid=4.0, ask=72.0, source="ws")
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("9011", "SELL")
        assert "intrinsic_floor" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. Pair parity: the ring's other strikes price the same forward (§8 item B)
# ---------------------------------------------------------------------------

#: Spot and forward are set equal so the intrinsic floor and parity agree on the
#: same book, and a test that expects ``pair_parity`` cannot be answered by a
#: different reason code by accident.
RING_SPOT = 24340.0
RING_TV = 8.0
RING_STRIKES = (24000.0, 24100.0, 24200.0, 24300.0, 24400.0, 24500.0, 24600.0)


def _fair(strike: float, option_type: OptionType) -> float:
    intrinsic = (
        max(0.0, RING_SPOT - strike)
        if option_type is OptionType.CE
        else max(0.0, strike - RING_SPOT)
    )
    return intrinsic + RING_TV


class _RingProvider:
    """A provider whose hot ring holds one expiry's full CE/PE ladder.

    ``set`` rewrites a leg's ask *and its own LTP together* on purpose: a bad
    price that agrees with its last trade is precisely what the per-row gates
    (§3) cannot see, and is what parity was derived for.
    """

    source_name = "ring"

    def __init__(self, strikes: tuple[float, ...] = RING_STRIKES) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._contracts: dict[str, OptionContract] = {}
        self.sibling_calls = 0
        for strike in strikes:
            for option_type in (OptionType.CE, OptionType.PE):
                token = f"{int(strike)}{option_type.value}"
                price = _fair(strike, option_type)
                self._contracts[token] = _contract(token, strike, option_type)
                self.rows[token] = {
                    "ltp": price, "bid": price - 0.5, "ask": price, "source": "ws",
                }

    def set(self, token: str, price: float) -> None:
        self.rows[token]["ask"] = price
        self.rows[token]["bid"] = price - 0.5
        self.rows[token]["ltp"] = price

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        return dict(self.rows.get(str(instrument_token), {}))

    def get_spot(self, underlying: str) -> float:
        return RING_SPOT

    def parity_siblings(self, instrument_token: str) -> list[tuple[Any, dict]]:
        self.sibling_calls += 1
        return [
            (contract, dict(self.rows[token]))
            for token, contract in self._contracts.items()
            if token != str(instrument_token)
        ]


class TestPairParity:
    def _provider(self, strikes: tuple[float, ...] = RING_STRIKES):
        inner = _RingProvider(strikes)
        return BidAskQuoteProvider(inner), inner

    def _enforce(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.setenv("OPTION_PARITY_SANITY", "enforce")

    def test_a_parity_consistent_ring_prices_clean(self, monkeypatch) -> None:
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        quote = provider.execution_quote("24000CE", "BUY")
        assert quote["ltp"] == pytest.approx(348.0)
        assert provider.sanity_stats == {}
        assert inner.sibling_calls  # parity ran, it just agreed

    def test_one_leg_out_of_line_with_its_expiry_is_refused(self, monkeypatch) -> None:
        """The ₹268 put: right of its own LTP, wrong against every other strike."""
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        inner.set("24400PE", 268.0)  # honest 68, +₹200, with matching ltp
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("24400PE", "BUY")
        assert "pair_parity" in str(exc.value)
        assert provider.sanity_stats == {"pair_parity": 1}

    def test_a_multiplicative_side_error_is_caught(self, monkeypatch) -> None:
        """Doubling one side bends the implied forward differently at each strike.

        This is the class the 2026-11-08 incident could not be caught by reading a
        row alone: every leg agrees with its own LTP and sits above intrinsic.
        """
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        for strike in RING_STRIKES:
            token = f"{int(strike)}PE"
            inner.set(token, 2.0 * inner.rows[token]["ask"])
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("24600PE", "BUY")
        assert "pair_parity" in str(exc.value)

    def test_a_common_shift_is_blind_and_that_is_documented(self, monkeypatch) -> None:
        """Add ₹5 to every put and the ring's answer is "the forward moved".

        Parity is relative, so it cannot see a whole side shift; that is the
        cross-broker witness's job (``shadow_diff``, docs/OPTION-JUNK-ASK-
        INCIDENT-2026-11-08.md §8f). Pinned here so the blind spot is a test
        rather than a surprise.
        """
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        for strike in RING_STRIKES:
            token = f"{int(strike)}PE"
            inner.set(token, inner.rows[token]["ask"] + 5.0)
        quote = provider.execution_quote("24600PE", "BUY")
        assert quote["ltp"] == pytest.approx(273.0)
        assert provider.sanity_stats == {}

    def test_a_thin_ring_has_no_opinion(self, monkeypatch) -> None:
        """Below PAIR_PARITY_MIN_STRIKES peers the median is noise: abstain."""
        self._enforce(monkeypatch)
        provider, inner = self._provider((24000.0, 24100.0, 24200.0, 24300.0))
        inner.set("24300PE", 500.0)
        quote = provider.execution_quote("24300PE", "BUY")
        assert quote["ltp"] == pytest.approx(500.0)
        assert provider.sanity_stats == {}

    def test_a_provider_without_peers_is_untouched(self, monkeypatch) -> None:
        """The seam keeps working for every inner that never heard of parity."""
        self._enforce(monkeypatch)
        inner = _ScriptedProvider(spot=RING_SPOT)
        provider = BidAskQuoteProvider(inner)
        inner.add("9200", _contract("9200", 24350.0, OptionType.CE),
                  ltp=340.0, bid=339.5, ask=341.0, source="ws")
        assert provider.execution_quote("9200", "BUY")["ltp"] == 341.0
        assert provider.sanity_stats == {}

    def test_shadow_is_the_default_and_counts_without_refusing(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.delenv("OPTION_PARITY_SANITY", raising=False)
        provider, inner = self._provider()
        inner.set("24400PE", 268.0)
        quote = provider.execution_quote("24400PE", "BUY")
        assert quote["ltp"] == pytest.approx(268.0)
        assert provider.sanity_stats == {"pair_parity": 1}

    def test_off_mode_never_reads_the_ring(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.setenv("OPTION_PARITY_SANITY", "off")
        provider, inner = self._provider()
        inner.set("24400PE", 268.0)
        assert provider.execution_quote("24400PE", "BUY")["ltp"] == pytest.approx(268.0)
        assert provider.sanity_stats == {}
        assert inner.sibling_calls == 0

    def test_the_general_gates_and_parity_answer_to_separate_envs(self, monkeypatch) -> None:
        """Parity can enforce while the proven gates stay in shadow, and back."""
        monkeypatch.setenv("OPTION_QUOTE_SANITY", "shadow")
        monkeypatch.setenv("OPTION_PARITY_SANITY", "off")
        provider, inner = self._provider()
        # ₹4 on a 340-point ITM call: the intrinsic floor sees it, shadow mode lets
        # the fill through and only counts it. Parity is off here on purpose — a
        # ₹4 ITM ask dissents from its own expiry too, and an enforcing gate is not
        # allowed to be masked by a shadow one (see TestCrossBrokerCorroboration).
        inner.rows["24000CE"].update(ask=4.0, bid=4.0)
        assert provider.execution_quote("24000CE", "BUY")["ltp"] == 4.0
        assert provider.sanity_stats == {"intrinsic_floor": 1}
        assert inner.sibling_calls == 0
        # ₹268 on the 24400 put: every per-row gate is silent, parity refuses.
        monkeypatch.setenv("OPTION_PARITY_SANITY", "enforce")
        inner.set("24400PE", 268.0)
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("24400PE", "BUY")
        assert "pair_parity" in str(exc.value)
        assert provider.sanity_stats == {"intrinsic_floor": 1, "pair_parity": 1}


# ---------------------------------------------------------------------------
# 5. The reference is read-only: no HTTP, no subscription (§8 item B)
# ---------------------------------------------------------------------------


class _WSOnlyBroker:
    """Answers only the WebSocket cache; any other door fails the test.

    A peer read that cold-misses must not fall through to the broker's HTTP quote
    (mStock's ``_try_ws_quote`` auto-subscribes on a miss) — a witness that
    subscribes on behalf of every mark turns an observation into feed churn.
    """

    def __init__(self, missing: tuple[int, ...] = ()) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.ws_reads: list[int] = []
        self._subscribed: set[int] = set()
        self._missing = set(missing)

    def get_ws_quote(self, token: int, max_age_seconds: float | None = None) -> dict:
        self.ws_reads.append(int(token))
        if int(token) in self._missing:
            return {}
        return dict(self.rows.get(int(token), {}))

    def subscribe_tokens_ws(self, tokens, mode=None) -> list[int]:
        self._subscribed.update(int(t) for t in tokens)
        return sorted(self._subscribed)

    def get_subscribed_tokens(self) -> set[int]:
        return set(self._subscribed)

    #: The doors a witness must never open: a peer fetched over HTTP, or the
    #: chain endpoint asked for a leg the ring already holds.
    FORBIDDEN = ("get_quote", "get_option_quote", "fetch_option_chain")

    def __getattr__(self, name: str):
        if name in self.FORBIDDEN:
            raise AssertionError(
                f"the parity witness reached for broker.{name} — building a "
                "reference for one leg may not spend an HTTP quote"
            )
        raise AttributeError(name)


def _live_ring(missing: tuple[int, ...] = ()):
    broker = _WSOnlyBroker(missing=missing)
    provider = LiveChainProvider(broker, require_live_spot=False)
    expiry = date.today() + timedelta(days=7)
    contracts: list[OptionContract] = []
    for index, offset in enumerate(range(-6, 7)):
        strike = 24350.0 + offset * 50.0
        for option_type in (OptionType.CE, OptionType.PE):
            token = str(63000 + index * 2 + (0 if option_type is OptionType.CE else 1))
            contracts.append(
                OptionContract(
                    instrument_token=token,
                    trading_symbol=f"NIFTY{token}",
                    underlying="NIFTY",
                    expiry=expiry,
                    strike=Decimal(str(strike)),
                    option_type=option_type,
                    lot_size=75,
                    tick_size=Decimal("0.05"),
                )
            )
            price = _fair(strike, option_type)
            broker.rows[int(token)] = {
                "ltp": price, "bid": price - 0.5, "ask": price, "source": "ws",
            }
    provider.hot_ring.recenter("NIFTY", RING_SPOT, contracts, force=True)
    return provider, broker


class TestRingBookIsReadOnly:
    def test_the_reference_walks_the_ring_through_the_ws_cache(self) -> None:
        provider, broker = _live_ring()
        book = provider.ring_book("NIFTY")
        ring = provider.hot_ring.subscribed_tokens("NIFTY")
        assert len(book) == len(ring) == 26
        assert sorted(broker.ws_reads) == sorted(ring)

    def test_a_second_read_inside_the_ttl_costs_nothing(self) -> None:
        """26 legs per read × every mark of every book must not re-read the socket."""
        provider, broker = _live_ring()
        first = len(provider.ring_book("NIFTY"))
        reads = len(broker.ws_reads)
        assert len(provider.ring_book("NIFTY")) == first
        assert len(broker.ws_reads) == reads

    def test_a_peer_the_feed_stopped_printing_drops_out_of_the_median(self) -> None:
        provider, _b = _live_ring(missing=(63000,))
        book = provider.ring_book("NIFTY")
        assert len(book) == 25
        assert all(row.get("ask") for _c, row in book)

    def test_siblings_are_the_ring_minus_the_leg_under_test(self) -> None:
        provider, _b = _live_ring()
        siblings = provider.parity_siblings("63000")
        assert len(siblings) == 25
        assert "63000" not in {str(c.instrument_token) for c, _r in siblings}

    def test_a_token_outside_the_ring_has_no_peers(self) -> None:
        provider, _b = _live_ring()
        assert provider.parity_siblings("77777") == []

    def test_the_seam_refuses_a_live_ring_dissent_end_to_end(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.setenv("OPTION_PARITY_SANITY", "enforce")
        chain, broker = _live_ring()
        chain.set_spot("NIFTY", RING_SPOT)
        seam = BidAskQuoteProvider(_CachedLegInner(chain, broker))
        broker.rows[63001]["ask"] += 200.0  # the 24350 PE, with a matching ltp
        broker.rows[63001]["ltp"] = broker.rows[63001]["ask"]
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            seam.execution_quote("63000", "BUY")
        assert "pair_parity" in str(exc.value)
        assert seam.sanity_stats == {"pair_parity": 1}


class _CachedLegInner:
    """The real ring for peers; the leg itself served from the cached row."""

    source_name = "live:test"

    def __init__(self, chain: LiveChainProvider, broker: _WSOnlyBroker) -> None:
        self._chain = chain
        self._broker = broker

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        return dict(self._broker.rows[int(instrument_token)])

    def __getattr__(self, name: str):
        return getattr(self._chain, name)


# ---------------------------------------------------------------------------
# 6. Cross-broker corroboration at the fill seam (§8 item F, promoted)
# ---------------------------------------------------------------------------


class _PeerMixin:
    """Adds a second broker's L1 to any inner, settable per token.

    Mirrors :meth:`LiveChainProvider.peer_l1` exactly — same shape, same
    abstention (``None``) — so the seam is tested against the contract the live
    mirror actually honors rather than against a richer fake.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.peers: dict[str, dict[str, Any]] = {}
        self.peer_calls = 0

    def peer_l1(self, instrument_token: str):
        self.peer_calls += 1
        return self.peers.get(str(instrument_token))


class _PeerScripted(_PeerMixin, _ScriptedProvider):
    pass


class _PeerRing(_PeerMixin, _RingProvider):
    pass


def _peer(price: float, age_s: float = 1.0) -> dict[str, Any]:
    return {"ltp": price, "bid": price - 0.5, "ask": price, "age_s": age_s,
            "broker": "upstox"}


class TestCrossBrokerCorroboration:
    #: Spot 25080 with a 25050 strike keeps the put OTM, so the intrinsic floor is
    #: silent and only the second broker can answer — the real incident's shape.
    SPOT = 25080.0

    def _provider(self) -> tuple[BidAskQuoteProvider, _PeerScripted]:
        inner = _PeerScripted(spot=self.SPOT)
        return BidAskQuoteProvider(inner), inner

    def _enforce(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.delenv("OPTION_PARITY_SANITY", raising=False)
        monkeypatch.setenv("OPTION_CROSS_BROKER_SANITY", "enforce")

    def _junk_and_witness(self, inner: _PeerScripted) -> None:
        """The 2026-11-08 exit price, with Upstox's real book beside it."""
        inner.add("25050PE", _contract("25050PE", 25050.0, OptionType.PE),
                  ltp=367.0, bid=366.75, ask=493.65, source="ws")
        inner.peers["25050PE"] = _peer(368.15)

    def test_the_offer_the_other_broker_denies_is_refused(self, monkeypatch) -> None:
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        self._junk_and_witness(inner)
        # 1.34x its own last trade: inside x5, above an OTM floor — every per-row
        # gate is silent. This is the row that stopped the straddle.
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("25050PE", "BUY")
        assert "cross_broker_l1" in str(exc.value)
        assert provider.sanity_stats == {"cross_broker_l1": 1}

    def test_band_is_the_measured_max_of_rupees_and_percent(self, monkeypatch) -> None:
        """p99 ₹3.60, largest legitimate gap ₹7.45 → floor ₹10; 2 % past ₹500."""
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        for ask, expected in ((417.80, True), (418.00, False)):
            inner.add("9100", _contract("9100", 26000.0, OptionType.PE),
                      ltp=ask, bid=ask - 0.5, ask=ask, source="ws")
            inner.peers["9100"] = _peer(407.85)
            if expected:
                assert provider.execution_quote("9100", "BUY")["ltp"] == pytest.approx(ask)
            else:
                with pytest.raises(UntrustedOptionQuoteError) as exc:
                    provider.execution_quote("9100", "BUY")
                assert "cross_broker_l1" in str(exc.value)
        # A ₹14 gap on a ₹900 leg is inside 2 % (₹18) but not inside ₹10: without
        # the proportional term the band would be scale-blind on deep ITM strikes.
        inner.add("9101", _contract("9101", 26000.0, OptionType.PE),
                  ltp=916.0, bid=915.5, ask=916.0, source="ws")
        inner.peers["9101"] = _peer(900.0)
        assert provider.execution_quote("9101", "BUY")["ltp"] == pytest.approx(916.0)

    def test_a_stale_peer_is_not_a_witness(self, monkeypatch) -> None:
        """2026-11-10: neither broker logged in, Upstox's ring ~28 h old.

        Without the age condition that is 1,522 "disagreements" and the gate
        refuses the whole session — so a stale peer must buy silence, not a veto.
        """
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        self._junk_and_witness(inner)
        inner.peers["25050PE"] = _peer(368.15, age_s=100_000.0)
        assert provider.execution_quote("25050PE", "BUY")["ltp"] == pytest.approx(493.65)
        assert provider.sanity_stats == {}

    def test_no_second_broker_costs_corroboration_not_trading(self, monkeypatch) -> None:
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        self._junk_and_witness(inner)
        inner.peers.pop("25050PE")
        assert provider.execution_quote("25050PE", "BUY")["ltp"] == pytest.approx(493.65)
        assert provider.sanity_stats == {}

    def test_a_peer_that_offers_nothing_on_this_side_is_ignored(self, monkeypatch) -> None:
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        self._junk_and_witness(inner)
        inner.peers["25050PE"] = {"ltp": 368.15, "bid": 367.0, "ask": 0.0, "age_s": 1.0}
        assert provider.execution_quote("25050PE", "BUY")["ltp"] == pytest.approx(493.65)

    def test_shadow_is_the_default_and_counts_without_refusing(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.delenv("OPTION_PARITY_SANITY", raising=False)
        monkeypatch.delenv("OPTION_CROSS_BROKER_SANITY", raising=False)
        provider, inner = self._provider()
        self._junk_and_witness(inner)
        assert provider.execution_quote("25050PE", "BUY")["ltp"] == pytest.approx(493.65)
        assert provider.sanity_stats == {"cross_broker_l1": 1}

    def test_off_mode_never_asks_the_peer(self, monkeypatch) -> None:
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.setenv("OPTION_CROSS_BROKER_SANITY", "off")
        provider, inner = self._provider()
        self._junk_and_witness(inner)
        assert provider.execution_quote("25050PE", "BUY")["ltp"] == pytest.approx(493.65)
        assert inner.peer_calls == 0

    def test_an_enforcing_gate_is_not_masked_by_a_shadow_witness(self, monkeypatch) -> None:
        """Parity still measuring must not veto the corroboration gate.

        Both witnesses can be right about the same leg; the refusal has to name
        the one that is enforcing, or flipping ``OPTION_CROSS_BROKER_SANITY`` to
        ``enforce`` would change nothing while parity sat in ``shadow``.
        """
        monkeypatch.delenv("OPTION_QUOTE_SANITY", raising=False)
        monkeypatch.delenv("OPTION_PARITY_SANITY", raising=False)
        monkeypatch.setenv("OPTION_CROSS_BROKER_SANITY", "enforce")
        inner = _PeerRing()
        provider = BidAskQuoteProvider(inner)
        inner.set("24400PE", 268.0)  # dissents from its own expiry AND from Upstox
        inner.peers["24400PE"] = _peer(68.5)
        with pytest.raises(UntrustedOptionQuoteError) as exc:
            provider.execution_quote("24400PE", "BUY")
        assert "cross_broker_l1" in str(exc.value)
        assert "pair_parity" not in str(exc.value)

    def test_a_denied_bid_cannot_become_a_mark_either(self, monkeypatch) -> None:
        """Item D's premise: refusing the price must also refuse the *mark*.

        A straddle was stopped by a mark, not a fill, so the mark path gets the
        same witness: on refusal the leg keeps its last honest price and the row
        is labelled failed instead of re-priced.
        """
        self._enforce(monkeypatch)
        provider, inner = self._provider()
        inner.add("25050PE", _contract("25050PE", 25050.0, OptionType.PE),
                  ltp=367.0, bid=366.75, ask=493.65, source="ws")
        inner.peers["25050PE"] = {"ltp": 102.0, "bid": 101.5, "ask": 102.0,
                                  "age_s": 0.9, "broker": "upstox"}
        quote = provider.mark_quote("25050PE", "SELL")  # short leg → marked at ask
        assert "cross_broker_l1" in str(quote.get("error"))
        assert quote["ltp"] == pytest.approx(367.0)

    def test_peer_l1_is_a_pure_read_when_no_mirror_is_running(self) -> None:
        chain, broker = _live_ring()
        reads = len(broker.ws_reads)
        assert chain.peer_l1("63000") is None
        assert len(broker.ws_reads) == reads
