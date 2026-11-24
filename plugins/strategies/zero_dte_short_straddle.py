"""ZeroDTEShortStraddle — sell ATM straddle on expiry day, close at 15:00 IST.

Entry: unconditional (NEUTRAL view), but only on expiry day (0 DTE contracts).
Exit: SL / TP bracket, trailing stop, AND forced squareoff at 15:00 IST
      (squareoff_minutes_before_close=30) even if SL/TP not hit.
Instrument type: options.

This is the 0 DTE variant of ImmediateShortStraddle. The key difference:
min_days_to_expiry = 0 in the direction rule (only enter expiry-day contracts),
and squareoff at 15:00 IST beats the broker RMS auto-squareoff (~15:15–15:20).
"""

from __future__ import annotations

from backtest.strategy.intent import Direction, MarketView

from backtest.strategy.short_strategy_base import ShortStrategyBase


class ZeroDTEShortStraddle(ShortStrategyBase):
    """Sell ATM straddle on expiry day only; force close at 15:00 IST."""

    name = "zero_dte_short_straddle"
    version = "2.0"
    author = "quant-options-engineer"
    instrument_type = "options"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]

    description = (
        "Zero-DTE short straddle: sells the ATM call and ATM put on today's "
        "expiry, one lot each, on the first bar after deploy. Forces squareoff "
        "at 15:00 IST regardless of SL/TP to avoid broker RMS auto-squareoff."
    )
    description_sections = {
        "overview": (
            "Sells both ATM legs on today's expiry only. Volatility-selling "
            "structure with no directional view. Held intraday only."
        ),
        "regime": (
            "Wants a range-bound expiry session. Gamma crush accelerates into "
            "the close; the 15:00 IST squareoff avoids last-hour pin risk."
        ),
        "entry": (
            "Unconditional on expiry day: the opening intent fires on the first "
            "bar after deploy only when 0 DTE contracts exist. Optional IV filter "
            "can suppress entries in unfavourable IV regimes."
        ),
        "construction": (
            "Two legs, one lot each: sold ATM call and sold ATM put, today's expiry. "
            "Strike comes from the live chain's nearest ATM, never hardcoded."
        ),
        "exits_risk": (
            "Declared bracket: stop and target in points or rupee. Trailing stop "
            "tightens as premium decays. FORCED squareoff at "
            "squareoff_minutes_before_close (default 30) before market close — "
            "on expiry day this means 15:00 IST, beating broker RMS (~15:15–15:20)."
        ),
        "parameters": (
            "underlying=NIFTY; exit_unit='points'; stop_loss_points=13; take_profit_points=7; "
            "trailing_stop_points=5; trailing_activation_points=3; "
            "squareoff_minutes_before_close=30; min_iv_percentile=0; max_iv_percentile=100."
        ),
    }

    opening_structure = {
        "structure": "short_straddle",
        "strike_selection": "atm",
        "expiry_policy": "nearest",
        "expiry_min_days": 0,
    }

    execution = {
        "emits": [],
        "structures": {},
        "exit": {
            "stop_loss_points": 13.0,
            "take_profit_points": 7.0,
            "signal_flip": False,
            "min_days_to_expiry": None,
        },
        "squareoff_minutes_before": 30,
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 3,
        "carry_policy": "intraday",
        "allowed_overrides": ["quantity", "exit"],
    }

    # --- generate_market_view: NEUTRAL entry only on expiry day -----------

    def generate_market_view(self, candles) -> MarketView | None:
        """NEUTRAL view on every valid bar — enter only on expiry day.

        The bridge's expiry_min_days=0 gate ensures only 0 DTE contracts
        are selected. If today is not expiry day, no contract matches and
        no entry occurs.
        """
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
