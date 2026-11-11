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
from types import SimpleNamespace

import pytest

from backtest.data.feed_depth_recorder import (
    FeedDepthRecorder,
    capture_dir,
    day_cap,
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
    # Bids step down with depth, asks up — a folded ladder is not a book a venue
    # posts, and the wrapper now refuses one instead of caching it.
    step = 0.05 if side == "ask" else -0.05
    return [
        {"price": first + i * step, "quantity": 65 * (i + 1)} for i in range(5)
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


def test_the_day_cap_clears_a_full_session(flag_on, monkeypatch):
    """64 MB used to stop the tape around 10:15 — before the exits it exists to witness."""
    assert day_cap({}) == 512 * 1024 * 1024
    assert day_cap({"ENABLE_FEED_MAX_DAY_BYTES": "1048576"}) == 1_048_576
    assert day_cap({"ENABLE_FEED_MAX_DAY_BYTES": "0"}) == 512 * 1024 * 1024
    assert day_cap({"ENABLE_FEED_MAX_DAY_BYTES": "plenty"}) == 512 * 1024 * 1024
    assert get_recorder().max_day_bytes == 512 * 1024 * 1024
    monkeypatch.setenv("ENABLE_FEED_MAX_DAY_BYTES", "4096")
    reset_recorder()
    assert get_recorder().max_day_bytes == 4096


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


def test_the_venue_last_trade_stamp_reaches_the_tape(flag_on):
    """T0.5-0: the tape must carry ``last_trade_ts`` (Upstox ``ltpc.ltt``) so the
    currentTs-vs-ltt measurement (impl plan T0.5-1) has both stamps to compare.

    Reading one written line is the point — a writer that stores the field in its
    dict but drops it at write time is exactly what finding A caught.
    """
    recorder = get_recorder()
    assert recorder is not None
    upstox_row = _row()
    upstox_row["last_trade_ts"] = "2026-11-11T09:19:13.812000+05:30"
    key = "NSE_FO|NIFTY|26NOV2026|25050|PE"
    record_depth_packet("upstox", upstox_row, token=key)
    line = next(ln for ln in _lines(recorder) if ln["kind"] == "packet" and ln["token"] == key)
    assert line["last_trade_ts"] == "2026-11-11T09:19:13.812000+05:30"

    reset_recorder()
    recorder = get_recorder()
    assert recorder is not None
    record_depth_packet("mstock", _row(), token=TOKEN)  # no last_trade_ts on mStock rows
    line = next(ln for ln in _lines(recorder) if ln["kind"] == "packet" and ln["token"] == TOKEN)
    assert line["last_trade_ts"] is None  # written as null, never a crash


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
    # The two numbers apart, which is the whole reason this row exists: the offer
    # the fill was priced from, and the last trade that offer contradicted. The
    # capture used to run after the seam overwrote ``ltp``, so both read 493.65.
    assert line["ltp"] == pytest.approx(367.0)
    assert line["asks"][0][0] == pytest.approx(493.65)
    # The seam's label is a provider string, not a filesystem name: ``live:mstock``
    # used to become ``feed_live`` and the colon made the write fail silently.
    assert line["broker"] == "mstock"
    assert line["source_label"] == "live:mstock+bidask"


class _NamedInner(_Inner):
    """A provider that can name the contract behind a token."""

    def contract_for(self, instrument_token: str):
        return SimpleNamespace(trading_symbol="NIFTY26NOV25100PE")


def test_the_fill_row_names_the_contract_not_just_the_token(flag_on):
    provider = BidAskQuoteProvider(_NamedInner(_row()))  # type: ignore[arg-type]
    provider.execution_quote(TOKEN, "BUY")
    line = next(ln for ln in _lines(get_recorder()) if ln["kind"] == "price_used")
    assert line["symbol"] == "NIFTY26NOV25100PE"
