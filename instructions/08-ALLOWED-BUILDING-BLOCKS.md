# Card 08 — Allowed building blocks (curb hallucinated APIs)

**Prerequisite:** Cards 00–04. **Purpose:** constrain what a strategy/module may
use so a lower-capability model doesn't invent nonexistent functions. If you need
something not listed here, prefer the closest listed primitive.

## Imports allowed
```python
from __future__ import annotations
import pandas as pd
import numpy as np
from backtest.strategy.base import Strategy      # in strategies/*.py
```
Standard library allowed where sensible: `dataclasses`, `pathlib`, `json`,
`argparse`, `importlib`, `pkgutil`, `io`, `os`, `socket`, `time`, `csv`.
Third-party allowed: `pandas`, `numpy`, `requests`, `dotenv` (python-dotenv),
`matplotlib`. **Do not** import any TA/indicator library (compute indicators with
pandas/numpy).

## pandas / numpy primitives you may use
- Series math: `+ - * /`, comparisons (`> < >= <= == !=`), `.abs()`, `.clip(a,b)`.
- Rolling / EWM: `s.rolling(n).mean()/.max()/.min()/.std()`,
  `s.ewm(alpha=1/n, min_periods=n, adjust=False).mean()`.
- Shifts / diffs: `s.shift(k)`, `s.diff()`, `s.pct_change()`, `s.cumprod()`,
  `s.cummax()`.
- Cleaning: `s.fillna(x)`, `s.reindex(index)`, `s.astype(int/float/bool)`,
  `.to_numpy()`.
- Construction: `pd.Series(data, index=candles.index)`,
  `pd.DataFrame({...})`, `pd.to_datetime`, `pd.date_range`, `pd.read_csv`.
- numpy: `np.sign`, `np.sqrt`, `np.zeros`, `np.isnan`, `np.allclose`.

## Strategy-authoring rules (Card 03 recap)
- Subclass `Strategy`; set a unique `name`; declare defaults in `params`.
- Override **one** of: `generate_signals(candles) -> Series` in {−1,0,1}, OR
  `entries(candles) -> Series[bool]` (+ optional `exits`).
- Read params via `self.p("key")`. Optionally declare `stop_loss`/`take_profit`
  as class floats.
- Use only the `candles` columns `open, high, low, close, volume`.
- Must be **pure** (Card 00 rule 7). Prefer `.shift(1)` when an entry compares to
  a rolling stat that includes the current bar.

## ⛔ Forbidden (in strategies and generally)
- Editing `engine/`, `data/`, `cli.py`, `runner.py`, `strategy/base.py`,
  `strategy/registry.py` to change results.
- Any order-placement / live-trading calls (the mStock client is read-only).
- Network access inside a strategy; reading files inside a strategy.
- Weakening a test, or changing metrics/engine to make numbers look better.
- Inventing pandas/TA functions not listed above.

## Minimal templates
Signal model:
```python
class MyStrat(Strategy):
    name = "my_strat"
    params = {"lookback": 20}
    def generate_signals(self, candles):
        ma = candles["close"].rolling(self.p("lookback")).mean()
        return (candles["close"] > ma).astype(int)
```
Entries/exits + risk:
```python
class MyBreakout(Strategy):
    name = "my_breakout"
    params = {"lookback": 20}
    stop_loss = 0.03
    take_profit = 0.06
    def entries(self, candles):
        return candles["close"] >= candles["close"].rolling(self.p("lookback")).max().shift(1)
    def exits(self, candles):
        return candles["close"] <= candles["close"].rolling(self.p("lookback")).min().shift(1)
```

**Verify:** `python -m backtest list` shows your strategy;
`python -m backtest run --strategy <name> --source synthetic` runs; `pytest -q`
stays green.
