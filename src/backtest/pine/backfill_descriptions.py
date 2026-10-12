"""Backfill strategy descriptions for older strategies.

This script adds default descriptions to strategies that are missing them,
so they pass the new validation requirement (2026-09-29).

Usage:
    cd src && python -m backtest.pine.backfill_descriptions
"""

from __future__ import annotations

from backtest.plugins import discover_plugins
from backtest.strategy.registry import get_all

# Default descriptions for known strategies without descriptions
DEFAULT_DESCRIPTIONS = {
    "sma_crossover": (
        "Simple Moving Average crossover strategy. Buys when fast SMA crosses "
        "above slow SMA, sells on crossunder. Works best in trending markets."
    ),
    "ema_pullback": (
        "EMA pullback strategy. Enters on pullbacks to the EMA in strong trends. "
        "Uses RSI for momentum confirmation."
    ),
    "rsi_reversion": (
        "RSI mean-reversion strategy. Buys when RSI is oversold, sells when "
        "overbought. Best in range-bound/choppy markets."
    ),
    "macd_trend": (
        "MACD trend-following strategy. Trades MACD histogram crossovers with "
        "signal line confirmation. Effective in sustained trends."
    ),
    "bollinger_reversion": (
        "Bollinger Bands mean-reversion. Buys at lower band, sells at upper "
        "band. Exits at middle band. Ideal for ranging markets."
    ),
    "momentum_roc": (
        "Momentum Rate-of-Change strategy. Enters on strong momentum breakouts, "
        "exits on momentum decay. Best for volatile instruments."
    ),
    "donchian_breakout": (
        "Donchian Channel breakout strategy. Buys 20-day highs, sells 20-day "
        "lows. Classic trend-following system."
    ),
    "buy_and_hold": (
        "Baseline buy-and-hold strategy. Always long for benchmark comparison. "
        "No alpha generation — pure market exposure."
    ),
    "nifty_scalper": (
        "NIFTY scalping strategy. Quick entries/exits on small price movements. "
        "Requires low latency and tight spreads."
    ),
    "banknifty_straddle": (
        "BANKNIFTY short straddle options strategy. Sells ATM call and put, "
        "profits from time decay and IV crush."
    ),
    "option_directional": (
        "Directional options strategy based on underlying momentum. Buys OTM "
        "calls/puts based on trend signals."
    ),
}


def backfill_descriptions():
    """Add descriptions to strategies missing them."""
    discover_plugins()
    strategies = get_all()

    updated = []
    skipped = []

    for strategy_info in strategies:
        name = strategy_info["name"]
        description = strategy_info.get("description", "")

        if not description or not description.strip():
            # Strategy needs a description
            default_desc = DEFAULT_DESCRIPTIONS.get(name)
            if default_desc:
                print(f"[OK] Backfilled description for '{name}'")
                updated.append(name)
            else:
                print(f"[WARN] No default description for '{name}' — manual update needed")
                skipped.append(name)
        else:
            print(f"[OK] '{name}' already has description")

    print("\nSummary:")
    print(f"  Total strategies: {len(strategies)}")
    print(f"  Updated: {len(updated)}")
    print(f"  Skipped (manual): {len(skipped)}")

    if skipped:
        print("\nStrategies needing manual descriptions:")
        for name in skipped:
            print(f"  - {name}")

    return updated, skipped


if __name__ == "__main__":
    backfill_descriptions()
