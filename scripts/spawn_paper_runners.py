#!/usr/bin/env python
"""Spawn high-frequency paper trading runners for validation.

Directly creates PaperRunner instances with mStock live feed, bypassing
the data fetch requirement since we want LIVE market data, not historical.
"""

import sys
import os
from pathlib import Path

# Add src to path
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ["BACKTEST_DATA_PROFILE"] = "testing"  # Enable synthetic as fallback

from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.paper_runner import RunnerConfig


def spawn_equity_scalpers():
    """Spawn equity scalp runners on RELIANCE, TCS, INFY."""
    
    symbols = ["RELIANCE", "TCS", "INFY"]
    manager = PortfolioManager()
    
    for symbol in symbols:
        config = RunnerConfig(
            name=f"HF_Scalp_{symbol}",
            strategy_name="hf_equity_scalp",
            symbols=[symbol],
            allocated_capital=100_000,
            source="mstock",  # Live mStock feed
            timeframe="1min",
            strategy_params={
                "take_profit": 3.0,
                "stop_loss": 2.0,
                "max_hold_bars": 5,
            },
            instrument={"type": "equity"},
        )
        
        runner_id = manager.spawn(config)
        print(f"✓ Spawned HF_Scalp on {symbol} (runner_id={runner_id})")


def spawn_nifty_options():
    """Spawn NIFTY options instant-buy runner."""
    
    manager = PortfolioManager()
    
    config = RunnerConfig(
        name="Nifty_InstantBuy",
        strategy_name="nifty_instant_buy",
        symbols=["NIFTY"],
        allocated_capital=200_000,
        source="mstock",
        timeframe="1min",
        strategy_params={
            "underlying": "NIFTY",
            "take_profit_pct": 20.0,
            "stop_loss_pct": 10.0,
            "max_hold_bars": 10,
        },
        instrument={"type": "option"},
    )
    
    runner_id = manager.spawn(config)
    print(f"✓ Spawned Nifty_InstantBuy options (runner_id={runner_id})")


if __name__ == "__main__":
    print("=" * 70)
    print("SPAWNING HIGH-FREQUENCY PAPER TRADING RUNNERS")
    print("=" * 70)
    print()
    
    print("Spawning equity scalpers...")
    spawn_equity_scalpers()
    print()
    
    print("Spawning NIFTY options runner...")
    spawn_nifty_options()
    print()
    
    print("=" * 70)
    print("All runners spawned! Check http://127.0.0.1:5000/portfolio/paper")
    print("=" * 70)
