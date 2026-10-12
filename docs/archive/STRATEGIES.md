# Strategies

## How Strategies Work

A strategy is a class that looks at OHLCV candles and decides when to buy (+1), sell (-1), or hold (0).

```python
class Strategy(ABC):
    name: str           # Unique ID (e.g. "sma_crossover")
    description: str    # Human-readable description
    params: dict        # Parameter schema for UI forms
    
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        # Input: DataFrame with [open, high, low, close, volume]
        # Output: Series of +1, -1, or 0 (same DatetimeIndex)
```

## Signal Values

| Value | Meaning | What Engine Does |
|-------|---------|-----------------|
| `+1` | Long (buy) | Enters long position |
| `-1` | Short (sell) | Enters short position |
| `0` | Flat (hold) | Closes any open position |

## Built-In Strategies

### 1. Buy & Hold (`buy_and_hold.py`)
**Logic:** Buy on the first bar, hold forever. Baseline benchmark.

```python
params = {}  # No parameters
signals = pd.Series(1, index=candles.index)  # Always long
```

### 2. SMA Crossover (`sma_crossover.py`)
**Logic:** Buy when fast SMA crosses above slow SMA.

```python
params = {
    "fast": {"default": 20, "min": 2, "max": 100, "type": "int"},
    "slow": {"default": 50, "min": 5, "max": 250, "type": "int"},
}
# signals = (fast_sma > slow_sma).astype(int)
```

### 3. RSI Reversion (`rsi_reversion.py`)
**Logic:** Buy when RSI is oversold, sell when it recovers.

```python
params = {
    "period": {"default": 14, "min": 2, "max": 50, "type": "int"},
    "lower": {"default": 30, "min": 1, "max": 49, "type": "int"},
    "exit_level": {"default": 55, "min": 50, "max": 90, "type": "int"},
}
# Buy when RSI < lower, sell when RSI > exit_level
```

### 4. Donchian Breakout (`donchian_breakout.py`)
**Logic:** Buy on new highs, sell on new lows. Has built-in risk management.

```python
params = {
    "lookback": {"default": 20, "min": 2, "max": 100, "type": "int"},
}
stop_loss = 0.05    # 5% stop loss
take_profit = 0.10  # 10% take profit
```

## Parameter Schema

Two formats are supported:

### Flat (Legacy)
```python
params = {"period": 14, "lower": 30}
```

### Schema (PRD — enables dynamic UI forms)
```python
params = {
    "period": {
        "default": 14,
        "min": 2,
        "max": 50,
        "type": "int",      # int, float, bool, str
        "label": "RSI Period",
        "tooltip": "Lookback window for RSI calculation",
    }
}
```

The UI auto-generates form fields from the schema.

## Strategy Registry (`strategy/registry.py`)

Strategies are auto-discovered by scanning `backtest/strategies/`:

```python
from backtest.strategy.registry import list_strategies, get_strategy

# List all available strategies
names = list_strategies()  # ["buy_and_hold", "donchian_breakout", "rsi_reversion", "sma_crossover"]

# Get a strategy class
cls = get_strategy("sma_crossover")
strategy = cls(fast=20, slow=50)  # instantiate with params
signals = strategy.generate_signals(candles)  # get signals
```

### Adding a New Strategy

1. Create `src/backtest/strategies/my_strategy.py`
2. Inherit from `Strategy`
3. Set `name`, `description`, `params`
4. Implement `generate_signals()`
5. It's auto-discovered — no registration needed

Example:
```python
import pandas as pd
from backtest.strategy.base import Strategy

class MyStrategy(Strategy):
    name = "my_strategy"
    description = "My custom strategy"
    version = "1.0"
    author = "Me"
    params = {
        "period": {"default": 20, "min": 2, "max": 100, "type": "int"},
    }
    
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        sma = candles["close"].rolling(self.period).mean()
        return (candles["close"] > sma).astype(int)
```

## Risk Management (Built Into Engine)

Strategies can declare `stop_loss` and `take_profit` as class attributes:

```python
class MyStrategy(Strategy):
    stop_loss = 0.05    # Exit if loss exceeds 5%
    take_profit = 0.10  # Exit if profit exceeds 10%
```

The engine's `_run_with_risk()` method handles the actual exit logic using intrabar high/low prices.

## Invariants (Safety Rules)

1. **No lookahead:** Signals are lagged by 1 period (today's signal executes tomorrow)
2. **Commission + slippage:** Applied on every position change
3. **Position sizing:** 100% of capital per trade (no partial positions)
4. **Vectorized execution:** Engine runs on full candle series, not bar-by-bar (fast)
