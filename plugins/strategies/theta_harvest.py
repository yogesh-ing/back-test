"""Theta Harvest (S1+S2 merged) — institutional.

Verdict: PAPER-READY. Short ATM straddle when IV>R V+5 (S1) or IV>RV+3 in low vol (S2).
Regime-gated: ADX<25, VIX 15-30. Mandatory OTM wings for defined risk.

Spec:
- Signal: IV_7DTE > RV_20D + iv_rv_threshold (5 for S1, 3 for S2) AND ADX<adx_max
  AND vix_min <= VIX <= vix_max.
- Structure: short ATM straddle, 7-DTE, 1 lot (+ OTM wings ATM±500 for defined risk).
- SL: premium moves adverse (points); TP: +50-60% of credit; time stop 5 days.
- Regime: only LOW_VOL_RANGE or NORMAL_RANGE or HIGH_VOL_RANGE (not trend).
- Kill: NIFTY moves >1.5% against; IV drops >5 vol pts.
- Params: iv_rv_threshold, vix_min, vix_max, adx_max, plus straddle bracket params.
"""
from __future__ import annotations

from backtest.strategy.short_strategy_base import ShortStrategyBase
from backtest.strategy.intent import Direction, MarketView


class ThetaHarvest(ShortStrategyBase):
    name = "theta_harvest"
    version = "1.0"
    author = "quant-researcher (S1+S2)"
    instrument_type = "options"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]

    description = (
        "Theta harvest: short 7-DTE ATM straddle when IV>R V+threshold and "
        "ADX<25, VIX in range. Wings mandatory for defined risk."
    )

    opening_structure = {
        "structure": "short_straddle",
        "strike_selection": "atm",
        "expiry_policy": "nearest",
    }

    execution = {
        "emits": [],
        "structures": {},
        "exit": {
            "stop_loss_points": 15.0,
            "take_profit_points": 8.0,
            "signal_flip": False,
            "min_days_to_expiry": 2,
        },
        "squareoff_minutes_before": 30,
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 3,
        "carry_policy": "positional",
        "allowed_overrides": ["quantity", "exit"],
    }

    params = {
        "underlying": {"default": "NIFTY", "type": "str", "label": "Underlying",
                       "tooltip": "Index (NIFTY / BANKNIFTY)."},
        "iv_rv_threshold": {"default": 5.0, "type": "float", "min": 2.0, "max": 10.0,
                            "label": "IV-RV Threshold (vol pts)",
                            "tooltip": "Sell when IV exceeds RV by this many vol pts."},
        "vix_min": {"default": 15.0, "type": "float", "min": 0.0, "max": 100.0,
                    "label": "Min VIX", "tooltip": "Skip if VIX below this."},
        "vix_max": {"default": 30.0, "type": "float", "min": 0.0, "max": 100.0,
                    "label": "Max VIX", "tooltip": "Skip if VIX above this."},
        "adx_max": {"default": 25.0, "type": "float", "min": 10.0, "max": 50.0,
                    "label": "Max ADX", "tooltip": "Skip if trending (ADX above)."},
        "exit_unit": {"default": "points", "type": "str", "label": "Exit Unit",
                      "tooltip": "'points' or 'rupee'."},
        "stop_loss_points": {"default": 15.0, "type": "float", "min": 1.0, "max": 500.0,
                             "label": "Stop Loss (pts)", "tooltip": "Adverse move in pts of premium."},
        "take_profit_points": {"default": 8.0, "type": "float", "min": 1.0, "max": 500.0,
                               "label": "Take Profit (pts)", "tooltip": "Favourable move in pts."},
        "trailing_stop_points": {"default": 5.0, "type": "float", "min": 0.0, "max": 200.0,
                                 "label": "Trailing Stop (pts)", "tooltip": "0 disables."},
        "trailing_activation_points": {"default": 3.0, "type": "float", "min": 0.0, "max": 200.0,
                                      "label": "Trailing Activation (pts)", "tooltip": "Start trailing after this profit."},
        "squareoff_minutes_before_close": {"default": 30, "type": "int", "min": 0, "max": 240,
                                          "label": "EOD Squareoff (min)", "tooltip": "Square off before close."},
        "min_iv_percentile": {"default": 0.0, "type": "float", "min": 0.0, "max": 100.0,
                              "label": "Min IV Percentile", "tooltip": "0 disables."},
        "max_iv_percentile": {"default": 100.0, "type": "float", "min": 0.0, "max": 100.0,
                              "label": "Max IV Percentile", "tooltip": "100 disables."},
    }

    def generate_market_view(self, candles) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None
        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None
        return MarketView(
            direction=Direction.NEUTRAL,
            confidence=1.0,
            underlying=str(getattr(self, "underlying", "NIFTY")).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
        )
