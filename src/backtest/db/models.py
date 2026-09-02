"""SQLAlchemy ORM models for the forward testing simulator.

These models are the Python mirror of ``db/migrations/001_initial_schema.sql``.
The SQL files remain the source of truth for production deployments (they are
applied by hand, see ``db/DB-IMPLEMENTATION-GUIDE.md``); these models must be
kept byte-for-byte equivalent in *meaning* so that
``Base.metadata.create_all()`` on a fresh SQLite file produces the same shape.

Design notes
------------
Portability
    ``String`` + ``CheckConstraint`` is used instead of native PostgreSQL ENUM
    types so the identical model set runs on SQLite for local development.
    The Python ``enum.Enum`` classes below give type safety in application code
    while the DB stores plain lowercase strings.

Numeric precision
    Money and prices use :class:`~sqlalchemy.types.Numeric` with explicit
    precision, which maps to ``NUMERIC`` on PostgreSQL and returns
    :class:`decimal.Decimal`. Never switch these to ``Float``: binary floating
    point silently breaks reconciliation between the equity curve and the sum
    of trade P&L.

Cross-dialect types
    ``UUIDStr`` and ``JSONVariant`` below use ``with_variant`` so a single
    column definition yields native ``UUID``/``JSONB`` on PostgreSQL and
    ``TEXT`` on SQLite.

Timestamps
    All ``DateTime`` columns are ``timezone=True`` and MUST be written as
    timezone-aware UTC datetimes. SQLite cannot enforce this, so the discipline
    is the application's responsibility.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

# ---------------------------------------------------------------------------
# Cross-dialect column types
# ---------------------------------------------------------------------------

#: ``UUID`` on PostgreSQL, ``VARCHAR(36)`` on SQLite. Values are always the
#: canonical hyphenated string form in Python.
UUIDStr = String(36).with_variant(PGUUID(as_uuid=False), "postgresql")

#: ``JSONB`` on PostgreSQL (indexable with GIN), ``TEXT``-backed JSON elsewhere.
JSONVariant = JSON().with_variant(JSONB, "postgresql")

#: Money / P&L amounts — 4 decimal places is enough for per-fill fee accuracy.
Money = Numeric(20, 4)

#: Prices and quantities — 8 decimal places supports fractional units.
Price = Numeric(20, 8)

#: Ratios, percentages, basis points.
Ratio = Numeric(12, 6)

#: Autoincrementing surrogate key for append-only tables.
#: SQLite only autoincrements a column declared exactly ``INTEGER PRIMARY KEY``
#: — a ``BIGINT`` PK silently fails with "NOT NULL constraint failed". The
#: variant keeps BIGINT on PostgreSQL (where the row counts justify it) while
#: degrading to INTEGER on SQLite.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def _uuid4_str() -> str:
    """Client-side primary key generator.

    Generating IDs in Python (rather than relying on ``gen_random_uuid()``)
    means an ``Order`` object has a stable identity *before* it is flushed,
    which the execution engine depends on when it emits events.
    """
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata for all tables."""


# ---------------------------------------------------------------------------
# Enumerations — values match the SQL CHECK constraints exactly
# ---------------------------------------------------------------------------


