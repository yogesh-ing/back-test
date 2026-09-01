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
    ATRBasedSizer,
    FixedDollarSizer,
    FixedQuantitySizer,
    KellySizer,
    PercentagePortfolioSizer,
    PositionSizer,
    RiskBasedSizer,
    Signal,
    SignalAction,
    SignalDirection,
    SignalType,
    StrategyAdapter,
    VolatilitySizer,
)

# Also re-export the full engine from simulator for convenience
try:
    from backtest.simulator.position_sizing import PositionSizer as FullPositionSizer
    from backtest.simulator.position_sizing import (
        RiskParams,
        SizingConfig,
        SizingConstraints,
        SizingResult,
        load_position_sizing_config,
    )
except Exception:  # pragma: no cover
    FullPositionSizer = PositionSizer  # type: ignore
    SizingConfig = None  # type: ignore
    SizingConstraints = None  # type: ignore
    RiskParams = None  # type: ignore
    SizingResult = None  # type: ignore
    load_position_sizing_config = None  # type: ignore

__all__ = [
    "StrategyAdapter",
    "Signal",
    "SignalAction",
    "SignalType",
    "SignalDirection",
    "PositionSizer",
    "FullPositionSizer",
    "FixedQuantitySizer",
    "FixedDollarSizer",
    "PercentagePortfolioSizer",
    "RiskBasedSizer",
    "VolatilitySizer",
    "ATRBasedSizer",
    "KellySizer",
    "SizingConfig",
    "SizingConstraints",
    "RiskParams",
    "SizingResult",
    "load_position_sizing_config",
]
