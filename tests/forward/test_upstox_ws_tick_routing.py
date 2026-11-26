"""Upstox shadow WS tick routing (the string-venue twin of the mStock path).

The shadow socket must feed the SAME bar fan-out as the primary so the regime
engine sees a continuous India VIX print, without ever driving a runner. These
tests exercise the routing/attach logic in isolation — no socket, no network,
no database.
"""

from __future__ import annotations

from backtest.forward.portfolio_manager import PortfolioManager


class _FakeUpstoxFeed:
    def __init__(self) -> None:
        self.ticks: list[tuple[str, dict]] = []
        self.symbols: list[str] = []

    def add_symbols(self, symbols):
        self.symbols.extend(symbols)

    def ingest_ws_tick(self, symbol, tick, **kwargs):
        self.ticks.append((symbol, dict(tick)))
        return True


class _FakeUpstoxBroker:
    broker_name = "upstox"
    DEFAULT_INDEX_KEYS = frozenset({"NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"})

    def __init__(self) -> None:
        self.listeners: list = []
        self.subscribed: list[list[str]] = []

    def add_tick_listener(self, callback):
        self.listeners.append(callback)

    def remove_tick_listener(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def subscribe_tokens_ws(self, keys, mode="full", wait_timeout=0.0):
        self.subscribed.append(list(keys))
        return list(keys)


def _manager() -> PortfolioManager:
    mgr = PortfolioManager.__new__(PortfolioManager)
    mgr._upstox_ws_key_symbols = {
        "NSE_INDEX|Nifty 50": "NIFTY",
        "NSE_INDEX|India VIX": "INDIAVIX",
    }
    mgr._index_feed_symbols = ("INDIAVIX",)
    mgr._attached_upstox_ws_broker = None
    mgr._injected_live_broker = None
    mgr.upstox_feed = _FakeUpstoxFeed()
    return mgr


class TestTickRouting:
    def test_vix_key_routes_to_the_upstox_bar_feed(self) -> None:
        mgr = _manager()
        tick = {"ltp": 14.25, "recv_utc": "2026-11-24T04:15:02+00:00"}
        mgr._on_upstox_ws_tick_batch({"NSE_INDEX|India VIX": tick})
        assert mgr.upstox_feed.ticks == [("INDIAVIX", tick)]

    def test_unknown_key_is_ignored(self) -> None:
        mgr = _manager()
        mgr._on_upstox_ws_tick_batch({"NSE_FO|UNKNOWN": {"ltp": 10.0}})
        assert mgr.upstox_feed.ticks == []

    def test_nonpositive_ltp_is_ignored(self) -> None:
        mgr = _manager()
        mgr._on_upstox_ws_tick_batch({"NSE_INDEX|India VIX": {"ltp": 0.0}})
        assert mgr.upstox_feed.ticks == []

    def test_empty_batch_is_a_noop(self) -> None:
        mgr = _manager()
        mgr._on_upstox_ws_tick_batch({})
        assert mgr.upstox_feed.ticks == []


class TestAttach:
    def test_attach_adds_the_vix_symbol_and_registers_the_listener(self) -> None:
        mgr = _manager()
        broker = _FakeUpstoxBroker()
        assert mgr.attach_upstox_ws_broker(broker) is True
        assert broker.listeners == [mgr._on_upstox_ws_tick_batch]
        assert "INDIAVIX" in mgr.upstox_feed.symbols

    def test_ensure_attaches_and_seeds_default_index_keys(self) -> None:
        mgr = _manager()
        broker = _FakeUpstoxBroker()
        mgr._injected_live_broker = broker
        assert mgr._ensure_upstox_ws_tick_subscription() is True
        assert broker.listeners == [mgr._on_upstox_ws_tick_batch]
        assert broker.subscribed and "NSE_INDEX|India VIX" in broker.subscribed[0]

    def test_detach_removes_the_listener(self) -> None:
        mgr = _manager()
        broker = _FakeUpstoxBroker()
        mgr.attach_upstox_ws_broker(broker)
        mgr.detach_upstox_ws_broker()
        assert broker.listeners == []
        assert mgr._attached_upstox_ws_broker is None