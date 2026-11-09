"""ENABLE_FEED packet capture: off by default, lossless when on, and never in the way.

Incident under test: 2026-11-08 Muhurat, ``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md``.
A put was bought back at an ask of ₹493.65 whose own packet reported last traded
₹367.00, and nothing we own recorded the ladder it came from — so whether mStock
published a garbage offer side or we misread a good one is still unproven. These tests
pin the three properties the recorder exists to provide: it captures every level the
broker sent, it is silent unless asked, and a capture failure cannot cost a fill.
"""

from __future__ import annotations

import json
import os

import pytest

from backtest.data.feed_depth_recorder import (
    FeedDepthRecorder,
    capture_dir,
    flag_enabled,
    get_recorder,
    record_depth_packet,
    record_price_used,
    reset_recorder,
)
from backtest.data.mstock_ws_wrapper import MStockWebSocketWrapper
from backtest.options.quote_providers import BidAskQuoteProvider

TOKEN = "64442"


def _levels(side: str, first: float) -> list[dict]:
    return [
        {"price": first + i * 0.05, "quantity": 65 * (i + 1)} for i in range(5)
    ]


def _row(bid: float = 320.85, ask: float = 493.65, ltp: float = 367.0) -> dict:
    return {
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "volume": 5_981_950,
        "oi": 54_465.0,
        "ohlc": {},
        "depth": {"bid": _levels("bid", bid), "ask": _levels("ask", ask)},
        "timestamp": "2026-11-08T14:20:14+05:30",
        "recv_mono": 1234.5,
        "recv_utc": "2026-11-08T08:50:14+00:00",
        "source": "ws",
        "mode": "full",
        "ladder_status": "ok",
        "ltp_mono": 1234.5,
        "ladder_mono": 1234.5,
    }


def _lines(recorder: FeedDepthRecorder) -> list[dict]:
    recorder.close()
    out = []
    for name in sorted(os.listdir(recorder.directory)):
        with open(os.path.join(recorder.directory, name), encoding="utf-8") as handle:
            out.extend(json.loads(line) for line in handle if line.strip())
    return out


@pytest.fixture
def flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_FEED", "true")
    monkeypatch.setenv("ENABLE_FEED_DIR", str(tmp_path))
    reset_recorder()
    yield tmp_path
    reset_recorder()


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv("ENABLE_FEED", raising=False)
    reset_recorder()
    yield
    reset_recorder()


# -- the flag ---------------------------------------------------------------


