"""Acceptance tests for strategy entries/exits and risk management (Card 00 invariant 5)."""

import pandas as pd

from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.strategy.base import Strategy
from backtest.strategy.registry import get_strategy


def build_ohlcv_frame(closes, highs=None, lows=None):
    """Helper: build OHLCV frame from close list with optional high/low."""
    if highs is None:
        highs = closes
    if lows is None:
        lows = closes
    
    index = pd.bdate_range(start="2021-01-01", periods=len(closes))
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000] * len(closes),
    }, index=index)


def test_entries_exits_generate_positions():
    """Test 8: Entries close>100/exits close<90 over [95,101,102,88,89,105] ⇒ positions [0,1,1,0,0,1]."""
    
    class TestStrategy(Strategy):
        name = "test_entries_exits"
        
        def entries(self, candles):
            return candles["close"] > 100
        
        def exits(self, candles):
            return candles["close"] < 90
    
    closes = [95.0, 101.0, 102.0, 88.0, 89.0, 105.0]
    candles = build_ohlcv_frame(closes)
    strategy = TestStrategy()
    signals = strategy.generate_signals(candles)
    
    expected_positions = [0, 1, 1, 0, 0, 1]
    assert list(signals) == expected_positions


def test_donchian_has_stops():
    """Test 9: donchian_breakout registered with non-None stop_loss & take_profit."""
    strategy_cls = get_strategy("donchian_breakout")
    
    assert strategy_cls.stop_loss is not None
    assert strategy_cls.take_profit is not None
    assert strategy_cls.stop_loss > 0
    assert strategy_cls.take_profit > 0


def test_stop_loss_caps_loss():
    """Test 10: Long entered at 100, later bar low pierces 5% stop ⇒ total return == −0.05 (zero costs)."""
    closes = [100.0, 100.0, 100.0, 95.0, 95.0]
    lows = [100.0, 100.0, 100.0, 94.0, 95.0]  # bar 3: low=94 < 95 stop
    candles = build_ohlcv_frame(closes, closes, lows)
    
    # Manual signals: hold from bar 1 to 3, then exit
    signals = pd.Series([0, 1, 1, 1, 1], index=candles.index)
    
    result = Backtester(
        BacktestConfig(
            initial_capital=100000.0,
            commission_pct=0.0,
            slippage_pct=0.0,
            stop_loss=0.05,
            take_profit=None,
        )
    ).run(candles, signals)
    
    # Stop at 95.0 (100 * 0.95), return = -0.05
    assert abs(result.metrics["total_return"] - (-0.05)) < 1e-4


def test_take_profit_caps_win():
    """Test 11: Long entered at 100, bar high >= 110 (10% target) ⇒ total return == +0.10."""
    closes = [100.0, 100.0, 100.0, 110.0, 110.0]
    highs = [100.0, 100.0, 100.0, 110.0, 110.0]
    candles = build_ohlcv_frame(closes, highs)
    
    signals = pd.Series([0, 1, 1, 1, 1], index=candles.index)
    
    result = Backtester(
        BacktestConfig(
            initial_capital=100000.0,
            commission_pct=0.0,
            slippage_pct=0.0,
            stop_loss=None,
            take_profit=0.10,
        )
    ).run(candles, signals)
    
    assert abs(result.metrics["total_return"] - 0.10) < 1e-4


def test_no_risk_path_matches_vectorized():
    """Test 12: No stop/target ⇒ total return == close[-1]/close[0]−1."""
    closes = [100.0, 102.0, 101.0, 103.0, 105.0]
    candles = build_ohlcv_frame(closes)
    
    # Signal at bar 0 to enter at bar 1 (accounting for lag)
    signals = pd.Series([1, 1, 1, 1, 1], index=candles.index)
    
    result = Backtester(
        BacktestConfig(
            initial_capital=100000.0,
            commission_pct=0.0,
            slippage_pct=0.0,
            stop_loss=None,
            take_profit=None,
        )
    ).run(candles, signals)
    
    # With lag, position enters at bar 1 at 102 (prev_close from bar 0)
    # Return = close[-1] / close[0] - 1 = 105 / 100 - 1 = 0.05
    expected = closes[-1] / closes[0] - 1
    assert abs(result.metrics["total_return"] - expected) < 1e-5
