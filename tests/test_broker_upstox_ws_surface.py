"""UpstoxBroker's WebSocket depth surface (impl plan Phase 1 item 2).

The contract is MStockBroker's, so ``HotStrikeRing`` and ``LiveChainProvider``
work unchanged: the ring speaks opaque keys, the broker hands them to the feed
that owns the socket, and the subscription registry tells the truth about what
the socket generation holds. Every test here uses a duck-typed fake feed — no
socket, no network, no token.

Ground rule 7 (blast radius): attaching ``_ws_feed`` is what makes
``has_ws_ring_support()`` True, and a book configured ``source=upstox`` then
warms its ring for real. Books stay on ``source=mstock`` (the default) until
the plan's Phase 4 shadow controls exist; these tests prove the surface, not
the deployment.
"""

from __future__ import annotations

from typing import Any

from backtest.brokers.upstox import UpstoxBroker
from backtest.options.quote_providers import LiveChainProvider


class _FakeFeed:
    """The wrapper's broker-facing API, minus the socket."""

    def __init__(self) -> None:
        self.started: list[tuple[list[str], Any]] = []
        self.subscribed: list[tuple[list[str], Any]] = []
        self.changed: list[tuple[list[str], Any]] = []
        self.unsubscribed: list[list[str]] = []
        self.forgotten: list[str] = []
        self.listeners: list[Any] = []
        self.closed = False
        self.connected = True
        self.keys: list[str] = []
        self.rows: dict[str, dict] = {}

    def get_quote(self, instrument_key):
        row = self.rows.get(str(instrument_key))
        return dict(row) if row is not None else None

    def subscribed_keys(self):
        return list(self.keys)

    def last_any_tick_mono(self):
        import time as _time

        return _time.monotonic()

    def start(self, keys, mode=None):
        self.started.append((list(keys), mode))
        self.keys.extend(k for k in keys if k not in self.keys)

    def subscribe(self, keys, mode=None):
        self.subscribed.append((list(keys), mode))
        self.keys.extend(k for k in keys if k not in self.keys)

    def change_mode(self, keys, mode):
        self.changed.append((list(keys), mode))

    def unsubscribe(self, keys):
        self.unsubscribed.append(list(keys))
        self.keys = [k for k in self.keys if k not in keys]

    def forget_token(self, key):
        self.forgotten.append(str(key))
        return True

    def is_connected(self):
        return self.connected

    def wait_for_connection(self, timeout):
        return self.connected

    def add_tick_listener(self, callback):
        self.listeners.append(callback)

    def remove_tick_listener(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def close(self):
        self.closed = True


def _broker_with_feed() -> tuple[UpstoxBroker, _FakeFeed]:
    broker = UpstoxBroker()
    feed = _FakeFeed()
    broker._ws_feed = feed
    return broker, feed


class TestRingSupport:
    def test_attached_feed_makes_ws_ring_support_true(self) -> None:
        broker, _feed = _broker_with_feed()
        provider = LiveChainProvider(broker)
        assert provider.has_ws_ring_support() is True

    def test_fresh_broker_reports_no_ring_support(self) -> None:
        provider = LiveChainProvider(UpstoxBroker())
        assert provider.has_ws_ring_support() is False


class TestSubscribeSurface:
    def test_first_subscribe_opens_the_socket_once(self) -> None:
        broker, feed = _broker_with_feed()
        ok = broker.subscribe_tokens_ws(["NSE_FO|A"], mode="full")
        assert ok == ["NSE_FO|A"]
        assert feed.started == [(["NSE_FO|A"], "full")]
        assert feed.subscribed == []  # start() carries the first keys

        more = broker.subscribe_tokens_ws(["NSE_FO|B"], mode="full")
        assert more == ["NSE_FO|B"]
        assert feed.subscribed == [(["NSE_FO|B"], "full")]  # no second start()
        assert broker.ws_subscribed_tokens() == {"NSE_FO|A", "NSE_FO|B"}

    def test_dupe_and_empty_keys_are_ignored(self) -> None:
        broker, feed = _broker_with_feed()
        ok = broker.subscribe_tokens_ws(["NSE_FO|A", "NSE_FO|A", "  ", ""], mode="full")
        assert ok == ["NSE_FO|A"]
        assert len(feed.started) == 1

    def test_mode_upgrade_uses_change_mode(self) -> None:
        broker, feed = _broker_with_feed()
        broker.subscribe_tokens_ws(["NSE_FO|A"], mode="ltp")
        assert feed.started == [(["NSE_FO|A"], "ltpc")]
        broker.subscribe_tokens_ws(["NSE_FO|A"], mode="full")
        assert feed.changed == [(["NSE_FO|A"], "full")]
        assert feed.subscribed == []

    def test_no_resend_when_nothing_is_new(self) -> None:
        broker, feed = _broker_with_feed()
        broker.subscribe_tokens_ws(["NSE_FO|A"], mode="full")
        again = broker.subscribe_tokens_ws(["NSE_FO|A"], mode="full")
        assert again == []
        assert feed.subscribed == [] and feed.changed == []

    def test_unattached_broker_sends_nothing(self) -> None:
        broker = UpstoxBroker()
        assert broker.subscribe_tokens_ws(["NSE_FO|A"]) == []

    def test_disconnected_socket_skips_the_batch(self) -> None:
        broker, feed = _broker_with_feed()
        broker._ws_started = True  # pretend the socket already started once
        feed.connected = False
        assert broker.subscribe_tokens_ws(["NSE_FO|B"]) == []
        assert feed.subscribed == []


class TestUnsubscribeAndPins:
    def test_unsubscribe_purges_and_forgets(self) -> None:
        broker, feed = _broker_with_feed()
        broker.subscribe_tokens_ws(["NSE_FO|A", "NSE_FO|B"], mode="full")
        removed = broker.unsubscribe_tokens_ws(["NSE_FO|A", "NSE_FO|B"])
        assert removed == ["NSE_FO|A", "NSE_FO|B"]
        assert feed.unsubscribed == [["NSE_FO|A", "NSE_FO|B"]]
        assert feed.forgotten == ["NSE_FO|A", "NSE_FO|B"]
        assert broker.ws_subscribed_tokens() == set()

    def test_pinned_legs_survive_unsubscribe(self) -> None:
        broker, feed = _broker_with_feed()
        broker.subscribe_tokens_ws(["NSE_FO|A", "NSE_FO|B"], mode="full")
        assert broker.pin_ws_tokens(["NSE_FO|A"], mode="full") == ["NSE_FO|A"]
        removed = broker.unsubscribe_tokens_ws(["NSE_FO|A", "NSE_FO|B"])
        assert removed == ["NSE_FO|B"]  # A is held open; it stays
        assert broker.ws_subscribed_tokens() == {"NSE_FO|A"}

    def test_index_spot_keys_are_never_unsubscribed(self) -> None:
        broker, feed = _broker_with_feed()
        broker.subscribe_tokens_ws(["NSE_INDEX|Nifty 50", "NSE_FO|A"], mode="full")
        removed = broker.unsubscribe_tokens_ws(["NSE_INDEX|Nifty 50", "NSE_FO|A"])
        assert removed == ["NSE_FO|A"]
        assert broker.ws_subscribed_tokens() == {"NSE_INDEX|Nifty 50"}

    def test_pin_refcount_is_one_for_one(self) -> None:
        broker, _feed = _broker_with_feed()
        broker.subscribe_tokens_ws(["NSE_FO|A"], mode="full")
        broker.pin_ws_tokens(["NSE_FO|A"], mode="full")
        broker.pin_ws_tokens(["NSE_FO|A"], mode="full")
        assert broker.unsubscribe_tokens_ws(["NSE_FO|A"]) == []  # twice pinned
        assert broker.unpin_ws_tokens(["NSE_FO|A"]) == []
        assert broker.unsubscribe_tokens_ws(["NSE_FO|A"]) == []  # once pinned
        assert broker.unpin_ws_tokens(["NSE_FO|A"]) == ["NSE_FO|A"]
        assert broker.unsubscribe_tokens_ws(["NSE_FO|A"]) == ["NSE_FO|A"]


class TestReinit:
    def test_reinit_resets_the_registry_and_fires_listeners(self) -> None:
        broker, feed = _broker_with_feed()
        events: list[int] = []
        broker.add_ws_reinit_listener(lambda: events.append(1))
        broker.subscribe_tokens_ws(["NSE_FO|A"], mode="full")
        assert broker.ws_subscribed_tokens() == {"NSE_FO|A"}

        broker._init_websocket_feed()  # re-login: a brand-new feed generation
        assert feed.closed is True
        assert broker.ws_subscribed_tokens() == set()  # the 2026-10-26 lesson
        assert events == [1]
        assert broker._ws_feed is not feed
        broker._ws_feed.close()  # the real wrapper: no thread started, safe

    def test_tick_listeners_survive_reinit(self) -> None:
        broker, feed = _broker_with_feed()
        seen: list[dict] = []
        broker.add_tick_listener(seen.append)
        assert feed.listeners == [seen.append]

        broker._init_websocket_feed()
        assert broker._ws_feed._listeners == [seen.append]
        broker._ws_feed.close()

    def test_logout_closes_the_feed(self) -> None:
        broker, feed = _broker_with_feed()
        broker.subscribe_tokens_ws(["NSE_FO|A"], mode="full")
        broker.logout()
        assert feed.closed is True
        assert broker._ws_feed is None
        assert broker.ws_subscribed_tokens() == set()


# ---------------------------------------------------------------------------
# item 4 — get_option_quote: WS first, HTTP fallback, never a digit guard
# ---------------------------------------------------------------------------


def _gate_fresh_row(**overrides) -> dict:
    import time as _time

    row = {
        "ltp": 493.65,
        "bid": 493.60,
        "ask": 493.70,
        "volume": 1000.0,
        "oi": 54465.0,
        "depth": {"bid": [[493.60, 65]], "ask": [[493.70, 65]]},
        "ladder_status": "ok",
        "timestamp": "2026-11-11T03:49:14.812000+00:00",
        "recv_mono": _time.monotonic(),
        "ltp_mono": _time.monotonic(),
        "ladder_mono": _time.monotonic(),
    }
    row.update(overrides)
    return row


class TestOptionQuoteWiring:
    def test_ws_row_wins_and_no_http_is_issued(self, monkeypatch) -> None:
        """Finding C: a key-shaped contract must never be refused by a digit test."""
        broker, feed = _broker_with_feed()
        feed.rows = {"NSE_FO|NIFTY|26NOV2026|25050|PE": _gate_fresh_row()}

        def no_http(*args, **kwargs):
            raise AssertionError("HTTP quote call must not happen when the WS row serves")

        monkeypatch.setattr(broker, "_api_request", no_http)
        q = broker.get_option_quote("NSE_FO|NIFTY|26NOV2026|25050|PE")
        assert (q["bid"], q["ask"]) == (493.60, 493.70)
        assert q["ladder_status"] == "ok"

    def test_http_fallback_when_the_cache_is_empty(self, monkeypatch) -> None:
        broker, feed = _broker_with_feed()
        calls = []

        def fake_api(method, path, params=None, **kwargs):
            calls.append((method, path, params))
            key = params["instrument_key"]
            return {
                "data": {
                    key: {
                        "last_price": 493.65,
                        "depth": {
                            "buy": [{"price": 493.60, "quantity": 65}],
                            "sell": [{"price": 493.70, "quantity": 65}],
                        },
                        "volume": 1000.0,
                        "open_interest": 54465.0,
                    }
                }
            }

        monkeypatch.setattr(broker, "_api_request", fake_api)
        q = broker.get_option_quote("NSE_FO|NIFTY|26NOV2026|25050|PE")
        assert q["ltp"] == 493.65 and (q["bid"], q["ask"]) == (493.60, 493.70)
        assert len(calls) == 1  # exactly one HTTP call, with the raw key

    def test_no_feed_at_all_still_falls_back_to_http(self, monkeypatch) -> None:
        broker = UpstoxBroker()  # unattached: get_ws_quote returns {}
        monkeypatch.setattr(
            broker,
            "_api_request",
            lambda *a, **k: {
                "data": {"NSE_FO|A": {"last_price": 10.0, "depth": {}}}
            },
        )
        q = broker.get_option_quote("NSE_FO|A")
        assert q["ltp"] == 10.0


# ---------------------------------------------------------------------------
# item 5 — spot lookup: the broker that owns the book names its own index
# ---------------------------------------------------------------------------


class TestSpotLookup:
    def test_upstox_index_key_is_the_venues_own_spelling(self) -> None:
        assert UpstoxBroker.underlying_index_key("NIFTY") == "NSE_INDEX|Nifty 50"
        assert UpstoxBroker.underlying_index_key("banknifty") == "NSE_INDEX|Nifty Bank"

    def test_unmapped_underlying_returns_none_not_a_guess(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "backtest.brokers.upstox._load_instrument_keys", lambda timeout: {}
        )
        assert UpstoxBroker.underlying_index_key("NOPE") is None

    def test_provider_spot_asks_the_broker_for_its_own_key(self) -> None:
        calls = []

        class _KeyedBroker:
            def underlying_index_key(self, underlying):
                return "NSE_INDEX|Nifty 50"

            def get_ws_quote(self, key, max_age_seconds=None, **kw):
                calls.append(key)
                return {"ltp": 25062.5}

        provider = LiveChainProvider(_KeyedBroker())
        assert provider._infer_spot_from_ws("NIFTY") == 25062.5
        assert calls == ["NSE_INDEX|Nifty 50"]  # a string key, never 26000

    def test_mstock_numeric_path_is_bit_for_bit_unchanged(self) -> None:
        calls = []

        class _NumericBroker:
            UNDERLYING_INDEX_TOKENS = {"NIFTY": 26000}

            def get_ws_quote(self, token, max_age_seconds=None, **kw):
                calls.append(token)
                return {"ltp": 25062.5}

        provider = LiveChainProvider(_NumericBroker())
        assert provider._infer_spot_from_ws("NIFTY") == 25062.5
        assert calls == [26000]

    def test_no_key_on_the_venue_makes_spot_abstain(self) -> None:
        calls = []

        class _BlindBroker:
            def underlying_index_key(self, underlying):
                return None

            def get_ws_quote(self, key, max_age_seconds=None, **kw):
                calls.append(key)
                return {"ltp": 25062.5}

        provider = LiveChainProvider(_BlindBroker())
        assert provider._infer_spot_from_ws("NIFTY") is None
        assert calls == []  # no invented number, no lookup at all
