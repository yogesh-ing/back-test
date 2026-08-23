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
    PaymentForOrderFlowCommission,
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
from backtest.simulator.execution import (
    DEFAULT_EXECUTION_CONFIG_PATH,
    ExecutionConfig,
    ExecutionEvent,
    ExecutionResult,
    ExecutionStatus,
    OrderExecutor,
    RealismLevel,
    RejectionCode,
    load_execution_config,
)
from backtest.simulator.fees import (
    BROKER_PRESETS,
    DEFAULT_BROKER_CONFIG_PATH,
    BrokerProfile,
    CommissionCalculator,
    CurrencyConverter,
    FeeBreakdown,
    FeeSchedule,
    IndiaEquityFees,
    NoStatutoryFees,
    TradeSegment,
    USEquityFees,
    get_broker_preset,
    load_broker_profile,
    resolve_fee_schedule,
)
from backtest.simulator.lots import CostBasisMethod, Lot, LotBook, LotConsumption
from backtest.simulator.slippage import (
    DEFAULT_SLIPPAGE_CONFIG_PATH,
    FixedBpsSlippage,
    HybridSlippage,
    LiquidityTier,
    MarketSnapshot,
    SlippageCalculator,
    SlippageConfig,
    SlippageEstimate,
    SlippageModel,
    SpreadSlippage,
    VolatilitySlippage,
    VolumeImpactSlippage,
    ZeroSlippage,
    load_slippage_config,
    resolve_slippage_model,
)
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
from backtest.simulator.position_sizing import (
    ATRBasedSizer,
    DEFAULT_SIZING_CONFIG_PATH,
    FixedDollarSizer,
    FixedQuantitySizer,
    KellySizer,
    PercentagePortfolioSizer,
    PositionSizer,
    RiskBasedSizer,
    RiskParams,
    SizingConfig,
    SizingConstraints,
    SizingMethod,
    SizingResult,
    VolatilitySizer,
    load_position_sizing_config,
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
    # Execution (Step 9)
    "OrderExecutor",
    "ExecutionConfig",
    "ExecutionResult",
    "ExecutionStatus",
    "ExecutionEvent",
    "RealismLevel",
    "RejectionCode",
    "load_execution_config",
    "DEFAULT_EXECUTION_CONFIG_PATH",
    # Fees (Step 8)
    "CommissionCalculator",
    "FeeBreakdown",
    "FeeSchedule",
    "IndiaEquityFees",
    "USEquityFees",
    "NoStatutoryFees",
    "BrokerProfile",
    "TradeSegment",
    "CurrencyConverter",
    "BROKER_PRESETS",
    "get_broker_preset",
    "load_broker_profile",
    "resolve_fee_schedule",
    "DEFAULT_BROKER_CONFIG_PATH",
    # Commission
    "CommissionModel",
    "ZeroCommission",
    "FlatCommission",
    "PerShareCommission",
    "PercentageCommission",
    "TieredCommission",
    "PaymentForOrderFlowCommission",
    "resolve_commission_model",
    # Slippage
    "SlippageCalculator",
    "SlippageConfig",
    "SlippageEstimate",
    "SlippageModel",
    "ZeroSlippage",
    "FixedBpsSlippage",
    "SpreadSlippage",
    "VolumeImpactSlippage",
    "VolatilitySlippage",
    "HybridSlippage",
    "LiquidityTier",
    "MarketSnapshot",
    "resolve_slippage_model",
    "load_slippage_config",
    "DEFAULT_SLIPPAGE_CONFIG_PATH",
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
    # Position sizing (Step 14)
    "PositionSizer",
    "SizingConfig",
    "SizingConstraints",
    "RiskParams",
    "SizingMethod",
    "SizingResult",
    "FixedQuantitySizer",
    "FixedDollarSizer",
    "PercentagePortfolioSizer",
    "RiskBasedSizer",
    "VolatilitySizer",
    "ATRBasedSizer",
    "KellySizer",
    "load_position_sizing_config",
    "DEFAULT_SIZING_CONFIG_PATH",
]