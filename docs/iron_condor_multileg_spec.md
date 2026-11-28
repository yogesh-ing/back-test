# Iron Condor (S5) — Multi-Leg Spec for QUANT-ARCH

## Goal
Support 4-leg defined-risk structures (iron condor) with parameterized wing width,
via a new multi-leg strategy base class and bridge execution.

## New base class: `MultiLegOptionStrategy` (src/backtest/strategy/multi_leg_base.py)
- Subclass of Strategy (not DirectionalOptionStrategy).
- Declares `opening_structure` as a list of legs:
  ```
  opening_structure = [
      {"side": "sell", "type": "call", "strike": "atm", "expiry_policy": "nearest"},
      {"side": "sell", "type": "put",  "strike": "atm", "expiry_policy": "nearest"},
      {"side": "buy",  "type": "call", "strike": "atm+wing", "expiry_policy": "nearest"},
      {"side": "buy",  "type": "put",  "strike": "atm-wing", "expiry_policy": "nearest"},
  ]
  ```
- `wing_width_points` param (default 500 for NIFTY) drives strike offsets.
- `quantity` param (1-3 lots).
- Emits NEUTRAL view (no directional signal); entry on regime gate.

## Execution block
```
execution = {
  "emits": [],
  "structures": {},
  "legs": [...],  # as above, resolved at spawn
  "exit": {
    "stop_loss_points": <param>,
    "take_profit_pct": 0.60,
    "signal_flip": False,
    "min_days_to_expiry": 2,
  },
  "squareoff_minutes_before": 30,
  "quantity": 1,
  "carry_policy": "positional",
  "allowed_overrides": ["quantity", "exit", "wing_width_points"],
}
```

## Regime gate (strategy-level, before emitting view)
- IV_7DTE > RV_20D + 3 vol pts
- ADX < 20 (range-bound)
- VIX between 15 and 30
- If any fails → return None (no entry).

## Kill conditions
- NIFTY moves > 300 pts from ATM at entry.
- IV drops > 5 vol pts (credit already captured).
- 4 consecutive losses.

## Params (all parameterized, no hardcoding)
- underlying (NIFTY/BANKNIFTY)
- wing_width_points (250/500/750)
- iv_rv_threshold (default 3.0)
- vix_min (15), vix_max (30)
- adx_max (20)
- stop_loss_points, take_profit_points
- trailing_stop_points, trailing_activation_points
- squareoff_minutes_before_close
- min_iv_percentile, max_iv_percentile

## Engine/bridge changes required
1. `forward/options_bridge.py`: support `legs` array in expression; resolve strikes per leg using wing_width_points offset and DeltaSelector/ATM selector.
2. Margin/SPAN: compute combined margin for 4-leg structure.
3. Exit engine: apply stop/target to net credit of the whole structure.
4. Conformance test: add multi-leg strategy to test_strategy_conformance.

## Plugin (after base exists)
- `plugins/strategies/iron_condor.py` rewritten to subclass MultiLegOptionStrategy, removing the short_straddle approximation.