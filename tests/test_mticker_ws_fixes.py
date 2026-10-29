"""Regression tests for the three 2026-10-23 mStock WebSocket fixes.

Live symptoms these pin (paper session, NIFTY options):

1. **Thread-unsafe sends** — frames sent from runner/Flask threads raced
   Twisted's reactor; the ``set_mode(full)`` frame was silently lost, option
   tokens stayed in quote mode, and quotes stayed one-sided all day.
   ``_send_frame`` must marshal every text frame via ``reactor.callFromThread``.
2. **LOGIN-after-resubscribe** — on reconnect ``_on_open`` resubscribed
   *before* the on_open callback sent the mandatory ``LOGIN:`` frame, so the
   server clean-closed the socket (the ~3 s flap). LOGIN must fire first.
3. **Trailing-comma OI tuples** — the full-mode parser assigned
   ``open_interest`` as a tuple; the wrapper's ``float()`` then threw inside
   the reactor thread and Twisted killed the socket on every full-mode option
   tick — the feed went silent the moment the first option token subscribed.
"""

from __future__ import annotations

import json
import struct

import pytest

import backtest.data.mticker as mticker_module
from backtest.data.mticker import MTicker
from backtest.data.mstock_ws_wrapper import MStockWebSocketWrapper

OPTION_TOKEN = 51332  # NIFTY 27-Oct-2026 call — live-verified 2026-10-23


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendMessage(self, payload, is_binary=False) -> None:
        self.sent.append(payload)


class FakeReactor:
    def __init__(self, running: bool) -> None:
        self.running = running
        self.marshalled: list[tuple] = []

    def callFromThread(self, fn, *args) -> None:
        self.marshalled.append((fn, args))


def _ticker(ws: FakeWS | None = None) -> MTicker:
    mt = MTicker.__new__(MTicker)  # skip network/session __init__
    mt.ws = ws if ws is not None else FakeWS()
    mt.subscribed_tokens = {}
    mt.access_token = "tok"
    return mt


# ---------------------------------------------------------------------------
# Fix 1: every frame goes through reactor.callFromThread while the reactor runs
# ---------------------------------------------------------------------------


def test_frames_are_marshalled_onto_the_reactor_when_it_runs(monkeypatch):
    ws, reactor = FakeWS(), FakeReactor(running=True)
    monkeypatch.setattr(mticker_module, "reactor", reactor)
    mt = _ticker(ws)

    mt.subscribe([OPTION_TOKEN])
    mt.set_mode(MTicker.MODE_FULL, [OPTION_TOKEN])
    mt.send_login_after_connect()

    assert ws.sent == [], "no frame may touch the socket from a foreign thread"
    assert len(reactor.marshalled) == 3
    subscribe_payload = json.loads(reactor.marshalled[0][1][0].decode())
    mode_payload = json.loads(reactor.marshalled[1][1][0].decode())
    assert subscribe_payload == {"a": "subscribe", "v": [OPTION_TOKEN]}
    assert mode_payload == {"a": "mode", "v": ["full", [OPTION_TOKEN]]}
    assert reactor.marshalled[2][1][0] == b"LOGIN:tok"


def test_frames_go_straight_to_the_socket_when_the_reactor_is_not_running(monkeypatch):
    ws, reactor = FakeWS(), FakeReactor(running=False)
    monkeypatch.setattr(mticker_module, "reactor", reactor)
    mt = _ticker(ws)

    mt.subscribe([OPTION_TOKEN])

    assert reactor.marshalled == []
    assert json.loads(ws.sent[0].decode()) == {"a": "subscribe", "v": [OPTION_TOKEN]}


# ---------------------------------------------------------------------------
# Fix 2: LOGIN fires before resubscribe on reconnect
# ---------------------------------------------------------------------------


def test_reconnect_sends_login_before_resubscribing():
    mt = _ticker()
    order: list[str] = []
    mt.on_open = lambda _tkr: order.append("login")
    mt.resubscribe = lambda: order.append("resubscribe")  # type: ignore[method-assign]
    mt._is_first_connect = False

    mt._on_open(None)

    assert order == ["login", "resubscribe"]