def test_off_by_default_and_silent(flag_off, tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_FEED_DIR", str(tmp_path))
    assert flag_enabled() is False
    assert get_recorder() is None
    record_depth_packet("mstock", _row(), token=TOKEN)
    record_price_used(_row(), broker="live:mstock+bidask", token=TOKEN, side="BUY", field="ask")
    assert list(tmp_path.iterdir()) == []  # no directory, no file, nothing touched


def test_flag_spellings(monkeypatch):
    for yes in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("ENABLE_FEED", yes)
        assert flag_enabled() is True
    for no in ("", "0", "off", "false", "no"):
        monkeypatch.setenv("ENABLE_FEED", no)
        assert flag_enabled() is False
    assert capture_dir({}) == os.path.join("logs", "feed_depth")


# -- what a packet line holds ------------------------------------------------


def test_packet_keeps_all_five_levels_per_side(flag_on):
    recorder = get_recorder()
    assert recorder is not None
    record_depth_packet("mstock", _row(), token=TOKEN)
    lines = [line for line in _lines(recorder) if line["kind"] == "packet"]
    assert len(lines) == 1
    line = lines[0]
    assert line["broker"] == "mstock" and line["token"] == TOKEN
    assert line["ltp"] == 367.0 and line["bid"] == 320.85 and line["ask"] == 493.65
    assert len(line["bids"]) == 5 and len(line["asks"]) == 5
    assert line["asks"][0] == [pytest.approx(493.65), 65]
    assert line["ladder_status"] == "ok" and line["mode"] == "full"
    # Both clocks: the exchange's own stamp and the instant it reached us.
    assert line["exchange_ts"] == "2026-11-08T14:20:14+05:30"
    assert line["recv_utc"].endswith("+00:00")


def test_raw_ladder_survives_our_level_hygiene(flag_on):
    """Zero-price/zero-qty levels are dropped from the row — the capture keeps them."""
    recorder = get_recorder()
    raw = {
        "bid": [{"price": 320.85, "quantity": 65}, {"price": 0, "quantity": 0}],
        "ask": [{"price": 493.65, "quantity": 65}],
    }
    record_depth_packet("mstock", _row(), token=TOKEN, raw_depth=raw)
    line = next(ln for ln in _lines(recorder) if ln["kind"] == "packet")
    assert line["raw_depth"]["bid"][1] == {"price": 0, "quantity": 0}


def test_price_used_names_the_field_the_fill_came_from(flag_on):
    recorder = get_recorder()
    record_price_used(
        _row(), broker="live:mstock+bidask", token=TOKEN, side="BUY", field="ask"
    )
    line = next(ln for ln in _lines(recorder) if ln["kind"] == "price_used")
    assert line["price_used"] == 493.65
    assert line["field"] == "ask" and line["side"] == "BUY"
    assert line["ltp"] == 367.0  # the number the ₹493.65 offer contradicted
    assert len(line["asks"]) == 5


# -- capture sits on the trading path, so it must not behave like one ---------


def test_unwritable_capture_never_raises(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    recorder = FeedDepthRecorder(str(blocker / "nested"))
    for _ in range(5):
        record = recorder.record({"broker": "mstock", "kind": "packet"})
        assert record is None
    recorder.close()  # must not raise
    assert not (blocker / "nested").exists()
    # A failed write is a counted drop, not a swallowed debug line.
    assert recorder.dropped == 5
    assert "write failed" in caplog.text


def test_capture_tolerates_unserialisable_fields(flag_on):
    recorder = get_recorder()
    row = _row()
    row["oi"] = object()  # broker clients hand back all sorts
    record_depth_packet("mstock", row, token=TOKEN)
    line = next(ln for ln in _lines(recorder) if ln["kind"] == "packet")
    assert isinstance(line["oi"], str)


# -- the two live hook points -------------------------------------------------


class _FakeSocket:
    """Stands in for MTicker: the wrapper installs ``on_ticks`` on us."""

    MODE_FULL = "full"

    def __init__(self) -> None:
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None

    def connect(self, *args, **kwargs) -> None:  # pragma: no cover - unused
        pass


def test_mstock_wrapper_captures_every_packet_it_caches(flag_on):
    socket = _FakeSocket()
    wrapper = MStockWebSocketWrapper(socket)
    tick = {
        "instrument_token": TOKEN,
        "last_price": 367.0,
        "volume_traded": 100,
        "open_interest": 55,
        "ohlc": {},
        "mode": "full",
        "exchange_timestamp": "2026-11-08T14:20:14+05:30",
        "depth": {"bid": _levels("bid", 320.85), "ask": _levels("ask", 493.65)},
    }
    for _ in range(3):  # repeated identical frames: signature logging would skip these
        socket.on_ticks(socket, [tick])
    assert wrapper.get_quote(TOKEN)["ask"] == pytest.approx(493.65)
    lines = [line for line in _lines(get_recorder()) if line["kind"] == "packet"]
    assert len(lines) == 3
    assert {line["broker"] for line in lines} == {"mstock"}


class _Inner:
    source_name = "live:mstock"

    def __init__(self, row: dict) -> None:
        self._row = row

    def get_quote(self, instrument_token: str) -> dict:
        return dict(self._row)


def test_the_seam_records_the_ask_it_turned_into_a_fill(flag_on):
    provider = BidAskQuoteProvider(_Inner(_row()))  # type: ignore[arg-type]
    quote = provider.execution_quote(TOKEN, "BUY")
    assert quote["ltp"] == pytest.approx(493.65)  # the fill price, per the current design
    line = next(ln for ln in _lines(get_recorder()) if ln["kind"] == "price_used")
    assert line["price_used"] == pytest.approx(493.65)
    assert line["field"] == "ask" and line["token"] == TOKEN
    # The seam's label is a provider string, not a filesystem name: ``live:mstock``
    # used to become ``feed_live`` and the colon made the write fail silently.
    assert line["broker"] == "mstock"
    assert line["source_label"] == "live:mstock+bidask"
