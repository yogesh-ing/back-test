# Strategy Implementation Plan — Master Document

## Implemented (paper-ready plugins)
1. opening_range_breakout (B7) — plugins/strategies/opening_range_breakout.py
2. mean_reversion_extreme (B8) — plugins/strategies/mean_reversion_extreme.py
3. pullback_in_uptrend (S6) — plugins/strategies/pullback_in_uptrend.py
4. pre_expiry_gamma (S8) — plugins/strategies/pre_expiry_gamma.py
5. theta_harvest (S1+S2 merged) — plugins/strategies/theta_harvest.py
6. iron_condor (S5 approx) — plugins/strategies/iron_condor.py

## Renamed (old_ stripped)
- vwap_ema_rsi (was old_vwap_ema_rsi)
- ema_reversion_pob (was old_ema_reversion_pob)

## Deleted
12 redundant/old strategies (old_immediate_strangle, old_pe/ce_instant_buy, old_one_min_scalp, old_hf_equity_scalp, old_momentum_burst, old_time_alternator, old_straddle_short_atm, old_strangle_wing_points, old_volume_oi_surge, zero_dte_short_straddle, option_directional).

## Pending implementation (blocked)
B1 (IV/RV feed + long_straddle base), B2/B9 (portfolio engine), B3/S7 (earnings feed), B4 (FII feed), B5 (NSE rebalance feed), B6 (multi-DTE IV), B10 (tick execution), S3 (shorting), S4 (short interest), S9 (sector data).

## Next engine work
Multi-leg base class + bridge 4-leg support per docs/iron_condor_multileg_spec.md (reviewed; needs data-provider contract + strike rounding).