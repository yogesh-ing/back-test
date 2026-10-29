"""High-frequency equity scalping strategy for paper trading validation.

Rules (aggressive, generates many trades):
- Entry: If current close > previous close (momentum), BUY at next open
- Exit: Take profit at +3 points OR stop loss at -2 points
- Time exit: Close all positions after 5 bars if neither TP nor SL hit
- Works on any equity symbol with sufficient liquidity

Designed to stress-test:
- Fill quantity tracking (Bug #1)
- Charge calculation per trade (Bug #2)  
- Trade persistence to DB (Bug #3)
- PnL reconciliation accuracy (Bug #4)
"""

from __future__ import annotations

import pandas as pd
from backtest.strategy.base import Strategy


class HF_EquityScalp(Strategy):
    """High-frequency momentum scalp on equities."""
    
    name = "hf_equity_scalp"
    timeframe = "1min"
    description = (
        "Aggressive equity scalper: buys on positive momentum, exits at "
        "+3pt TP / -2pt SL or after 5 bars. Generates frequent trades for "
        "paper trading validation."
    )
    version = "1.0"
    author = "paper-trading-validation"
    
    params = {
        "take_profit": {
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "type": "float",
            "label": "Take Profit (pts)",
            "tooltip": "Points above entry to take profit.",
        },
        "stop_loss": {
            "default": 2.0,
            "min": 1.0,
            "max": 10.0,
            "type": "float",
            "label": "Stop Loss (pts)",
            "tooltip": "Points below entry to stop loss.",
        },
        "max_hold_bars": {
            "default": 5,
            "min": 1,
            "max": 20,
            "type": "int",
            "label": "Max Hold Bars",
            "tooltip": "Force exit after N bars if TP/SL not hit.",
        },
    }
    
    def __init__(self, take_profit=3.0, stop_loss=2.0, max_hold_bars=5, **kwargs):
        super().__init__(**kwargs)
        self.take_profit = float(take_profit)
        self.stop_loss = float(stop_loss)
        self.max_hold_bars = int(max_hold_bars)
        
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        """Generate BUY signals on positive momentum."""
        if len(candles) < 2:
            return pd.Series(0, index=candles.index, dtype=int)
        
        # Momentum: close[t] > close[t-1]
        momentum_up = candles['close'].iloc[1:] > candles['close'].iloc[:-1].values
        
        signals = pd.Series(0, index=candles.index, dtype=int)
        signals.iloc[1:] = momentum_up.astype(int).values
        
        return signals
    
    def should_exit(self, position, current_price, candles=None) -> tuple[bool, str]:
        """Check exit conditions: TP, SL, or time-based."""
        if not position or position.quantity == 0:
            return False, ""
        
        entry_price = position.average_entry_price
        pnl_per_share = current_price - entry_price
        
        # Take profit
        if pnl_per_share >= self.take_profit:
            return True, f"TP +{pnl_per_share:.2f}"
        
        # Stop loss
        if pnl_per_share <= -self.stop_loss:
            return True, f"SL {pnl_per_share:.2f}"
        
        # Time-based exit
        if hasattr(position, '_entry_bar_idx') and candles is not None:
            current_idx = len(candles) - 1
            held_bars = current_idx - position._entry_bar_idx
            if held_bars >= self.max_hold_bars:
                return True, f"Time exit ({held_bars} bars)"
        
        return False, ""
