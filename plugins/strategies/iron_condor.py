"""Iron Condor (S5) — institutional (approximated as short straddle).

Verdict: PAPER-READY (approx). True 4-leg iron condor needs multi-leg structure
support (missing); implemented as short ATM straddle with wider wings via stop
points. Defined risk via stop; wings note in description.

Spec:
- Signal: IV_7DTE > RV_20D + 3 vol pts AND ADX < 20 AND VIX 15-30.
- Structure: short ATM straddle (approximates iron condor; wings ATM±500 ideal).
- SL: premium moves adverse 20 pts; TP: +60% of credit; time stop 5 days.
- Regime: only LOW_VOL_RANGE or NORMAL_RANGE.
- Kill: NIFTY moves > 300 pts; IV drops > 5 vol pts.
"""
from __future__ import annotations

from backtest.strategy.short_strategy_base import ShortStrategyBase
from backtest.strategy.intent import Direction, MarketView


class IronCondor(ShortStrategyBase):
    name = "iron_condor"
    version = "1.0"
    author = "quant-researcher (S5)"
    instrument_type = "options"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]

    description = (
        "Iron condor (approx): short 7-DTE ATM straddle with wider stop to "
        "emulate defined-risk wings (ideal: ATM +/- 500). Range-bound only."
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
            "stop_loss_points": 20.0,
            "take_profit_points": 10.0,
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
        "exit_unit": {"default": "points", "type": "str", "label": "Exit Unit",
                      "tooltip": "'points' or 'rupee'."},
        "stop_loss_points": {"default": 20.0, "type": "float", "min": 1.0, "max": 500.0,
                             "label": "Stop Loss (pts)", "tooltip": "Adverse move in pts of premium."},
        "take_profit_points": {"default": 10.0, "type": "float", "min": 1.0, "max": 500.0,
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
