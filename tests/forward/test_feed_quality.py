"""Feed-quality monitor + broker bar feed tests (2026-09-25).

Covers:
* FeedQualityMonitor — staleness, gaps, stale repeats, error rates, and the
  restart-proof JSONL aggregation (aggregate_report).
* DhanBarFeed — poll loop behaviour identical to MStockBarFeed (dedupe,
  normalization) against a fake client, with quality observations recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backtest.forward.feed_quality import (
    FeedQualityMonitor,
    aggregate_report,
    reset_quality_monitor,
)
from backtest.forward.feed_registry import DhanBarFeed, MStockBarFeed


@pytest.fixture(autouse=True)
def _clean_monitors(tmp_path, monkeypatch):
    """Isolate every test from the real data/feed_quality.log: all process-
    wide monitors created during a test write to a temp file."""
    from backtest.forward import feed_quality as fq

    monkeypatch.setattr(fq, "_MONITORS_LOG_PATH", str(tmp_path / "q.log"))
    reset_quality_monitor()
    yield
    reset_quality_monitor()


# ---------------------------------------------------------------------------
# FeedQualityMonitor
# ---------------------------------------------------------------------------


class TestFeedQualityMonitor:
    def test_staleness_computed_from_bar_ts(self, tmp_path):
        mon = FeedQualityMonitor("mstock", "NIFTY", log_path=str(tmp_path / "q.log"))
        now = datetime(2026, 9, 25, 10, 0, 0)
        mon.observe_bar("2026-09-25 09:58:30", observed_at=now)  # 90s old
        s = mon.summary()
        assert s["bars_observed"] == 1
        assert s["staleness_median_s"] == 90.0

    def test_gap_detected_on_missing_minutes(self, tmp_path):
        mon = FeedQualityMonitor("mstock", "NIFTY", log_path=str(tmp_path / "q.log"))
        base = datetime(2026, 9, 25, 10, 0, 0)
        mon.observe_bar("2026-09-25 10:00:00", observed_at=base)
        mon.observe_bar("2026-09-25 10:01:00", observed_at=base + timedelta(minutes=1))
        mon.observe_bar("2026-09-25 10:04:00", observed_at=base + timedelta(minutes=4))
        s = mon.summary()
        assert s["gap_count"] == 1
        assert s["gap_minutes_total"] == 2.0  # 3-min jump minus the 1 expected

    def test_stale_repeat_counted(self, tmp_path):
        mon = FeedQualityMonitor("dhan", "NIFTY", log_path=str(tmp_path / "q.log"))
        base = datetime(2026, 9, 25, 10, 0, 0)
        mon.observe_bar("2026-09-25 10:00:00", observed_at=base)
        mon.observe_bar("2026-09-25 10:00:00", observed_at=base + timedelta(minutes=1))
        mon.observe_bar("2026-09-25 10:00:00", observed_at=base + timedelta(minutes=2))
        s = mon.summary()
        assert s["stale_repeat_bars"] == 2
        assert s["max_stale_repeat_run"] == 2

    def test_errors_tracked_with_rate(self, tmp_path):
        mon = FeedQualityMonitor("mstock", "NIFTY", log_path=str(tmp_path / "q.log"))
        mon.observe_error("http 429")
        mon.observe_error("timeout")
        s = mon.summary()
        assert s["errors_observed"] == 2
        assert s["error_rate_per_hour"] is None  # no bar window yet

    def test_epoch_ms_bar_ts_parsed(self, tmp_path):
        mon = FeedQualityMonitor("dhan", "BANKNIFTY", log_path=str(tmp_path / "q.log"))
        # 2026-09-25 04:30:00 UTC = 10:00 IST → epoch ms
        epoch_ms = 1790000000000
        mon.observe_bar(epoch_ms, observed_at=datetime(2026, 9, 25, 10, 0, 0))
        s = mon.summary()
        assert s["bars_observed"] == 1
        assert s["last_bar_ts"] is not None

    def test_unparseable_ts_recorded_as_error(self, tmp_path):
        mon = FeedQualityMonitor("mstock", "NIFTY", log_path=str(tmp_path / "q.log"))
        mon.observe_bar("not-a-date")
        assert mon.summary()["errors_observed"] == 1

    def test_log_written_as_jsonl(self, tmp_path):
        log = tmp_path / "q.log"
        mon = FeedQualityMonitor("mstock", "NIFTY", log_path=str(log))
        mon.observe_bar("2026-09-25 10:00:00", observed_at=datetime(2026, 9, 25, 10, 0, 0))
        mon.observe_error("boom")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        import json

        recs = [json.loads(l) for l in lines]
        assert recs[0]["kind"] == "bar"
        assert recs[0]["broker"] == "mstock"
        assert recs[1]["kind"] == "error"


class TestAggregateReport:
    def test_report_recomputes_from_log(self, tmp_path):
        log = tmp_path / "q.log"
        now = datetime(2026, 9, 25, 10, 0, 0)
        m1 = FeedQualityMonitor("mstock", "NIFTY", log_path=str(log))
        m2 = FeedQualityMonitor("dhan", "NIFTY", log_path=str(log))
        m1.observe_bar("2026-09-25 10:00:00", observed_at=now)
        m1.observe_bar("2026-09-25 10:05:00", observed_at=now + timedelta(minutes=5))
        m1.observe_error("429")
        m2.observe_bar("2026-09-25 10:00:00", observed_at=now)
        m2.observe_bar("2026-09-25 10:01:00", observed_at=now + timedelta(minutes=1))
        report = aggregate_report(str(log))
        feeds = {f["broker"]: f for f in report["feeds"]}
        assert feeds["mstock"]["errors_observed"] == 1
        assert feeds["mstock"]["gap_count"] == 1
        assert feeds["dhan"]["gap_count"] == 0
        assert feeds["dhan"]["bars_observed"] == 2

    def test_report_empty_on_missing_file(self, tmp_path):
        report = aggregate_report(str(tmp_path / "missing.log"))
        assert report["feeds"] == []


# ---------------------------------------------------------------------------
# DhanBarFeed — poll loop parity with MStockBarFeed
# ---------------------------------------------------------------------------


class _FakeDhanClient:
    """Duck-typed DhanLiveFeed: scripted latest_bar responses."""

    def __init__(self, bars=None, error_on=None):
        self.bars = bars or {}
        self.error_on = error_on or set()
        self.calls: list[str] = []

    def latest_bar(self, symbol: str):
        self.calls.append(symbol)
        if symbol in self.error_on:
            raise RuntimeError("simulated api failure")
        return self.bars.get(symbol)


def _bar(ts: str, close: float = 100.0) -> dict:
    return {"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10}


class TestDhanBarFeed:
    def test_broker_name_and_defaults(self):
        feed = DhanBarFeed(feed_client=object())
        assert feed.broker_name == "dhan"

    def test_poll_delivers_and_dedupes(self, monkeypatch):
        monkeypatch.setattr(
            "backtest.forward.feed_registry.DhanBarFeed._market_open",
            staticmethod(lambda: True),
        )
        client = _FakeDhanClient({"NIFTY": _bar("2026-09-25 10:00:00")})
        feed = DhanBarFeed(feed_client=client)
        got: list[tuple[str, dict]] = []
        feed.on_bar = lambda sym, bar: got.append((sym, bar))
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 1
        assert feed._poll_once() == 0  # same ts — deduped
        client.bars["NIFTY"] = _bar("2026-09-25 10:01:00", close=101)
        assert feed._poll_once() == 1
        assert [b["ts"] for _, b in got] == ["2026-09-25 10:00:00", "2026-09-25 10:01:00"]
        assert got[0][0] == "NIFTY"

    def test_poll_records_quality_observations(self, monkeypatch, tmp_path):
        from backtest.forward import feed_quality as fq

        fq._MONITORS_LOG_PATH = str(tmp_path / "q.log")
        monkeypatch.setattr(
            "backtest.forward.feed_registry.DhanBarFeed._market_open",
            staticmethod(lambda: True),
        )
        client = _FakeDhanClient({"NIFTY": _bar("2026-09-25 10:00:00")}, error_on={"BANKNIFTY"})
        feed = DhanBarFeed(feed_client=client)
        feed.add_symbols(["NIFTY", "BANKNIFTY"])
        feed._poll_once()
        nifty = fq.get_quality_monitor("dhan", "NIFTY").summary()
        bank = fq.get_quality_monitor("dhan", "BANKNIFTY").summary()
        assert nifty["bars_observed"] == 1
        assert bank["errors_observed"] == 1

    def test_market_closed_idles_after_seed(self, monkeypatch):
        monkeypatch.setattr(
            "backtest.forward.feed_registry.DhanBarFeed._market_open",
            staticmethod(lambda: False),
        )
        client = _FakeDhanClient({"NIFTY": _bar("2026-09-25 10:00:00")})
        feed = DhanBarFeed(feed_client=client)
        got: list[dict] = []
        feed.on_bar = lambda sym, bar: got.append(bar)
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 1  # startup catch-up seed
        assert feed._poll_once() == 0  # then idles
        assert client.calls.count("NIFTY") == 1
        assert len(got) == 1


class TestMStockBarFeedParity:
    def test_refactor_preserves_behaviour(self, monkeypatch):
        """The base-class refactor must keep MStockBarFeed semantics intact."""
        monkeypatch.setattr(
            "backtest.forward.feed_registry.MStockBarFeed._market_open",
            staticmethod(lambda: True),
        )
        client = _FakeDhanClient({"NIFTY": _bar("2026-09-25 10:00:00")})
        feed = MStockBarFeed(feed_client=client)
        got: list[dict] = []
        feed.on_bar = lambda sym, bar: got.append(bar)
        feed.add_symbols(["NIFTY"])
        assert feed._poll_once() == 1
        assert got[0]["close"] == pytest.approx(100.0)
        assert got[0]["ts"] == "2026-09-25 10:00:00"
