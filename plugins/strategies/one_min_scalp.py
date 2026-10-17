"""1-minute momentum scalp strategy for paper trading validation.

Rules:
- If 1-min candle closes positive (close > open), BUY next bar at open
- Take profit: +5 points from entry
- Stop loss: -3 points from entry
- Exit all positions at end of day

Designed to generate frequent trades for validating:
- Fill quantities (Bug #1)
- Charges calculation (Bug #2)
- Trade persistence (Bug #3)
- PnL reconciliation (Bug #4)
"""

from __future__ import annotations

import pandas as pd
from backtest.strategy.templates import EquityStrategy


class OneMinScalp(EquityStrategy):
    """Simple 1-min momentum scalp: buy green candles, TP=5pts, SL=3pts."""
    
    name = "one_min_scalp"
    timeframe = "1min"
    description = (
        "Buys on next bar after a green candle (close > open), exits at "
        "+5pt TP / -3pt SL. Generates frequent trades for paper trading tests."
    )
    description_sections = {
        'overview': (
            'One-minute scalper for paper-trading tests: buys the bar after a green candle and'
            'takes a quick fixed target.'
        ),
        'regime': (
            'Works when green candles reliably see next-bar continuation at 1-minute scale'
            '(strong intraday momentum). Struggles in choppy minute bars — green/red alternation'
            'triggers entries right into reversals, and costs dominate the 5-point target.'
        ),
        'entry': (
            'A green candle (close above open) arms the entry; the buy fires on the next bar.'
        ),
        'construction': (
            'Single-instrument equity position (default RELIANCE), long only, 1-minute timeframe,'
            'one trade at a time.'
        ),
        'exits_risk': (
            'Fixed bracket on every trade: +5 points take-profit, -3 points stop-loss. Worst case'
            'per trade is the 3-point stop plus slippage — the entire edge rests on hitting +5'
            'first more often than chance.'
        ),
        'parameters': (
            'take_profit=5.0, stop_loss=3.0, symbol=RELIANCE. Points are absolute — retune for'
            'other instruments.'
        ),
    }
    version = "1.0"
    author = "paper-trading-validation"
    
    params = {
        "take_profit": {
            "default": 5.0,
            "min": 1.0,
            "max": 20.0,
            "type": "float",
            "label": "Take Profit (pts)",
            "tooltip": "Points above entry to take profit.",
        },
        "stop_loss": {
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "type": "float",
            "label": "Stop Loss (pts)",
            "tooltip": "Points below entry to stop loss.",
        },
        "symbol": {
            "default": "RELIANCE",
            "type": "str",
            "label": "Symbol",
            "tooltip": "Equity symbol to trade.",
        },
    }
    
    def __init__(self, take_profit=5.0, stop_loss=3.0, symbol="RELIANCE", **kwargs):
        super().__init__(**kwargs)
        self.take_profit = float(take_profit)
        self.stop_loss = float(stop_loss)
        self.symbol = str(symbol).upper()
        
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        """Generate signals based on candle direction.
        
        Returns:
            Series with values:
            - 1: BUY (previous candle was green)
            - 0: HOLD/EXIT (no signal)
        """
        if len(candles) < 2:
            return pd.Series(0, index=candles.index, dtype=int)
        
        # Green candle = close > open
        is_green = candles['close'] > candles['open']
        
        # Signal on NEXT bar after green candle
        signals = pd.Series(0, index=candles.index, dtype=int)
        signals.iloc[1:] = is_green.iloc[:-1].astype(int).values
        
        return signals
    
    def should_exit(self, position, current_price, candles=None) -> tuple[bool, str]:
        """Check if position should exit based on TP/SL.
        
        Returns:
            Tuple of (should_exit: bool, reason: str)
        """
        if not position or position.quantity == 0:
            return False, ""
        
        entry_price = position.average_entry_price
        pnl_per_share = current_price - entry_price
        
        # Take profit check
        if pnl_per_share >= self.take_profit:
            return True, f"TP hit (+{pnl_per_share:.2f})"
        
        # Stop loss check
        if pnl_per_share <= -self.stop_loss:
            return True, f"SL hit ({pnl_per_share:.2f})"
        
        return False, ""
