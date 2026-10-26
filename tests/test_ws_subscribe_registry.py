"""A re-login must not inherit the previous socket's subscription registry.

Live regression 2026-10-26: ``POST /api/broker/login`` mid-session rebuilt the
MTicker socket but left ``MStockBroker._ws_subscribed_tokens`` standing, so
``subscribe_to_instrument()`` answered "already subscribed" for the indices and
every option token from the old socket. No subscribe frame ever reached the new
connection, ``get_ws_quote()`` had no ticks to serve, and quotes silently
downgraded to the LTP-only HTTP endpoint. The socket reported
``connection established`` + ``LOGIN sent`` all day while carrying zero depth:
no entry or exit filled, and held SELL legs could not be marked against an ask.
"""

from __future__ import annotations

import sys
import types

import pytest

from backtest.brokers.mstock import MStockBroker

INDEX_TOKENS = [26000, 26009, 26013, 26037]
OPTION_TOKEN = 51338


class FakeTicker:
    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.on_open = None
        self.subscribed: list[int] = []
        self.modes: list[tuple[str, tuple[int, ...]]] = []

    def connect(self, threaded: bool = False) -> None:
        self.connected = threaded

    def subscribe(self, tokens) -> None:
        self.subscribed.extend(tokens)

    def set_mode(self, mode, tokens) -> None:
        self.modes.append((mode, tuple(tokens)))

    def send_login_after_connect(self) -> None:
        self.login_sent = True


class FakeWrapper:
    def __init__(self, ticker) -> None:
        self._ticker = ticker

    def wait_for_connection(self, timeout: float = 0.0) -> bool:
        return True


@pytest.fixture
def fake_ws(monkeypatch):
    mticker_mod = types.ModuleType("backtest.data.mticker")
    mticker_mod.MTicker = FakeTicker
    wrapper_mod = types.ModuleType("backtest.data.mstock_ws_wrapper")
    wrapper_mod.MStockWebSocketWrapper = FakeWrapper
    monkeypatch.setitem(sys.modules, "backtest.data.mticker", mticker_mod)
    monkeypatch.setitem(sys.modules, "backtest.data.mstock_ws_wrapper", wrapper_mod)
    monkeypatch.setenv("MSTOCK_API_KEY", "fake-api-key")
    return FakeTicker


def _broker() -> MStockBroker:
    return MStockBroker(session_ttl_minutes=30.0)


def test_relogin_drops_the_stale_registry_and_subscribes_indices(fake_ws):
    broker = _broker()
    broker._ws_subscribed_tokens = set(INDEX_TOKENS + [OPTION_TOKEN])

    broker._init_websocket_feed("access-token")

    ticker = broker._ws_feed._ticker
    assert sorted(ticker.subscribed) == INDEX_TOKENS
    assert broker._ws_subscribed_tokens == set(INDEX_TOKENS)


def test_option_token_subscribes_after_a_mid_session_relogin(fake_ws):
    broker = _broker()
    broker._ws_subscribed_tokens = {OPTION_TOKEN}

    broker._init_websocket_feed("access-token")
    assert broker.subscribe_to_instrument(OPTION_TOKEN, mode="full") is True

    ticker = broker._ws_feed._ticker
    assert ticker.subscribed.count(OPTION_TOKEN) == 1
    assert (ticker.MODE_FULL, (OPTION_TOKEN,)) in ticker.modes


def test_same_socket_still_skips_a_duplicate_subscribe(fake_ws):
    broker = _broker()
    broker._init_websocket_feed("access-token")

    assert broker.subscribe_to_instrument(OPTION_TOKEN) is True
    assert broker.subscribe_to_instrument(OPTION_TOKEN) is True

    assert broker._ws_feed._ticker.subscribed.count(OPTION_TOKEN) == 1
