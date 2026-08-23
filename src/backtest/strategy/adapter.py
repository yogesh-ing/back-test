"""Re-export of StrategyAdapter for convenience.

The canonical implementation lives in ``backtest.forward.strategy_adapter``
to keep forward-testing orchestration together. This module re-exports it
from ``backtest.strategy`` so existing code that expects
``from backtest.strategy.adapter import StrategyAdapter`` works, and to
satisfy the task tracker's note \"Bridge to existing strategy/base.py — do
not duplicate it\".

No logic is duplicated here; import the canonical module.
"""

from backtest.forward.strategy_adapter import (
    FixedDollarSizer,
    FixedQuantitySizer,
    PercentagePortfolioSizer,
    PositionSizer,
    Signal,
    SignalAction,
    SignalDirection,
    SignalType,
    StrategyAdapter,
)

__all__ = [
    "StrategyAdapter",
    "Signal",
    "SignalAction",
    "SignalType",
    "SignalDirection",
    "PositionSizer",
    "FixedQuantitySizer",
    "FixedDollarSizer",
    "PercentagePortfolioSizer",
]
