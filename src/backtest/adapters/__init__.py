"""Adapters that translate domain objects into the shape the UI expects.

The :class:`~backtest.adapters.backtest_adapter.BacktestAdapter` is the bridge
between the engine's ``BacktestResult`` and the dashboard/compare/forward pages
(PRD §4.5, Task 1.4). Every output method returns JSON-serialisable plain Python
(no numpy scalars, no pandas Timestamps).
"""

from backtest.adapters.backtest_adapter import BacktestAdapter

__all__ = ["BacktestAdapter"]
