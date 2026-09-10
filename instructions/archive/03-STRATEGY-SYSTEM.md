# Card 03 — Strategy plug-in system (`strategy/` + `strategies/`)

**Prerequisite:** Cards 00–02. **Builds:** the pluggable strategy contract and
four built-in strategies (auto-discovered — no central list to edit).

## `strategy/base.py`
- `class Strategy(ABC)` with class attrs `name=""`, `params={}`,
  `stop_loss=None`, `take_profit=None`.
- `__init_subclass__`: auto-register the subclass (via `registry.register`) when
  it sets a non-empty `name`.
- `__init__(**overrides)`: merge `params` with overrides; raise `ValueError` on
  unknown keys (list the known ones).
- `p(key)`: return a resolved param.
- Authoring surface — a subclass overrides **one** of:
  - `entries(candles) -> Series[bool]` (default raises `NotImplementedError`)
  - `exits(candles) -> Series[bool] | None` (default `None`)
  - `generate_signals(candles) -> Series` in {−1,0,1}. **Default:** if `entries`
    is overridden, build positions from entries/exits; else raise
    `NotImplementedError`.
- `_uses_entries_model()`: `type(self).entries is not Strategy.entries`.
- `_signals_from_entries_exits(candles)`: loop; hold long when an entry fires,
  exit to flat when an exit fires; return a {0,1} target Series on the index.

## `strategy/registry.py`
- `_REGISTRY: dict[str, type[Strategy]]`.
- `register(cls)`: require non-empty `name`; reject duplicates; store.
- `_discover()`: import every module under `backtest.strategies`
  (`pkgutil.iter_modules`) so subclasses self-register.
- `list_strategies()` (sorted copy) and `get_strategy(name)` (raise `KeyError`
  listing available names if unknown); both call `_discover()` first.

## `strategies/` — the four built-ins
- `buy_and_hold.py` — `BuyAndHold`, `name="buy_and_hold"`, params `{}`; signals
  all 1.
- `sma_crossover.py` — `SmaCrossover`, params `{"fast":20,"slow":50}`;
  `generate_signals`: `(SMA(fast) > SMA(slow))` as int.
- `rsi_reversion.py` — `RsiReversion`, params
  `{"period":14,"lower":30,"exit_level":55}`; Wilder RSI (EWM `alpha=1/period`);
  enter long when RSI<lower, hold until RSI>exit_level, else flat (stateful loop).
- `donchian_breakout.py` — `DonchianBreakout`, params `{"lookback":20}`,
  `stop_loss=0.05`, `take_profit=0.10`; `entries`:
  `close ≥ rolling(lookback).max().shift(1)`; `exits`:
  `close ≤ rolling(lookback).min().shift(1)`.

**Verify:**
```
$env:PYTHONPATH="src"; python -c "from backtest.strategy.registry import list_strategies as L; print(sorted(L()))"
# expect: ['buy_and_hold','donchian_breakout','rsi_reversion','sma_crossover']
```

