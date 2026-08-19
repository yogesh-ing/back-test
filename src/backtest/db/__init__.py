"""Database layer for the forward testing simulator.

Exposes the SQLAlchemy declarative models and the shared ``Base`` metadata.
The connection manager arrives in Step 2; until then, construct engines
directly with :func:`sqlalchemy.create_engine`.
"""

from __future__ import annotations

from backtest.db.models import (
    Base,
    EquityCurve,
    Fill,
    MarketDataCache,
    Order,
    PerformanceMetric,
    Portfolio,
    Position,
    StrategySignal,
    SystemLog,
    Trade,
    # Enums
    ExitReason,
    LiquidityFlag,
    LogLevel,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioStatus,
    PositionStatus,
    PositionType,
    SignalDirection,
    SignalType,
    TimeInForce,
    Timeframe,
)

__all__ = [
    "Base",
    "Portfolio",
    "Position",
    "Order",
    "Fill",
    "Trade",
    "EquityCurve",
    "MarketDataCache",
    "PerformanceMetric",
    "StrategySignal",
    "SystemLog",
    "PortfolioStatus",
    "PositionStatus",
    "PositionType",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "TimeInForce",
    "LiquidityFlag",
    "ExitReason",
    "SignalType",
    "SignalDirection",
    "LogLevel",
    "Timeframe",
]
