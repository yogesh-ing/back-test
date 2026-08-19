"""Forward testing simulator — domain models.

Pure in-memory trading domain logic for the forward testing simulator
(Steps 3-6 and 20 of ``instructions/forword-testing.md``).

Layering rule
-------------
This package must not import from ``backtest.engine`` or ``backtest.forward``.
It reaches the database only through :class:`backtest.db.DatabaseManager`, so
every model stays unit-testable with no I/O.

Naming
------
:class:`backtest.simulator.Portfolio` is the *domain* model — cash, positions
and risk limits. It is distinct from:

* :class:`backtest.db.models.Portfolio` — the ORM row it persists to
* :class:`backtest.forward.portfolio.Portfolio` — the older multi-strategy
  allocation helper used by the walk-forward paper trader

Money
-----
Every monetary value is a :class:`~decimal.Decimal`. Never introduce floats
here; see :mod:`backtest.simulator.money` for the reasoning.
"""

from __future__ import annotations

from backtest.simulator.errors import (
    DuplicatePositionError,
    InsufficientFundsError,
    LimitExceededError,
    PortfolioStateError,
    PositionError,
    PositionNotFoundError,
    ShortSellingNotAllowedError,
    SimulatorError,
    ValidationError,
)
from backtest.simulator.money import (
    MONEY_PLACES,
    PRICE_PLACES,
    ZERO,
    money,
    price,
    to_decimal,
)
from backtest.simulator.portfolio import (
    EquityPoint,
    Portfolio,
    PortfolioLimits,
    PortfolioStatus,
    PositionCheck,
)
from backtest.simulator.enums import (
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    WORKING_STATUSES,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from backtest.simulator.commission import (
    CommissionModel,
    FlatCommission,
    PercentageCommission,
    PerShareCommission,
    TieredCommission,
    ZeroCommission,
    resolve_commission_model,
)
from backtest.simulator.fill import (
    Fill,
    LiquidityFlag,
    PositionAction,
    PositionImpact,
)
from backtest.simulator.lots import CostBasisMethod, Lot, LotBook, LotConsumption
from backtest.simulator.order import (
    FillLike,
    InvalidTransitionError,
    Order,
    OrderEvent,
    OrderValidationError,
    StatusChange,
)
from backtest.simulator.position import (
    DividendResult,
    Position,
    PositionType,
    ReduceResult,
    SplitResult,
)

__all__ = [
    # Portfolio
    "Portfolio",
    "PortfolioLimits",
    "PortfolioStatus",
    "PositionCheck",
    "EquityPoint",
    # Position
    "Position",
    "PositionType",
    "ReduceResult",
    "SplitResult",
    "DividendResult",
    # Order
    "Order",
    "OrderEvent",
    "StatusChange",
    "FillLike",
    "OrderValidationError",
    "InvalidTransitionError",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "TimeInForce",
    "TERMINAL_STATUSES",
    "WORKING_STATUSES",
    "VALID_TRANSITIONS",
    # Fill
    "Fill",
    "LiquidityFlag",
    "PositionImpact",
    "PositionAction",
    # Commission
    "CommissionModel",
    "ZeroCommission",
    "FlatCommission",
    "PerShareCommission",
    "PercentageCommission",
    "TieredCommission",
    "resolve_commission_model",
    # Tax lots
    "CostBasisMethod",
    "Lot",
    "LotBook",
    "LotConsumption",
    # Errors
    "SimulatorError",
    "ValidationError",
    "InsufficientFundsError",
    "PositionError",
    "PositionNotFoundError",
    "DuplicatePositionError",
    "LimitExceededError",
    "ShortSellingNotAllowedError",
    "PortfolioStateError",
    # Money
    "money",
    "price",
    "to_decimal",
    "ZERO",
    "MONEY_PLACES",
    "PRICE_PLACES",
]
