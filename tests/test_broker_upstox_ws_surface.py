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