class StrEnum(str, enum.Enum):
    """String-valued enum that serialises to its plain value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class PortfolioStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class PortfolioMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class PortfolioSource(StrEnum):
    SYNTHETIC = "synthetic"
    REPLAY = "replay"
    MSTOCK = "mstock"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class PositionType(StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class LiquidityFlag(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class ExitReason(StrEnum):
    SIGNAL = "signal"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    RISK_LIMIT = "risk_limit"
    MANUAL = "manual"
    EOD_FLAT = "eod_flat"


class SignalType(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Timeframe(StrEnum):
    #: Canonical timeframe vocabulary (migration 003). Every layer —
    #: API, config, DB (``market_data_cache.timeframe`` CHECK), UI,
    #: feeds — speaks these names. Aligns with
    #: :data:`~backtest.data.base.CANONICAL_TIMEFRAMES`.
    M1 = "1min"
    M5 = "5min"
    M15 = "15min"
    H1 = "1hour"
    H4 = "4hour"
    DAY = "1day"
    WEEK = "1week"


def _in_check(column: str, enum_cls: type[StrEnum]) -> str:
    """Render a SQL ``IN`` predicate from an enum, keeping SQL and Python aligned."""
    joined = ",".join(f"'{value}'" for value in enum_cls.values())
    return f"{column} IN ({joined})"


# ---------------------------------------------------------------------------
# 1. Portfolios
# ---------------------------------------------------------------------------


class Portfolio(Base):
    """A single forward-testing run. The root aggregate of the schema."""

    __tablename__ = "portfolios"

    portfolio_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Money, nullable=False)
    current_cash: Mapped[Decimal] = mapped_column(Money, nullable=False)
    base_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default=text("'INR'")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    #: Run classification (migration 002): paper = simulated fills,
    #: live = real broker orders. Pre-002 history defaults to 'paper'.
    #: Python-level ``default`` covers ORM-only inserts; ``server_default``
    #: applies at the DB level for any non-ORM writer. Both match migration 002.
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="paper", server_default=text("'paper'")
    )
    #: Where this run's bars come from (migration 002): synthetic = generated,
    #: replay = historical DB, mstock = live broker feed.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="synthetic", server_default=text("'synthetic'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    positions: Mapped[list["Position"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan", passive_deletes=True
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan", passive_deletes=True
    )
    trades: Mapped[list["Trade"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan", passive_deletes=True
    )
    equity_points: Mapped[list["EquityCurve"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_portfolios_name"),
        CheckConstraint(_in_check("status", PortfolioStatus), name="ck_portfolios_status"),
        CheckConstraint("initial_capital > 0", name="ck_portfolios_capital_pos"),
        CheckConstraint(_in_check("mode", PortfolioMode), name="ck_portfolios_mode"),
        CheckConstraint(_in_check("source", PortfolioSource), name="ck_portfolios_source"),
        Index("ix_portfolios_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Portfolio {self.name!r} status={self.status} cash={self.current_cash}>"


# ---------------------------------------------------------------------------
# 2. Positions
# ---------------------------------------------------------------------------


class Position(Base):
    """Net open exposure in one symbol. Closed rows are retained as history."""

    __tablename__ = "positions"

    position_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    portfolio_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE", name="fk_positions_portfolio"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'NSE'"))
    position_type: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'long'")
    )

    #: Signed: positive = long, negative = short, zero once closed.
    quantity: Mapped[Decimal] = mapped_column(Price, nullable=False, server_default=text("0"))
    average_entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    current_price: Mapped[Optional[Decimal]] = mapped_column(Price)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    commission_total: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    status: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'open'"))

    portfolio: Mapped["Portfolio"] = relationship(back_populates="positions")
    fills: Mapped[list["Fill"]] = relationship(back_populates="position")

    __table_args__ = (
        CheckConstraint(_in_check("status", PositionStatus), name="ck_positions_status"),
        CheckConstraint(_in_check("position_type", PositionType), name="ck_positions_type"),
        CheckConstraint("average_entry_price >= 0", name="ck_positions_avgpx"),
        CheckConstraint(
            "(status = 'closed' AND closed_at IS NOT NULL) OR "
            "(status = 'open' AND closed_at IS NULL)",
            name="ck_positions_closed_consistency",
        ),
        CheckConstraint(
            "status = 'closed' "
            "OR (position_type = 'long' AND quantity >= 0) "
            "OR (position_type = 'short' AND quantity <= 0)",
            name="ck_positions_qty_sign",
        ),
        # Partial unique index: at most one OPEN position per portfolio+symbol,
        # while allowing unlimited closed history for the same pair.
        Index(
            "uq_positions_one_open_per_symbol",
            "portfolio_id",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
        Index("ix_positions_portfolio_status", "portfolio_id", "status"),
        Index("ix_positions_symbol", "symbol"),
        Index("ix_positions_opened_at", text("opened_at DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Position {self.symbol} qty={self.quantity} status={self.status}>"


# ---------------------------------------------------------------------------
# 3. Orders
# ---------------------------------------------------------------------------


class Order(Base):
    """Order lifecycle record. Mutated only along the status/fill progression."""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    portfolio_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE", name="fk_orders_portfolio"),
        nullable=False,
    )
    position_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr, ForeignKey("positions.position_id", ondelete="SET NULL", name="fk_orders_position")
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'NSE'"))
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)

    quantity: Mapped[Decimal] = mapped_column(Price, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(
        Price, nullable=False, server_default=text("0")
    )
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Price)
    stop_price: Mapped[Optional[Decimal]] = mapped_column(Price)
    trailing_amount: Mapped[Optional[Decimal]] = mapped_column(Price)
    #: Weighted average across fills. Denormalised from ``fills`` for read speed.
    average_fill_price: Mapped[Optional[Decimal]] = mapped_column(Price)

    time_in_force: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'day'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    #: Idempotency key generated by the engine before submission.
    client_order_id: Mapped[Optional[str]] = mapped_column(String(64))
    #: Broker-assigned identifier; NULL in pure simulation.
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(64))

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    portfolio: Mapped["Portfolio"] = relationship(back_populates="orders")
    fills: Mapped[list["Fill"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(_in_check("side", OrderSide), name="ck_orders_side"),
        CheckConstraint(_in_check("order_type", OrderType), name="ck_orders_type"),
        CheckConstraint(_in_check("time_in_force", TimeInForce), name="ck_orders_tif"),
        CheckConstraint(_in_check("status", OrderStatus), name="ck_orders_status"),
        CheckConstraint("quantity > 0", name="ck_orders_qty_pos"),
        CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity", name="ck_orders_filled_qty"
        ),
        CheckConstraint(
            "order_type NOT IN ('limit','stop_limit') OR limit_price IS NOT NULL",
            name="ck_orders_limit_price_required",
        ),
        CheckConstraint(
            "order_type NOT IN ('stop','stop_limit') OR stop_price IS NOT NULL",
            name="ck_orders_stop_price_required",
        ),
        CheckConstraint(
            "order_type <> 'trailing_stop' OR trailing_amount IS NOT NULL",
            name="ck_orders_trailing_required",
        ),
        CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL",
            name="ck_orders_rejection_reason",
        ),
        CheckConstraint(
            "status <> 'filled' OR (filled_at IS NOT NULL AND filled_quantity = quantity)",
            name="ck_orders_filled_consistency",
        ),
        Index(
            "uq_orders_client_order_id",
            "portfolio_id",
            "client_order_id",
            unique=True,
            postgresql_where=text("client_order_id IS NOT NULL"),
            sqlite_where=text("client_order_id IS NOT NULL"),
        ),
        Index("ix_orders_portfolio_status", "portfolio_id", "status"),
        Index("ix_orders_symbol_submitted", "symbol", text("submitted_at DESC")),
        Index("ix_orders_position", "position_id"),
        # Hot path: the execution loop scans working orders every tick.
        Index(
            "ix_orders_working",
            "portfolio_id",
            "symbol",
            postgresql_where=text("status IN ('pending','partial')"),
            sqlite_where=text("status IN ('pending','partial')"),
        ),
    )

    @property
    def remaining_quantity(self) -> Decimal:
        """Unfilled balance. Derived, never stored."""
        return Decimal(self.quantity) - Decimal(self.filled_quantity)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Order {self.side} {self.quantity} {self.symbol} {self.order_type} {self.status}>"


# ---------------------------------------------------------------------------
# 4. Fills
# ---------------------------------------------------------------------------


class Fill(Base):
    """A single execution against an order. Append-only and immutable."""

    __tablename__ = "fills"

    fill_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    order_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("orders.order_id", ondelete="CASCADE", name="fk_fills_order"),
        nullable=False,
    )
    position_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr, ForeignKey("positions.position_id", ondelete="SET NULL", name="fk_fills_position")
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Price, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Price, nullable=False)

    commission: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    #: Signed basis points vs ``reference_price``; positive = adverse.
    slippage_bps: Mapped[Decimal] = mapped_column(Ratio, nullable=False, server_default=text("0"))
    slippage_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    exchange_fees: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    regulatory_fees: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    liquidity_flag: Mapped[Optional[str]] = mapped_column(String(8))
    #: Pre-slippage decision price, for realised-slippage attribution.
    reference_price: Mapped[Optional[Decimal]] = mapped_column(Price)

    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    order: Mapped["Order"] = relationship(back_populates="fills")
    position: Mapped[Optional["Position"]] = relationship(back_populates="fills")

    __table_args__ = (
        CheckConstraint(_in_check("side", OrderSide), name="ck_fills_side"),
        CheckConstraint(
            "liquidity_flag IS NULL OR liquidity_flag IN ('maker','taker')",
            name="ck_fills_liquidity",
        ),
        CheckConstraint("quantity > 0", name="ck_fills_qty_pos"),
        CheckConstraint("fill_price > 0", name="ck_fills_price_pos"),
        CheckConstraint(
            "commission >= 0 AND exchange_fees >= 0 AND regulatory_fees >= 0",
            name="ck_fills_fees_nonneg",
        ),
        Index("ix_fills_order", "order_id"),
        Index("ix_fills_position", "position_id"),
        Index("ix_fills_filled_at", text("filled_at DESC")),
        Index("ix_fills_symbol", "symbol", text("filled_at DESC")),
    )

    @property
    def total_fees(self) -> Decimal:
        """Commission plus all exchange and regulatory charges."""
        return (
            Decimal(self.commission) + Decimal(self.exchange_fees) + Decimal(self.regulatory_fees)
        )

    @property
    def gross_value(self) -> Decimal:
        """Notional traded, before fees."""
        return Decimal(self.quantity) * Decimal(self.fill_price)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Fill {self.side} {self.quantity} {self.symbol} @ {self.fill_price}>"


# ---------------------------------------------------------------------------
# 5. Trades
# ---------------------------------------------------------------------------


class Trade(Base):
    """A matched round-trip (entry -> exit). Written when a position closes."""

    __tablename__ = "trades"

    trade_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    portfolio_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE", name="fk_trades_portfolio"),
        nullable=False,
    )
    position_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr, ForeignKey("positions.position_id", ondelete="SET NULL", name="fk_trades_position")
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'long'"))

    entry_order_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr, ForeignKey("orders.order_id", ondelete="SET NULL", name="fk_trades_entry_order")
    )
    exit_order_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr, ForeignKey("orders.order_id", ondelete="SET NULL", name="fk_trades_exit_order")
    )

    quantity: Mapped[Decimal] = mapped_column(Price, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Price P&L before costs, sign-adjusted for direction.
    gross_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: ``gross_pnl - commission_total - slippage_total``. Hits the equity curve.
    net_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False)
    commission_total: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    slippage_total: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    holding_period_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    return_percentage: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    portfolio: Mapped["Portfolio"] = relationship(back_populates="trades")
    entry_order: Mapped[Optional["Order"]] = relationship(foreign_keys=[entry_order_id])
    exit_order: Mapped[Optional["Order"]] = relationship(foreign_keys=[exit_order_id])

    __table_args__ = (
        CheckConstraint(_in_check("direction", PositionType), name="ck_trades_direction"),
        CheckConstraint("quantity > 0", name="ck_trades_qty_pos"),
        CheckConstraint("exit_time >= entry_time", name="ck_trades_time_order"),
        CheckConstraint(
            "exit_reason IS NULL OR " + _in_check("exit_reason", ExitReason),
            name="ck_trades_exit_reason",
        ),
        Index("ix_trades_portfolio_exit", "portfolio_id", text("exit_time DESC")),
        Index("ix_trades_symbol", "symbol"),
        Index("ix_trades_strategy", "strategy_name"),
        Index("ix_trades_net_pnl", "portfolio_id", "net_pnl"),
    )

    @property
    def is_winner(self) -> bool:
        return Decimal(self.net_pnl) > 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Trade {self.symbol} {self.direction} net_pnl={self.net_pnl}>"


# ---------------------------------------------------------------------------
# 6. Equity curve
# ---------------------------------------------------------------------------


class EquityCurve(Base):
    """Mark-to-market snapshot. The authoritative performance time series."""

    __tablename__ = "equity_curve"

    equity_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE", name="fk_equity_portfolio"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    total_equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    cash: Mapped[Decimal] = mapped_column(Money, nullable=False)
    position_value: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    daily_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    cumulative_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    drawdown: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    #: Fractional (0.10 == 10%), precomputed to keep dashboard queries cheap.
    drawdown_pct: Mapped[Decimal] = mapped_column(Ratio, nullable=False, server_default=text("0"))

    portfolio: Mapped["Portfolio"] = relationship(back_populates="equity_points")

    __table_args__ = (
        # Makes the writer idempotent on restart/replay via ON CONFLICT.
        UniqueConstraint("portfolio_id", "ts", name="uq_equity_portfolio_ts"),
        Index("ix_equity_portfolio_ts", "portfolio_id", text("ts DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EquityCurve {self.ts} equity={self.total_equity}>"


# ---------------------------------------------------------------------------
# 7. Market data cache
# ---------------------------------------------------------------------------


class MarketDataCache(Base):
    """Local OHLCV cache so warm-up windows do not re-hit the broker API."""

    __tablename__ = "market_data_cache"

    data_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'NSE'"))
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Bar OPEN time, aligned to the timeframe boundary. Never bar close time.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    open: Mapped[Decimal] = mapped_column(Price, nullable=False)
    high: Mapped[Decimal] = mapped_column(Price, nullable=False)
    low: Mapped[Decimal] = mapped_column(Price, nullable=False)
    close: Mapped[Decimal] = mapped_column(Price, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    bid: Mapped[Optional[Decimal]] = mapped_column(Price)
    ask: Mapped[Optional[Decimal]] = mapped_column(Price)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'mstock'"))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in_check("timeframe", Timeframe), name="ck_mdc_timeframe"),
        # Storage-layer OHLC sanity: bad ticks cannot be persisted even if the
        # Step 11 validator is bypassed.
        CheckConstraint(
            "high >= low AND high >= open AND high >= close " "AND low <= open AND low <= close",
            name="ck_mdc_ohlc",
        ),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_mdc_prices_pos"
        ),
        CheckConstraint("volume >= 0", name="ck_mdc_volume"),
        CheckConstraint("bid IS NULL OR ask IS NULL OR bid <= ask", name="ck_mdc_spread"),
        UniqueConstraint("symbol", "exchange", "timeframe", "ts", name="uq_mdc_bar"),
        Index("ix_mdc_symbol_tf_ts", "symbol", "timeframe", text("ts DESC")),
        Index("ix_mdc_ts", text("ts DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Bar {self.symbol} {self.timeframe} {self.ts} c={self.close}>"


# ---------------------------------------------------------------------------
# 8. Performance metrics
# ---------------------------------------------------------------------------


class PerformanceMetric(Base):
    """Daily rollup produced by the Step 17 calculator. Safe to rebuild."""

    __tablename__ = "performance_metrics"

    metric_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE", name="fk_perf_portfolio"),
        nullable=False,
    )
    calculation_date: Mapped[date] = mapped_column(Date, nullable=False)

    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Fractional 0..1.
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    avg_win: Mapped[Optional[Decimal]] = mapped_column(Money)
    avg_loss: Mapped[Optional[Decimal]] = mapped_column(Money)
    largest_win: Mapped[Optional[Decimal]] = mapped_column(Money)
    largest_loss: Mapped[Optional[Decimal]] = mapped_column(Money)
    #: Gross profit / gross loss. NULL when there are no losses (undefined).
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    expectancy: Mapped[Optional[Decimal]] = mapped_column(Money)
    sharpe_ratio: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    sortino_ratio: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Money)
    max_drawdown_percentage: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    total_return: Mapped[Optional[Decimal]] = mapped_column(Money)
    total_return_percentage: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    total_commission: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    total_slippage: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default=text("0"))
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "total_trades >= 0 AND winning_trades >= 0 AND losing_trades >= 0 "
            "AND winning_trades + losing_trades <= total_trades",
            name="ck_perf_counts",
        ),
        CheckConstraint(
            "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 1)", name="ck_perf_win_rate"
        ),
        UniqueConstraint("portfolio_id", "calculation_date", name="uq_perf_portfolio_date"),
        Index("ix_perf_portfolio_date", "portfolio_id", text("calculation_date DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PerformanceMetric {self.calculation_date} trades={self.total_trades}>"


# ---------------------------------------------------------------------------
# 9. Strategy signals
# ---------------------------------------------------------------------------


class StrategySignal(Base):
    """Audit log of every signal, executed or not.

    Feeds look-ahead-bias detection in Step 22: ``bar_ts`` must always be
    strictly earlier than ``generated_at``.
    """

    __tablename__ = "strategy_signals"

    signal_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE", name="fk_signals_portfolio"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(64))
    signal_type: Mapped[str] = mapped_column(String(8), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    strength: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    #: Clipped to [-1, 1], matching the backtest engine signal contract.
    target_position: Mapped[Optional[Decimal]] = mapped_column(Ratio)
    #: Open time of the COMPLETED bar that produced this signal.
    bar_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    indicators_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONVariant)
    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    order_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr, ForeignKey("orders.order_id", ondelete="SET NULL", name="fk_signals_order")
    )
    #: Why ``executed`` is false (risk rejection, zero size, market closed...).
    skip_reason: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(_in_check("signal_type", SignalType), name="ck_signals_type"),
        CheckConstraint(_in_check("direction", SignalDirection), name="ck_signals_direction"),
        CheckConstraint(
            "strength IS NULL OR (strength >= 0 AND strength <= 1)", name="ck_signals_strength"
        ),
        CheckConstraint(
            "target_position IS NULL OR (target_position >= -1 AND target_position <= 1)",
            name="ck_signals_target",
        ),
        Index("ix_signals_portfolio_gen", "portfolio_id", text("generated_at DESC")),
        Index("ix_signals_symbol", "symbol", text("generated_at DESC")),
        Index(
            "ix_signals_unexecuted",
            "portfolio_id",
            postgresql_where=text("executed = false"),
            sqlite_where=text("executed = 0"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Signal {self.symbol} {self.signal_type}/{self.direction} executed={self.executed}>"
        )


# GIN index on the JSONB indicator snapshot. Declared outside ``__table_args__``
# because it must only be emitted on PostgreSQL — SQLite has no GIN access
# method and would fail at create_all() time.
Index(
    "ix_signals_indicators",
    StrategySignal.indicators_snapshot,
    postgresql_using="gin",
).ddl_if(dialect="postgresql")


# ---------------------------------------------------------------------------
# 10. System logs
# ---------------------------------------------------------------------------


class SystemLog(Base):
    """Structured application log, mirroring stdout for post-mortem queries."""

    __tablename__ = "system_logs"

    log_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # Logs outlive the portfolio they describe: SET NULL, never CASCADE.
    portfolio_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr,
        ForeignKey("portfolios.portfolio_id", ondelete="SET NULL", name="fk_logs_portfolio"),
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    log_level: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Emitting subsystem: order_executor, risk_manager, data_handler, ...
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack_trace: Mapped[Optional[str]] = mapped_column(Text)
    context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONVariant)

    __table_args__ = (
        CheckConstraint(_in_check("log_level", LogLevel), name="ck_logs_level"),
        Index("ix_logs_ts", text("ts DESC")),
        Index("ix_logs_portfolio_ts", "portfolio_id", text("ts DESC")),
        Index("ix_logs_level_ts", "log_level", text("ts DESC")),
        Index("ix_logs_component", "component", text("ts DESC")),
        Index(
            "ix_logs_errors",
            text("ts DESC"),
            postgresql_where=text("log_level IN ('error','critical')"),
            sqlite_where=text("log_level IN ('error','critical')"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SystemLog {self.log_level} {self.component}: {self.message[:40]}>"
