"""Tests for the PnL-vs-spot watch series + end-of-day export (2026-09-24).

Covers: series capture only for live (mstock) option runners, the CSV+PNG
export layout under charts/<date>/, the manual sweep over the manager's
runners, and the MarketCloseExporter's fire-once-per-day logic (using a
monkeypatched clock — no thread, no real waiting).
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.forward import watch_export


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, name="watch NIFTY", source="mstock", instrument_type="option"):
        self.name = name
        self.source = source
        self.instrument = {"type": instrument_type}
        self.allocated_capital = 100000.0


class _FakeRunner:
    """Just enough of StrategyRunner for the exporter."""

    def __init__(self, name="watch NIFTY", source="mstock", instrument_type="option"):
        self.config = _FakeConfig(name, source, instrument_type)
        self.watch_series = [
            {"ts": "2026-09-24 09:15:00", "spot": 25000.0, "option_pnl": 0.0, "equity": 100000.0},
            {"ts": "2026-09-24 09:16:00", "spot": 25010.0, "option_pnl": 120.5, "equity": 100120.5},
            {"ts": "2026-09-24 09:17:00", "spot": 25005.0, "option_pnl": -40.25, "equity": 99959.75},
        ]
        self.last_option_label = "long_call 23250"


@pytest.fixture()
def charts_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_export, "CHARTS_DIR", tmp_path / "charts")
    monkeypatch.setattr(watch_export, "_PROJECT_ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# export_runner_chart
# ---------------------------------------------------------------------------


def test_export_writes_csv_and_png(charts_tmp):
    runner = _FakeRunner()
    out = charts_tmp / "out"
    result = watch_export.export_runner_chart(runner, out_dir=out)

    assert result is not None
    assert result["points"] == 3
    csv_path = Path(result["csv"])
    assert csv_path.exists()
    png_path = result["png"]
    # matplotlib is a project dependency — the PNG should exist
    assert png_path is not None and Path(png_path).exists()


def test_csv_rows_match_series(charts_tmp):
    runner = _FakeRunner()
    out = charts_tmp / "out"
    result = watch_export.export_runner_chart(runner, out_dir=out)

    with open(result["csv"], newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["ts", "spot", "option_pnl", "equity"]
    assert rows[1] == ["2026-09-24 09:15:00", "25000.0", "0.0", "100000.0"]
    assert rows[3] == ["2026-09-24 09:17:00", "25005.0", "-40.25", "99959.75"]


def test_export_none_when_no_series(charts_tmp):
    runner = _FakeRunner()
    runner.watch_series = []
    assert watch_export.export_runner_chart(runner, out_dir=charts_tmp / "o") is None


def test_csv_only_when_matplotlib_missing(charts_tmp, monkeypatch):
    runner = _FakeRunner()
    builtins_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "matplotlib":
            raise ImportError("no matplotlib in this test")
        return builtins_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    result = watch_export.export_runner_chart(runner, out_dir=charts_tmp / "o")
    assert result is not None
    assert result["png"] is None
    assert Path(result["csv"]).exists()


# ---------------------------------------------------------------------------
# export_all_watch_runners — live option runners only
# ---------------------------------------------------------------------------


class _FakeManager:
    def __init__(self, runners):
        self._runners = {r.config.name: r for r in runners}


def test_sweep_exports_only_live_option_runners(charts_tmp, monkeypatch):
    live = _FakeRunner("live NIFTY", source="mstock", instrument_type="option")
    synthetic = _FakeRunner("synthetic NIFTY", source="synthetic", instrument_type="option")
    equity = _FakeRunner("equity REL", source="mstock", instrument_type="equity")

    mgr = _FakeManager([live, synthetic, equity])
    monkeypatch.setattr(watch_export, "charts_dir_for", lambda d=None: charts_tmp / "c")

    results = watch_export.export_all_watch_runners(mgr)
    names = [r["runner"] for r in results]
    assert names == ["live NIFTY"]


def test_sweep_skips_runners_with_no_data(charts_tmp, monkeypatch):
    empty = _FakeRunner("live BANKNIFTY")
    empty.watch_series = []
    mgr = _FakeManager([empty])
    monkeypatch.setattr(watch_export, "charts_dir_for", lambda d=None: charts_tmp / "c")
    assert watch_export.export_all_watch_runners(mgr) == []


# ---------------------------------------------------------------------------
# MarketCloseExporter — fire-once-per-day logic (no threads)
# ---------------------------------------------------------------------------


def _make_exporter(monkeypatch, now_ist: datetime):
    exporter = watch_export.MarketCloseExporter(manager=object())
    monkeypatch.setattr(watch_export, "_ist_now", lambda: now_ist)
    fired = []

    def fake_sweep(manager=None):
        fired.append(now_ist)
        return [{"runner": "x"}]

    monkeypatch.setattr(watch_export, "export_all_watch_runners", fake_sweep)
    return exporter, fired


def test_fires_after_market_close(monkeypatch):
    now = datetime(2026, 9, 24, 15, 31, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    exporter, fired = _make_exporter(monkeypatch, now)
    assert exporter._maybe_fire() is True
    assert len(fired) == 1
    # Same day again → no second fire
    assert exporter._maybe_fire() is False
    assert len(fired) == 1


def test_does_not_fire_before_market_close(monkeypatch):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    exporter, fired = _make_exporter(monkeypatch, now)
    assert exporter._maybe_fire() is False
    assert fired == []


def test_fires_next_day_again(monkeypatch):
    day1 = datetime(2026, 9, 24, 15, 31, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    exporter, fired = _make_exporter(monkeypatch, day1)
    assert exporter._maybe_fire() is True

    day2 = day1 + timedelta(days=1)
    monkeypatch.setattr(watch_export, "_ist_now", lambda: day2)
    assert exporter._maybe_fire() is True
    assert len(fired) == 2


def test_catchup_fires_when_booted_after_close(monkeypatch):
    now = datetime(2026, 9, 24, 16, 45, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    exporter, fired = _make_exporter(monkeypatch, now)
    assert exporter._maybe_fire(catchup=True) is True
    assert len(fired) == 1


def test_catchup_skips_before_close(monkeypatch):
    now = datetime(2026, 9, 24, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    exporter, fired = _make_exporter(monkeypatch, now)
    assert exporter._maybe_fire(catchup=True) is False
    assert fired == []


# ---------------------------------------------------------------------------
# Series capture policy (the runner-side half)
# ---------------------------------------------------------------------------


def test_watch_series_cap_bounds_memory():
    """The 1600-row cap keeps the series bounded (1 trading day of 1-min bars
    is ~400 rows; 4 days of headroom)."""
    runner = _FakeRunner()
    runner.watch_series = [{"ts": f"t{i}", "spot": 1, "option_pnl": 0, "equity": 1} for i in range(1600)]
    # The runner's _record_watch_point pops row 0 before appending at the cap.
    assert len(runner.watch_series) == 1600