def test_first_connect_logs_in_without_resubscribing():
    mt = _ticker()
    order: list[str] = []
    mt.on_open = lambda _tkr: order.append("login")
    mt.resubscribe = lambda: order.append("resubscribe")  # type: ignore[method-assign]
    mt._is_first_connect = True

    mt._on_open(None)

    assert order == ["login"]
    assert mt._is_first_connect is False


# ---------------------------------------------------------------------------
# Fix 3: full-mode OI fields are numbers, not tuples
# ---------------------------------------------------------------------------


def _full_mode_packet(size: int = 200) -> bytes:
    """A synthetic 184/200-byte full-mode tick for ``OPTION_TOKEN``."""
    packet = bytearray(size)
    struct.pack_into(">I", packet, 0, OPTION_TOKEN)
    struct.pack_into(">I", packet, 4, 15_250)  # ltp 152.50
    struct.pack_into(">I", packet, 8, 150)  # last traded quantity
    struct.pack_into(">I", packet, 12, 15_200)  # average traded price
    struct.pack_into(">I", packet, 16, 38_635_090)  # volume
    struct.pack_into(">I", packet, 20, 900)  # total buy quantity
    struct.pack_into(">I", packet, 24, 1_100)  # total sell quantity
    struct.pack_into(">I", packet, 28, 15_100)  # open
    struct.pack_into(">I", packet, 32, 15_400)  # high
    struct.pack_into(">I", packet, 36, 14_900)  # low
    struct.pack_into(">I", packet, 40, 15_150)  # close
    struct.pack_into(">I", packet, 44, 1_477_033_670)  # last traded ts
    struct.pack_into(">I", packet, 48, 4_764_955)  # open interest 47649.55
    struct.pack_into(">I", packet, 52, 4_800_000)  # oi high
    struct.pack_into(">I", packet, 56, 4_500_000)  # oi low
    struct.pack_into(">I", packet, 60, 1_477_033_670)  # exchange ts
    # depth: 10 entries x 12 bytes (qty, price, orders, padding) from byte 64
    for i in range(10):
        off = 64 + i * 12
        struct.pack_into(">I", packet, off, 150 * (i + 1))
        struct.pack_into(">I", packet, off + 4, 15_160 + i * 5)
        struct.pack_into(">H", packet, off + 8, 3)
        struct.pack_into(">H", packet, off + 10, 0)
    if size == 200:
        struct.pack_into(">I", packet, 184, 99_999)  # upper circuit
        struct.pack_into(">I", packet, 188, 1_000)  # lower circuit
        struct.pack_into(">I", packet, 192, 20_000)  # 52w high
        struct.pack_into(">I", packet, 196, 8_000)  # 52w low
    return bytes(packet)


def _parsed_tick(size: int = 200) -> dict:
    mt = MTicker.__new__(MTicker)
    mt.subscribed_tokens = {OPTION_TOKEN: MTicker.MODE_FULL}
    frame = struct.pack(">HH", 1, size) + _full_mode_packet(size)
    ticks = mt._parse_binary(frame)
    assert len(ticks) == 1
    return ticks[0]


@pytest.mark.parametrize("size", [184, 200])
def test_full_mode_open_interest_is_a_number_not_a_tuple(size):
    tick = _parsed_tick(size)

    assert tick["open_interest"] == pytest.approx(47_649.55)
    assert tick["open_interest_high"] == pytest.approx(48_000.0)
    assert tick["open_interest_low"] == pytest.approx(45_000.0)


def test_full_mode_depth_ladder_parses():
    tick = _parsed_tick(200)

    bids, asks = tick["depth"]["bid"], tick["depth"]["ask"]
    assert len(bids) == 5 and len(asks) == 5
    assert bids[0]["price"] == pytest.approx(151.6)
    assert asks[0]["price"] == pytest.approx(151.85)


def test_wrapper_normalize_tick_survives_a_full_mode_option_tick():
    tick = _parsed_tick(200)

    quote = MStockWebSocketWrapper._normalize_tick(tick)

    assert quote["oi"] == pytest.approx(47_649.55)
    assert quote["ltp"] == pytest.approx(152.5)
    assert quote["bid"] == pytest.approx(151.6)
    assert quote["ask"] == pytest.approx(151.85)
