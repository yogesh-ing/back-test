"""Nifty F&O instant-buy strategy for paper trading validation.

Rules (aggressive options buying):
- Entry: BUY ATM call option immediately when strategy starts
- Exit: Take profit at +20% premium OR stop loss at -10% premium
- Time exit: Square off after 10 bars if neither hit
- Only works on NIFTY index options

Designed to test:
- Options chain fetching from mStock
- Option contract selection (ATM)
- Premium-based PnL calculation
- Options-specific charges (STT on options is different)
"""

from __future__ import annotations

import pandas as pd
from backtest.strategy.base import Strategy


class Nifty_InstantBuy(Strategy):
    """Instant ATM call buy on NIFTY options."""
    
    name = "nifty_instant_buy"
    timeframe = "1min"
    instrument_type = "option"  # Signals this trades options
    description = (
        "Buys ATM NIFTY call option immediately, exits at +20% TP / -10% SL "
        "or after 10 bars. Tests options chain fetching and premium PnL."
    )
    version = "1.0"
    author = "paper-trading-validation"
    
    params = {
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying Index",
            "tooltip": "Index to trade options on.",
        },
        "take_profit_pct": {
            "default": 20.0,
            "min": 5.0,
            "max": 50.0,
            "type": "float",
            "label": "Take Profit (%)",
            "tooltip": "Premium percentage for take profit.",
        },
        "stop_loss_pct": {
            "default": 10.0,
            "min": 5.0,
            "max": 30.0,
            "type": "float",
            "label": "Stop Loss (%)",
            "tooltip": "Premium percentage for stop loss.",
        },
        "max_hold_bars": {
            "default": 10,
            "min": 1,
            "max": 30,
            "type": "int",
            "label": "Max Hold Bars",
            "tooltip": "Force exit after N bars if TP/SL not hit.",
        },
    }
    
    def __init__(self, underlying="NIFTY", take_profit_pct=20.0, 
                 stop_loss_pct=10.0, max_hold_bars=10, **kwargs):
        super().__init__(**kwargs)
        self.underlying = str(underlying).upper()
        self.take_profit_pct = float(take_profit_pct) / 100.0
        self.stop_loss_pct = float(stop_loss_pct) / 100.0
        self.max_hold_bars = int(max_hold_bars)
        
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        """Always signal BUY on first bar, then HOLD."""
        signals = pd.Series(0, index=candles.index, dtype=int)
        
        if len(candles) > 0:
            # Signal on first available bar
            signals.iloc[0] = 1
        
        return signals
    
    def should_exit(self, position, current_price, candles=None) -> tuple[bool, str]:
        """Check options exit conditions based on premium % change."""
        if not position or position.quantity == 0:
            return False, ""
        
        entry_price = position.average_entry_price
        if entry_price <= 0:
            return False, ""
        
        pnl_pct = (current_price - entry_price) / entry_price
        
        # Take profit
        if pnl_pct >= self.take_profit_pct:
            return True, f"TP +{pnl_pct*100:.1f}%"
        
        # Stop loss  
        if pnl_pct <= -self.stop_loss_pct:
            return True, f"SL {pnl_pct*100:.1f}%"
        
        # Time-based exit
        if hasattr(position, '_entry_bar_idx') and candles is not None:
            current_idx = len(candles) - 1
            held_bars = current_idx - position._entry_bar_idx
            if held_bars >= self.max_hold_bars:
                return True, f"Time ({held_bars}b)"
        
        return False, ""
