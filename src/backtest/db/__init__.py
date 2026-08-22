"""Database layer for the forward testing simulator.

Exposes the SQLAlchemy declarative models, the shared ``Base`` metadata, and
the connection manager.

Typical use::

    from backtest.db import DatabaseManager, Portfolio

    with DatabaseManager.from_env() as db:
        with db.session() as s:
            s.add(Portfolio(name="run-1", initial_capital=100000,
                            current_cash=100000))
"""

from __future__ import annotations

from backtest.db.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    DatabaseConfig,
    load_config,
)
from backtest.db.manager import (
    ConnectionError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseManager,
    TransactionError,
)
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
    # Connection management
    "DatabaseManager",
    "DatabaseConfig",
    "load_config",
    "DatabaseError",
    "ConnectionError",
    "DatabaseConnectionError",
    "TransactionError",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    # Models
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
