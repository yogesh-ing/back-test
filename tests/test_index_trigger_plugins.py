"""Unit tests for index-triggered option strategy plugins."""

from __future__ import annotations

import pandas as pd

from backtest.strategy.intent import Direction
from plugins.strategies.nifty_trigger_long_ce import NiftyTriggerLongCE
from plugins.strategies.nifty_trigger_long_pe import NiftyTriggerLongPE


def _candles(close: float) -> pd.DataFrame:
    return pd.DataFrame({"close": [close]}, index=pd.date_range("2026-01-01", periods=1, freq="D"))


def test_ce_bullish_when_above_trigger():
    view = NiftyTriggerLongCE(trigger_level=25000).generate_market_view(_candles(25100.0))
    assert view is not None
    assert view.direction is Direction.BULLISH
    assert view.underlying == "NIFTY"
    assert view.spot_price == 25100.0


def test_ce_silent_when_below_trigger():
    view = NiftyTriggerLongCE(trigger_level=25000).generate_market_view(_candles(24900.0))
    assert view is None


def test_ce_exact_trigger_fires():
    view = NiftyTriggerLongCE(trigger_level=25000).generate_market_view(_candles(25000.0))
    assert view is not None
    assert view.direction is Direction.BULLISH


def test_pe_bearish_when_at_or_below_trigger():
    view = NiftyTriggerLongPE(trigger_level=25000).generate_market_view(_candles(25000.0))
    assert view is not None
    assert view.direction is Direction.BEARISH


def test_pe_silent_when_above_trigger():
    view = NiftyTriggerLongPE(trigger_level=25000).generate_market_view(_candles(25100.0))
    assert view is None


def test_plugins_have_execution_spec():
    ce = NiftyTriggerLongCE()
    pe = NiftyTriggerLongPE()
    assert ce.execution_spec() is not None
    assert pe.execution_spec() is not None
    assert ce.side == "CE"
    assert pe.side == "PE"
