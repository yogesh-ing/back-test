"""Ticket P3.4 — MStockLiveFeed: the 60s mStock polling loop, re-homed.

Acceptance: the feed yields real-time bars. All HTTP is mocked (fake
client or patched requests) — no test touches the real API.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from backtest.data.mstock_live_feed import MStockLiveFeed, _market_open


class _FakeClient:
    """Duck-typed mstock_client: scripted bars, no HTTP, no credentials."""

    def __init__(self, bars: list[dict] | None = None, candles_frame=None):
        self.bars = list(bars or [])
        self.candles_frame = candles_frame
        self.latest_calls: list[str] = []

    def get_latest_bar(self, symbol: str):
        self.latest_calls.append(symbol)
        return self.bars.pop(0) if self.bars else None

    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day"):
        return self.candles_frame


def _bar(ts: str, close: float) -> dict:
    return {
        "ts": ts,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000,
    }


def test_iter_bars_yields_realtime_bars():
    """The ticket's acceptance: the feed yields real-time bars."""
    client = _FakeClient([_bar(f"2024-01-02 09:1{i}:00", 100 + i) for i in range(3)])
    feed = MStockLiveFeed(mstock_client=client, poll_interval_s=60)

    bars = list(feed.iter_bars("RELIANCE", max_bars=3, market_gate=False, sleep=lambda s: None))

    assert [b["ts"] for b in bars] == [
        "2024-01-02 09:10:00",
        "2024-01-02 09:11:00",
        "2024-01-02 09:12:00",
    ]
    assert [b["close"] for b in bars] == [100, 101, 102]
    assert client.latest_calls == ["RELIANCE"] * 3
    assert feed.interval == 60.0  # the old engine's 60s cadence is the default


def test_poll_interval_is_configurable():
    feed = MStockLiveFeed(mstock_client=_FakeClient(), poll_interval_s=5)
    assert feed.interval == 5.0


def test_iter_bars_skips_duplicate_bars():
    """A retried poll returning the same bar must not double-feed it."""
    seen: list[str] = []
    client = _FakeClient([_bar("T1", 100), _bar("T1", 100), _bar("T2", 101), _bar("T2", 101)])
    feed = MStockLiveFeed(mstock_client=client)

    def stop_check():
        stop_check.n = getattr(stop_check, "n", 0) + 1
        return stop_check.n > 10

    for bar in feed.iter_bars(
        "DEMO", stop_check=stop_check, market_gate=False, sleep=lambda s: None
    ):
        seen.append(bar["ts"])

    assert seen == ["T1", "T2"]  # duplicates skipped


def test_iter_bars_stops_on_stop_check():
    client = _FakeClient([_bar(f"T{i}", 100 + i) for i in range(10)])
    feed = MStockLiveFeed(mstock_client=client)

    def stop_check():
        stop_check.n = getattr(stop_check, "n", 0) + 1
        return stop_check.n > 2  # allow 2 polls, then stop

    bars = list(
        feed.iter_bars("DEMO", stop_check=stop_check, market_gate=False, sleep=lambda s: None)
    )
    assert len(bars) == 2


def test_market_gate_idles_without_fetching_when_closed(monkeypatch):
    import backtest.data.mstock_live_feed as feed_mod

    monkeypatch.setattr(feed_mod, "_market_open", lambda now_utc=None: False)
    client = _FakeClient([_bar("T1", 100)])
    feed = MStockLiveFeed(mstock_client=client)

    def stop_check():
        stop_check.n = getattr(stop_check, "n", 0) + 1
        return stop_check.n > 3

    sleeps: list[float] = []
    bars = list(feed.iter_bars("DEMO", stop_check=stop_check, sleep=sleeps.append))

    assert bars == []  # nothing fetched while closed
    assert client.latest_calls == []
    assert all(s == 60.0 for s in sleeps)


def test_get_candles_uses_client_frame_when_available():
    frame = pd.DataFrame(
        {"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2], "volume": [1, 1]},
        index=pd.DatetimeIndex(["2024-01-01 09:15", "2024-01-01 09:16"]),
    )
    feed = MStockLiveFeed(mstock_client=_FakeClient(candles_frame=frame))
    out = feed.get_candles("RELIANCE", "2024-01-01", "2024-01-01", "1min")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert len(out) == 2


def test_get_candles_without_credentials_returns_empty_frame(monkeypatch):
    """Construction without credentials stays safe; an uncredentialed query
    degrades to an empty frame (logged), not a crash."""
    monkeypatch.delenv("MSTOCK_API_KEY", raising=False)
    feed = MStockLiveFeed()
    out = feed.get_candles("RELIANCE", "2024-01-01", "2024-01-02", "1min")
    assert out.empty
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_get_candles_parses_http_candles(monkeypatch):
    """The HTTP path (no client) parses mStock candle rows into a frame."""
    monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")
    monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")

    import backtest.data.mstock_live_feed as feed_mod

    # Auth + instrument resolution
    monkeypatch.setattr("backtest.live.auth.get_session_token", lambda: "sess-token")
    monkeypatch.setattr(feed_mod, "_resolve_security_token", lambda *a, **k: "54321")

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "candles": [
                        ["2024-01-02 09:15:00", 100.0, 101.0, 99.5, 100.5, 1000],
                        ["2024-01-02 09:16:00", 100.5, 102.0, 100.0, 101.5, 2000],
                    ]
                }
            }

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())

    feed = MStockLiveFeed(base_url="https://api.mstock.test")
    out = feed.get_candles("RELIANCE", "2024-01-02", "2024-01-02", "1min")

    assert len(out) == 2
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert float(out["close"].iloc[-1]) == 101.5


