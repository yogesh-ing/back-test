"""Live market data layer for the forward testing simulator (Step 10).

Normalizes broker payloads into one standard tick format, aggregates ticks
into boundary-aligned OHLCV bars, buffers recent data in bounded memory, and
publishes to observers. The concrete broker feed is mStock (``live/``), per
deviation #4 in the task tracker; a :class:`MockFeed` powers tests and
offline replays.

Layering rule
-------------
Pure in-memory except two edges: the injected :class:`DataFeed`, and
:meth:`MarketDataHandler.persist_closed_bars`, which reaches the database
only through :class:`backtest.db.DatabaseManager` — same rule as
``backtest.simulator``. This package must not import from
``backtest.engine`` or ``backtest.forward``.
"""

from __future__ import annotations

from backtest.marketdata.bars import (
    INTRADAY_MINUTES,
    AggregatorStats,
    BarAggregator,
    Timeframe,
    align_to_boundary,
    next_boundary,
)
from backtest.marketdata.errors import (
    FeedConnectionError,
    FeedError,
    MarketDataError,
    NormalizationError,
)
from backtest.marketdata.feed import DataFeed, MockFeed, MStockFeed
from backtest.marketdata.handler import (
    DEFAULT_MARKETDATA_CONFIG_PATH,
    MarketDataConfig,
    MarketDataHandler,
    load_marketdata_config,
)
from backtest.marketdata.quality import (
    DEFAULT_QUALITY_CONFIG_PATH,
    Action,
    BadDataPolicy,
    DataValidator,
    QualityConfig,
    Severity,
    Strictness,
    ValidationIssue,
    ValidationResult,
    load_quality_config,
)
from backtest.marketdata.ticks import Bar, Tick, normalize_tick, parse_timestamp

__all__ = [
    # errors
    "MarketDataError",
    "NormalizationError",
    "FeedError",
    "FeedConnectionError",
    # ticks & bars
    "Tick",
    "Bar",
    "normalize_tick",
    "parse_timestamp",
    "Timeframe",
    "INTRADAY_MINUTES",
    "align_to_boundary",
    "next_boundary",
    "BarAggregator",
    "AggregatorStats",
    # feeds
    "DataFeed",
    "MockFeed",
    "MStockFeed",
    # handler
    "MarketDataConfig",
    "MarketDataHandler",
    "load_marketdata_config",
    "DEFAULT_MARKETDATA_CONFIG_PATH",
    # quality (Step 11)
    "DataValidator",
    "QualityConfig",
    "ValidationIssue",
    "ValidationResult",
    "Action",
    "Severity",
    "Strictness",
    "BadDataPolicy",
    "load_quality_config",
    "DEFAULT_QUALITY_CONFIG_PATH",
]
