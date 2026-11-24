"""ImmediateShortStraddle — sell ATM straddle on the first bar after deploy.

Entry: unconditional (NEUTRAL view), fires on first bar after deploy.
Exit: SL / TP bracket (points or rupee), trailing stop, EOD squareoff.
Instrument type: options.

This is the "basic selling strategy" — no VIX gate, no DTE regime filter,
no delta adjustment. It sells premium and manages the bracket until expiry
or squareoff, whichever comes first.
"""

from __future__ import annotations

from __future__ import annotations

from backtest.strategy.intent import Direction, MarketView

from backtest.strategy.short_strategy_base import ShortStrategyBase


class ImmediateShortStraddle(ShortStrategyBase):
    """Sell the ATM call and ATM put on the nearest expiry at deploy."""

    name = "immediate_short_straddle"
    version = "2.0"
    author = "quant-options-engineer"
    instrument_type = "options"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]

    description = (
        "Immediate short straddle: sells the ATM call and ATM put on the "
        "nearest expiry, one lot each, on the first bar after deploy. "
        "Fee-aware bracket in points or rupee, with trailing stop, EOD "
        "squareoff, and IV entry filter."
    )
    description_sections = {
        "overview": (
            "Sells both ATM legs on the same nearest expiry. It is a volatility-selling "
            "structure with no directional view."
        ),
        "regime": (
            "Wants a range-bound session. Any move big enough to put one ATM leg deep in the "
            "money loses on that leg faster than the other leg and the net credit recover."
        ),
        "entry": (
            "Unconditional: the opening intent fires on the first bar after deploy with no "
            "signal, filter or confirmation. Optional IV filter can suppress entries in "
            "unfavourable IV regimes."
        ),
        "construction": (
            "Two legs, one lot each: sold ATM call and sold ATM put, same expiry. Strike comes "
            "from the live chain's nearest ATM, never from a hardcoded ladder."
        ),
        "exits_risk": (
            "Declared bracket: stop and target in points of premium or rupees "
            "('exit_unit' selects). Trailing stop tightens as premium decays. EOD squareoff at "
            "squareoff_minutes_before_close before market close. The true worst case is "
            "unlimited; the stop is the only limit. Carry is positional: the book holds "
            "overnight until its expiry unless squared off earlier."
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
        "carry_policy": "positional",
        "allowed_overrides": ["quantity", "exit"],
    }

    # --- generate_market_view: unconditional NEUTRAL entry -----------------

    def generate_market_view(self, candles) -> MarketView | None:
        """NEUTRAL view on every valid bar — enter immediately."""
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
