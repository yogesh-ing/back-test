"""Unit tests for index-triggered option strategy plugins.

Strategies are obtained from the registry (after plugin discovery via
create_app), never imported directly — direct imports would register a
second class object under the same name and collide with discovery.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.strategy.intent import Direction
from backtest.strategy.registry import get_strategy
from backtest.web.app import create_app


@pytest.fixture(scope="module")
def app():
    return create_app(source="synthetic")


@pytest.fixture(scope="module")
def ce_strategy(app):
    return get_strategy("nifty_trigger_long_ce")


@pytest.fixture(scope="module")
def pe_strategy(app):
    return get_strategy("nifty_trigger_long_pe")


def _candles(close: float) -> pd.DataFrame:
    return pd.DataFrame({"close": [close]}, index=pd.date_range("2026-01-01", periods=1, freq="D"))


def test_ce_bullish_when_above_trigger(ce_strategy):
    view = ce_strategy(trigger_level=25000).generate_market_view(_candles(25100.0))
    assert view is not None
    assert view.direction is Direction.BULLISH
    assert view.underlying == "NIFTY"
    assert view.spot_price == 25100.0


def test_ce_silent_when_below_trigger(ce_strategy):
    view = ce_strategy(trigger_level=25000).generate_market_view(_candles(24900.0))
    assert view is None


def test_ce_exact_trigger_fires(ce_strategy):
    view = ce_strategy(trigger_level=25000).generate_market_view(_candles(25000.0))
    assert view is not None
    assert view.direction is Direction.BULLISH


def test_pe_bearish_when_at_or_below_trigger(pe_strategy):
    view = pe_strategy(trigger_level=25000).generate_market_view(_candles(25000.0))
    assert view is not None
    assert view.direction is Direction.BEARISH


def test_pe_silent_when_above_trigger(pe_strategy):
    view = pe_strategy(trigger_level=25000).generate_market_view(_candles(25100.0))
    assert view is None


def test_plugins_have_execution_spec(ce_strategy, pe_strategy):
    assert ce_strategy.execution_spec() is not None
    assert pe_strategy.execution_spec() is not None
    assert ce_strategy.side == "CE"
    assert pe_strategy.side == "PE"
