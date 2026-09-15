"""Unit tests for the ``price_move`` strategy (registry + signal logic)."""

from __future__ import annotations

import pandas as pd

from backtest.strategies.price_move import PriceMove
from backtest.strategy.registry import get_strategy, list_strategies


def _candles(closes):
    idx = pd.date_range("2026-09-01", periods=len(closes))
    return pd.DataFrame({"close": closes}, index=idx)


def test_registered_in_registry():
    assert "price_move" in list_strategies()
    assert get_strategy("price_move") is PriceMove


def test_defaults():
    s = PriceMove()
    assert s.threshold == 100.0
    assert s.lookback == 5


def test_bullish_breakout_signal():
    s = PriceMove(threshold=50, lookback=3)
    closes = [1000.0] * 5 + [1060.0]  # +60 over 3 bars > 50
    signals = s.generate_signals(_candles(closes))
    assert signals.iloc[-1] == 1


def test_bearish_breakdown_signal():
    s = PriceMove(threshold=50, lookback=3)
    closes = [1000.0] * 5 + [940.0]  # -60 over 3 bars < -50
    signals = s.generate_signals(_candles(closes))
    assert signals.iloc[-1] == -1


def test_inside_threshold_is_neutral():
    s = PriceMove(threshold=100, lookback=5)
    closes = [24800.0] * 8 + [24850.0]  # +50 < 100
    signals = s.generate_signals(_candles(closes))
    assert signals.iloc[-1] == 0


def test_warmup_bars_are_neutral():
    s = PriceMove(threshold=10, lookback=5)
    signals = s.generate_signals(_candles([1000.0, 1500.0]))  # no lookback yet
    assert signals.tolist() == [0, 0]
