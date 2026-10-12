"""Shared deterministic live-test fixtures for the ``forward`` test package.

Extracted from ``test_live_engine.py`` (STATUS P4 item 12b): ``test_bucket_risk``
used to import these directly from a sibling test module — a test-to-test
dependency that made collection order and refactors fragile. Both modules now
import from here.

Contents:
* :class:`FakeLiveBroker` — duck-typed venue (``place_order`` + ``poll_fill``,
  canned REAL fills; polls return ``None`` while unexecuted, the fill row is
  returned EXACTLY ONCE).
* :class:`ThresholdStrategy` — deterministic ``close > threshold`` signal.
* :func:`_bar` — numbered 1-based live bar (bar 1 = 2024-01-01).
* :func:`_run_live_until` — drives the REAL ``run_loop`` in a thread, injecting
  one bar per poll cycle (with duplicate injections to exercise dedupe).
"""

from __future__ import annotations

import threading
import time

import pandas as pd

from backtest.strategy.base import Strategy

# ---------------------------------------------------------------------------
# Deterministic live simulator
# ---------------------------------------------------------------------------


class FakeLiveBroker:
    """Duck-typed live broker: ``place_order`` + ``poll_fill``.

    Models a real venue honestly: polls return ``None`` while unexecuted;
    once the venue reports, the canned fill row is returned EXACTLY ONCE
    (a repeated row would over-fill the order — the executor treats every
    provider response as new execution).
    """

    def __init__(self, fill_row: dict | None, polls_before_fill: int = 1):
        self.fill_row = fill_row
        self.polls_before_fill = int(polls_before_fill)
        self.placed: list = []
        self.polled: list = []
        self._reported = False

    def place_order(self, order):
        self.placed.append(order)
        return f"BROKER-{len(self.placed)}"

    def poll_fill(self, broker_order_id):
        self.polled.append(broker_order_id)
        if self._reported or self.fill_row is None:
            return None
        if len(self.polled) <= self.polls_before_fill:
            return None
        self._reported = True
        return self.fill_row


class ThresholdStrategy(Strategy):
    name = ""
    params = {"threshold": 100}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "threshold_test"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return (candles["close"] > self.threshold).astype(int)


def _bar(bar_no: int, close: float, volume: int = 10_000):
    """Deterministic live bar, numbered 1-based (bar 1 = 2024-01-01)."""
    return {
        "symbol": "TEST",
        "timestamp": f"2024-01-{bar_no:02d}T09:15:00+05:30",
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": volume,
    }


def _run_live_until(engine, first_bar: int, n_bars: int, timeout: float = 15.0):
    """Drive the REAL ``run_loop`` in a thread, one bar per poll cycle.

    Each bar is injected and then the loop is given a moment to observe it
    (the live poll dedupes repeats, so advancing one bar at a time makes the
    per-bar decisions deterministic). Duplicates of every injected bar are
    also injected to exercise the streaming dedupe on every cycle.
    """
    engine._running = True
    thread = threading.Thread(target=engine.run_loop, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    for i in range(n_bars):
        bar_no = first_bar + i
        close = 99.0 if bar_no == 1 else 105.0  # exactly one 0→1 transition
        engine.data_handler.inject_bar(_bar(bar_no, close))
        # Wait until THIS bar was digested by the loop's dedupe (the adapter
        # appends exactly one row per NEW bar; repeats and re-polls add none).
        while time.monotonic() < deadline:
            bars = engine.adapter._bars.get("TEST")
            if bars is not None and len(bars) >= bar_no:
                break
            time.sleep(0.02)
        # A little extra time so the same bar is re-polled at least once
        # (dedupe exercise) and the executor can retry a working order.
        time.sleep(0.05)

    engine._running = False
    thread.join(timeout=5)
    assert not thread.is_alive(), "run_loop did not exit after _running=False"
