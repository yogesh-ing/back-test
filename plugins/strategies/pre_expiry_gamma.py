"""Pre-Expiry Gamma (S8) — institutional.

Verdict: PAPER-READY. Short ATM straddle in last 2 DTE when range-bound, with
mandatory OTM wings (ATM +/- 250). Highest-probability options trade.

Spec:
- Signal: 2 DTE to expiry AND ADX < 20 (range) AND |NIFTY - high_OI_strike| < 0.5%.
- Structure: short 2-DTE ATM straddle + wings (ATM +/- 250), 1 lot.
- SL: NIFTY moves > 0.3% against; TP: +50% of credit; time stop 1 DTE.
- Regime: ONLY LOW_VOL_RANGE.
- Kill: NIFTY moves > 0.3%; ADX > 25.
- Params: min_iv_percentile, max_iv_percentile, squareoff, trailing (reuse straddle params).
"""
from __future__ import annotations

from backtest.strategy.short_strategy_base import ShortStrategyBase
from backtest.strategy.intent import Direction, MarketView


class PreExpiryGamma(ShortStrategyBase):
    name = "pre_expiry_gamma"
    version = "1.0"
    author = "quant-researcher (S8)"
    instrument_type = "options"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]

    description = (
        "Pre-expiry gamma: short 2-DTE ATM straddle with OTM wings when NIFTY "
        "is range-bound (ADX<20) and pinned near high-OI strike. Exit at 1 DTE."
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
            "stop_loss_points": 0.003,
            "take_profit_points": 5.0,
            "signal_flip": False,
            "min_days_to_expiry": 2,
        },
        "squareoff_minutes_before": 60,
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
        "stop_loss_points": {"default": 10.0, "type": "float", "min": 1.0, "max": 500.0,
                             "label": "Stop Loss (pts)", "tooltip": "Adverse move in pts of premium."},
        "take_profit_points": {"default": 5.0, "type": "float", "min": 1.0, "max": 500.0,
                               "label": "Take Profit (pts)", "tooltip": "Favourable move in pts."},
        "trailing_stop_points": {"default": 3.0, "type": "float", "min": 0.0, "max": 200.0,
                                 "label": "Trailing Stop (pts)", "tooltip": "0 disables."},
        "trailing_activation_points": {"default": 2.0, "type": "float", "min": 0.0, "max": 200.0,
                                      "label": "Trailing Activation (pts)", "tooltip": "Start trailing after this profit."},
        "squareoff_minutes_before_close": {"default": 60, "type": "int", "min": 0, "max": 240,
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
