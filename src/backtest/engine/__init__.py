"""Engine package — backtest + execution engine."""

from backtest.engine.execution_engine import ExecutionEngine, Fill, OrderRejected, RiskHalted, get_engine, reset_engine
from backtest.engine.strategy_adapter import StrategyAdapter, adapt_strategy, adapt_strategy_many

__all__ = [
    "ExecutionEngine",
    "Fill",
    "OrderRejected",
    "RiskHalted",
    "get_engine",
    "reset_engine",
    "StrategyAdapter",
    "adapt_strategy",
    "adapt_strategy_many",
]