def test_market_open_boundaries():
    from datetime import timedelta, timezone

    wed = datetime(2024, 1, 3, tzinfo=timezone.utc)  # a Wednesday
    # 09:15 IST = 03:45 UTC
    assert _market_open(wed + timedelta(hours=3, minutes=46)) is True
    assert _market_open(wed + timedelta(hours=10, minutes=1)) is False  # 15:31 IST — closed
    friday = datetime(2024, 1, 5, tzinfo=timezone.utc)
    assert _market_open(friday + timedelta(hours=20)) is False  # Saturday IST


# ---------------------------------------------------------------------------
# Equity live-bar path — 2026-10-01: RELIANCE runners froze on the prior
# day's close. First attempt served equities from quote/ohlc under
# "NSE:<TRADINGSYMBOL>", but that catalog is index-only: mStock answers
# equities with HTTP 202 + "Invalid symbol.'RELIANCE' Please verify the
# exchange and scrip name" forever (verified live). Equities are served by
# the intraday-chart endpoint, keyed by scriptmaster instrument_token.
# ---------------------------------------------------------------------------


def test_latest_bar_serves_equity_intraday_bar(monkeypatch):
    monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")
    monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")
    monkeypatch.setattr("backtest.live.auth.get_session_token", lambda: "sess")

    import backtest.data.mstock_live_feed as feed_mod

    monkeypatch.setattr(
        feed_mod,
        "_resolve_scriptmaster_row",
        lambda *a, **k: {"exchange": "NSE", "instrument_token": 2885},
    )

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            # Newest-first, exactly as the live endpoint returned it.
            return {
                "data": {
                    "candles": [
                        ["2026-10-01T12:10:00+05", 1176.2, 1176.2, 1174.8, 1175.4, 12228],
                        ["2026-10-01T12:09:00+05", 1175.0, 1176.5, 1174.0, 1176.2, 20000],
                    ]
                }
            }

    def fake_get(url, *a, **k):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr("requests.get", fake_get)

    feed = MStockLiveFeed(base_url="https://api.mstock.test")
    bar = feed.latest_bar("RELIANCE")

    assert bar is not None
    assert bar["close"] == 1175.4 and bar["volume"] == 12228  # newest candle
    assert captured["url"].endswith("/instruments/intraday/1/2885/minute")
    # quote/ohlc must NOT be probed for equities (it only ever 404s there).
    assert "/quote/ohlc" not in captured["url"]


def test_equity_intraday_key_defaults_to_nse_on_resolution_failure(monkeypatch):
    """If scriptmaster row lookup fails, fall back to the NSE segment with
    the security-token resolution rather than dropping the symbol (so the
    poll thread keeps producing bars)."""
    monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")
    monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")
    monkeypatch.setattr("backtest.live.auth.get_session_token", lambda: "sess")

    import backtest.data.mstock_live_feed as feed_mod

    def boom(*a, **k):
        raise ValueError("symbol not in scriptmaster")

    monkeypatch.setattr(feed_mod, "_resolve_scriptmaster_row", boom)
    monkeypatch.setattr(feed_mod, "_resolve_security_token", lambda *a, **k: "9999")

    feed = MStockLiveFeed(base_url="https://api.mstock.test")
    assert feed._intraday_key_for("TATAMOTORS") == ("1", "9999")


def test_index_quote_map_is_preferred_over_equity_resolution(monkeypatch):
    """An index resolves via the curated map; scriptmaster must never be hit."""
    monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")
    monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")
    monkeypatch.setattr("backtest.live.auth.get_session_token", lambda: "sess")

    import backtest.data.mstock_live_feed as feed_mod
    from backtest.data.mstock_live_feed import QUOTE_SYMBOL_NAMES

    def never(*a, **k):
        raise AssertionError("scriptmaster must not be hit for an index")

    monkeypatch.setattr(feed_mod, "_resolve_scriptmaster_row", never)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"NSE:NIFTY 50": {"ohlc": {"open": 25000.0}, "last_price": 25010.0}}}

    captured: dict = {}

    def fake_get(url, *a, **k):
        captured["params"] = k.get("params")
        return _Resp()

    monkeypatch.setattr("requests.get", fake_get)

    feed = MStockLiveFeed()
    bar = feed._fetch_quote_bar("NIFTY")
    assert bar is not None and bar["close"] == 25010.0
    assert ("i", "NSE:NIFTY 50") in captured["params"]
    assert feed._intraday_keys == {}  # equity-resolution cache stayed empty
    assert QUOTE_SYMBOL_NAMES["NIFTY"] == "NSE:NIFTY 50"
