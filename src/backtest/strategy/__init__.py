from .base import Strategy
from .registry import get_strategy, list_strategies

# Adapter is the forward-testing bridge; re-exported for convenience.
# Import lazily to avoid circular dependencies at module load time.
try:
    from .adapter import (
        FixedDollarSizer,
        FixedQuantitySizer,
        PercentagePortfolioSizer,
        Signal,
        SignalAction,
        SignalDirection,
        SignalType,
        StrategyAdapter,
    )
except Exception:  # pragma: no cover - optional dependency chain
    StrategyAdapter = None  # type: ignore
    Signal = None  # type: ignore
    SignalAction = None  # type: ignore
    SignalType = None  # type: ignore
    SignalDirection = None  # type: ignore
    FixedQuantitySizer = None  # type: ignore
    FixedDollarSizer = None  # type: ignore
    PercentagePortfolioSizer = None  # type: ignore

__all__ = [
    "Strategy",
    "get_strategy",
    "list_strategies",
    "StrategyAdapter",
    "Signal",
    "SignalAction",
    "SignalType",
    "SignalDirection",
    "FixedQuantitySizer",
    "FixedDollarSizer",
    "PercentagePortfolioSizer",
]
