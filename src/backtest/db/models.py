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
    #: Second broker venue (migration 016 widened the DB CHECK to admit it).
    DHAN = "dhan"
    #: Third broker venue (migration 020 widened the DB CHECK to admit it).
    UPSTOX = "upstox"


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
    #: Where this run's bars come from (migration 002, widened by 016/020):
    #: synthetic = generated, replay = historical DB, mstock | dhan | upstox
    #: = live broker feed.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="synthetic", server_default=text("'synthetic'")
    )
    #: Capital partition this run belongs to (migration 008): '' = none, legacy.
    segment: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    #: Broker the runner routes orders to (migration 008): '' = default routing.
    execution_broker: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("''")
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
    #: Venue classification (migration 008): 'paper' = simulated fills, else
    #: the broker the position's orders were routed to.
    broker: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'paper'"))

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
        Index("ix_positions_broker", "broker"),
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
    #: Venue classification (migration 008): 'paper' = simulated fills, else
    #: the broker the order was routed to.
    broker: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'paper'"))

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
        Index("ix_orders_broker", "broker"),
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
        # Timeframe-leading so the freshness chip's per-timeframe
        # ``ORDER BY ts DESC LIMIT 1`` is an O(log n) seek for EMPTY
        # timeframes too (13.7s full scan without it, 2026-10-04).
        Index("ix_mdc_tf_ts", "timeframe", text("ts DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Bar {self.symbol} {self.timeframe} {self.ts} c={self.close}>"


# ---------------------------------------------------------------------------
# 7b. Corporate actions (review §3.3 / gap 12c — adjusted-price policy store)
# ---------------------------------------------------------------------------


class CorporateActionRecord(Base):
    """Split / bonus / dividend events per symbol.

    The adjustment POLICY applies these at read time (``AdjustedSource``);
    raw bars in ``market_data_cache`` are never rewritten. ``factor`` is the
    pre-ex-date price multiplier: split ₹10 → ₹1 ⇒ 0.10; bonus 1:1 ⇒ 0.50;
    dividends default to 1.0 (recorded, price-neutral).
    """

    __tablename__ = "corporate_actions"

    action_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    #: First day the post-action price trades. Bars strictly before this are
    #: back-adjusted by ``factor``.
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    factor: Mapped[Decimal] = mapped_column(Price, nullable=False, server_default=text("1"))
    #: Dividend rupees per share (informational; policy does not auto-adjust).
    amount: Mapped[Optional[Decimal]] = mapped_column(Money)
    note: Mapped[Optional[str]] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'manual'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('split', 'bonus', 'dividend')", name="ck_ca_kind"),
        CheckConstraint("factor > 0", name="ck_ca_factor_pos"),
        # One event per symbol/day/kind — re-announcements are updates, not rows.
        UniqueConstraint("symbol", "ex_date", "kind", name="uq_ca_event"),
        Index("ix_ca_symbol_exdate", "symbol", "ex_date"),
    )

    def to_action(self):
        """→ ``backtest.data.corporate_actions.CorporateAction`` (lazy import)."""
        from backtest.data.corporate_actions import CorporateAction

        return CorporateAction(
            symbol=self.symbol,
            ex_date=self.ex_date,
            kind=self.kind,
            adjustment_factor=float(self.factor),
            amount=float(self.amount) if self.amount is not None else None,
            note=self.note or "",
        )

    @classmethod
    def ensure_schema(cls, manager: Any) -> None:
        """Create just this table (same convention as the snapshot asset)."""
        engine = manager.engine if hasattr(manager, "engine") else manager
        Base.metadata.create_all(engine, tables=[cls.__table__])

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<CorporateAction {self.symbol} {self.kind} x{float(self.factor):g} "
            f"ex={self.ex_date}>"
        )


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


# ---------------------------------------------------------------------------
# 11. Option trade structures (Gap-Analysis G4.2)
# ---------------------------------------------------------------------------


class TradeStructureStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


class TradeStructure(Base):
    """A persisted option structure (one row per executed structure).

    Gap G4.2: the options paper book was in-process memory only, so a server
    restart lost every open position. This table is the durable record —
    one row per structure, with its legs serialised into ``legs`` so the
    in-memory :class:`~backtest.options.paper_trading.StructurePosition`
    can be rehydrated byte-for-byte on startup.

    ``entry_cost`` / ``fees_paid`` capture the opening cash debit (net
    premium, and the statutory fee stack at entry) so a rehydrated book
    restores ``available_cash`` exactly.
    """

    __tablename__ = "trade_structures"

    structure_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    structure_type: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'open'")
    )

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    #: Net premium paid to open (debit positive, credit negative).
    entry_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    #: Statutory fee stack charged at entry (STT, exchange, SEBI, stamp, GST).
    fees_paid: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )
    #: Gross realized P&L across legs once closed/expired.
    realized_pnl: Mapped[Decimal] = mapped_column(
        Money, nullable=False, server_default=text("0")
    )

    #: Serialised leg snapshots (see options.persistence for the shape).
    legs: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, server_default=text("'{}'")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            _in_check("status", TradeStructureStatus),
            name="ck_trade_structures_status",
        ),
        Index("ix_trade_structures_status", "status"),
        Index("ix_trade_structures_underlying", "underlying"),
        Index("ix_trade_structures_opened", text("opened_at DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TradeStructure {self.structure_type} {self.underlying} {self.status}>"


class OptionChainSnapshot(Base):
    """One contract's state in one option-chain snapshot batch (P1.1).

    The data asset behind options *research*: every snapshot run appends the
    full contract terms of the chain (from the instrument master — one API
    call) plus L1 quotes where available, so backtests can eventually price
    off recorded premiums/IV instead of Black-Scholes
    (see ``docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md`` E-6).

    Append-only: each run writes a new batch sharing one ``snapshot_ts``;
    there is no update path. Research reads go through
    :func:`backtest.options.chain_snapshots.load_snapshots`.
    """

    __tablename__ = "option_chain_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Batch timestamp — one value shared by every row of one snapshot run.
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Data source tag ("mstock" today; vendors plug in here).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    underlying: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal] = mapped_column(Money, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    instrument_token: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_size: Mapped[Optional[int]] = mapped_column(Integer)

    #: L1 quotes — nullable because contract terms are captured even when a
    #: quote call fails or is skipped (rate-limit budget).
    ltp: Mapped[Optional[Decimal]] = mapped_column(Money)
    bid: Mapped[Optional[Decimal]] = mapped_column(Money)
    ask: Mapped[Optional[Decimal]] = mapped_column(Money)
    volume: Mapped[Optional[int]] = mapped_column(Integer)
    oi: Mapped[Optional[int]] = mapped_column(Integer)
    #: Underlying spot at snapshot time (when the caller has one).
    spot: Mapped[Optional[Decimal]] = mapped_column(Money)

    __table_args__ = (
        CheckConstraint(
            option_type.in_(["CE", "PE"]), name="ck_option_chain_snapshots_type"
        ),
        Index("ix_chain_snapshots_ts", "snapshot_ts"),
        Index("ix_chain_snapshots_underlying", "underlying"),
        Index(
            "ix_chain_snapshots_lookup",
            "underlying",
            "expiry",
            "option_type",
            "strike",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<OptionChainSnapshot {self.underlying} {self.expiry} "
            f"{self.strike}{self.option_type}>"
        )


# ====================================================================
# Parameter Optimization Engine (PRD "Parameter Optimization Engine" v1.0)
# ===========================================================================
#
# Mirrors Alembic revisions 009-013 / db/migrations/009-013_*.sql. PostgreSQL
# is the production target (native UUID + JSONB, GIN index on result params,
# updated_at triggers, analytics views); the same models run on SQLite for
# local development and the test suite via ``create_all``.


class OptimizationStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OptimizationObjective(StrEnum):
    SHARPE = "sharpe"
    SORTINO = "sortino"
    CALMAR = "calmar"
    TOTAL_RETURN = "total_return"
    PROFIT_FACTOR = "profit_factor"
    EXPECTANCY = "expectancy"


class OptimizationMethod(StrEnum):
    GRID = "grid"
    RANDOM = "random"
    BAYESIAN = "bayesian"
    GENETIC = "genetic"


class PresetSource(StrEnum):
    OPTIMIZATION = "optimization"
    MANUAL = "manual"
    DEFAULT = "default"
    IMPORT = "import"
    #: Automatic "params before apply" snapshot kept for one-click rollback.
    SNAPSHOT = "snapshot"


#: Ratio / score columns (Sharpe, returns as decimals, drawdown...).
Score = Numeric(10, 4)
#: Per-trade currency amounts on result rows (PRD precision).
TradeMoney = Numeric(10, 2)


class OptimizationRun(Base):
    """One optimization job: config, lifecycle, best result, WF verdict.

    Columns beyond the PRD schema (``baseline_*``, ``analysis``,
    ``robustness_score``) hold the original-vs-optimized comparison and the
    sensitivity/robustness output the results dashboard renders, so a
    completed run is fully reproducible from this row plus its results.
    """

    __tablename__ = "optimization_runs"

    run_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    bucket_id: Mapped[Optional[str]] = mapped_column(String(100))

    # -- configuration ------------------------------------------------------
    objective_function: Mapped[str] = mapped_column(String(50), nullable=False)
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    param_space: Mapped[Any] = mapped_column(JSONVariant, nullable=False)
    constraints: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    backtest_config: Mapped[Any] = mapped_column(JSONVariant, nullable=False)

    # -- lifecycle ----------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # -- progress -----------------------------------------------------------
    total_combinations: Mapped[Optional[int]] = mapped_column(Integer)
    tested_combinations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    valid_combinations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # -- results summary ----------------------------------------------------
    best_params: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    best_score: Mapped[Optional[Decimal]] = mapped_column(Score)
    best_metrics: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    # -- original-vs-optimized comparison (extension) ------------------------
    baseline_params: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    baseline_metrics: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    baseline_score: Mapped[Optional[Decimal]] = mapped_column(Score)

    # -- walk-forward -------------------------------------------------------
    walk_forward_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    walk_forward_config: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    walk_forward_results: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    overfitted: Mapped[Optional[bool]] = mapped_column(Boolean)
    avg_train_score: Mapped[Optional[Decimal]] = mapped_column(Score)
    avg_test_score: Mapped[Optional[Decimal]] = mapped_column(Score)

    # -- analytics (extension): sensitivity, equity curves, warnings --------
    analysis: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    robustness_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2))
    # PRD Part 2 §3. The Sharpe the best-of-N had to beat, on the same scale
    # as `best_metrics.sharpe`. The full statistic (including the probability)
    # is in `analysis["deflated_sharpe"]`; this column is the one number you
    # would want to sort or filter on.
    deflated_sharpe: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3))

    # -- data attestation (PRD Part 2 §2, migration 015) --------------------
    # What this run actually ran on, measured on the candles the search loaded
    # rather than on the range that was requested. All nullable and all
    # unbackfilled on purpose: a row written before this migration has no
    # attestation, and that is the truth of it. A guessed value here would be a
    # claim nobody checked.
    #
    # The flat columns exist so runs can be listed and filtered by source
    # without unpacking JSON in every query; `data_attestation` holds the whole
    # record, including staleness and the synthetic acknowledgement. They are
    # written together by `attestation.attestation_columns()` and nowhere else,
    # so the two cannot drift.
    data_source: Mapped[Optional[str]] = mapped_column(String(30))
    data_fetch_date: Mapped[Optional[date]] = mapped_column(Date)
    bars_count: Mapped[Optional[int]] = mapped_column(Integer)
    symbol: Mapped[Optional[str]] = mapped_column(String(30))
    timeframe: Mapped[Optional[str]] = mapped_column(String(10))
    date_from: Mapped[Optional[date]] = mapped_column(Date)
    date_to: Mapped[Optional[date]] = mapped_column(Date)
    data_attestation: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    # -- metadata -----------------------------------------------------------
    created_by: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
    task_id: Mapped[Optional[str]] = mapped_column(String(100))

    results: Mapped[list["OptimizationResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(_in_check("status", OptimizationStatus), name="ck_opt_runs_status"),
        CheckConstraint(
            _in_check("objective_function", OptimizationObjective),
            name="ck_opt_runs_objective",
        ),
        CheckConstraint(_in_check("method", OptimizationMethod), name="ck_opt_runs_method"),
        CheckConstraint("tested_combinations >= 0", name="ck_opt_runs_tested_nonneg"),
        CheckConstraint("valid_combinations >= 0", name="ck_opt_runs_valid_nonneg"),
        Index("idx_opt_runs_strategy", "strategy_id"),
        Index("idx_opt_runs_status", "status"),
        Index("idx_opt_runs_created", "created_at"),
        Index("idx_opt_runs_strategy_status", "strategy_id", "status"),
        Index("idx_opt_runs_bucket", "bucket_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OptimizationRun {self.strategy_id} {self.method} {self.status}>"


class OptimizationResult(Base):
    """One tested parameter combination and its standardized metrics."""

    __tablename__ = "optimization_results"

    result_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    run_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey("optimization_runs.run_id", ondelete="CASCADE", name="fk_opt_results_run"),
        nullable=False,
    )
    params: Mapped[Any] = mapped_column(JSONVariant, nullable=False)

    # -- core metrics -------------------------------------------------------
    sharpe: Mapped[Optional[Decimal]] = mapped_column(Score)
    sortino: Mapped[Optional[Decimal]] = mapped_column(Score)
    calmar: Mapped[Optional[Decimal]] = mapped_column(Score)
    #: Decimal fraction: 0.124 = 12.4 %.
    total_return: Mapped[Optional[Decimal]] = mapped_column(Score)
    cagr: Mapped[Optional[Decimal]] = mapped_column(Score)
    #: Negative decimal fraction: -0.083 = -8.3 %.
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Score)
    drawdown_duration_days: Mapped[Optional[int]] = mapped_column(Integer)

    # -- trade statistics ---------------------------------------------------
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Score)
    #: Percentage: 54.20 = 54.2 % of closed trades were winners.
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    total_trades: Mapped[Optional[int]] = mapped_column(Integer)
    winning_trades: Mapped[Optional[int]] = mapped_column(Integer)
    losing_trades: Mapped[Optional[int]] = mapped_column(Integer)
    expectancy: Mapped[Optional[Decimal]] = mapped_column(TradeMoney)
    avg_win: Mapped[Optional[Decimal]] = mapped_column(TradeMoney)
    avg_loss: Mapped[Optional[Decimal]] = mapped_column(TradeMoney)
    largest_win: Mapped[Optional[Decimal]] = mapped_column(TradeMoney)
    largest_loss: Mapped[Optional[Decimal]] = mapped_column(TradeMoney)

    # -- risk ---------------------------------------------------------------
    volatility: Mapped[Optional[Decimal]] = mapped_column(Score)
    downside_deviation: Mapped[Optional[Decimal]] = mapped_column(Score)

    # -- execution ----------------------------------------------------------
    avg_holding_time_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    avg_slippage: Mapped[Optional[Decimal]] = mapped_column(Score)

    # -- constraints & ranking ----------------------------------------------
    constraints_met: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    constraint_violations: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    objective_score: Mapped[Decimal] = mapped_column(Score, nullable=False)
    rank: Mapped[Optional[int]] = mapped_column(Integer)

    full_result: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[OptimizationRun] = relationship(back_populates="results")

    __table_args__ = (
        CheckConstraint("rank IS NULL OR rank >= 1", name="ck_opt_results_rank_pos"),
        CheckConstraint(
            "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)",
            name="ck_opt_results_win_rate",
        ),
        Index("idx_opt_results_run", "run_id"),
        Index(
            "idx_opt_results_rank",
            "run_id",
            "rank",
            postgresql_where=text("rank IS NOT NULL"),
            sqlite_where=text("rank IS NOT NULL"),
        ),
        Index("idx_opt_results_score", "run_id", "objective_score"),
        Index("idx_opt_results_constraints", "run_id", "constraints_met"),
        # JSONB containment search (params @> '{"fast": 10}') — PostgreSQL only.
        Index("idx_opt_results_params_gin", "params", postgresql_using="gin").ddl_if(
            dialect="postgresql"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OptimizationResult rank={self.rank} score={self.objective_score}>"


class ParameterPreset(Base):
    """A named, reusable parameter set for a strategy."""

    __tablename__ = "parameter_presets"

    preset_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    params: Mapped[Any] = mapped_column(JSONVariant, nullable=False)

    source: Mapped[str] = mapped_column(String(50), nullable=False)
    optimization_run_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr,
        ForeignKey(
            "optimization_runs.run_id", ondelete="SET NULL", name="fk_presets_opt_run"
        ),
    )
    #: Backtest-run ledger lineage (migration 018, PRD R3): set when the
    #: preset is saved from a plain backtest run ("Promote to Forward" anchor).
    backtest_run_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr,
        ForeignKey(
            "backtest_runs.run_id", ondelete="SET NULL", name="fk_presets_backtest_run"
        ),
    )
    backtest_metrics: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    applied_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("strategy_id", "name", name="uq_strategy_preset_name"),
        CheckConstraint(_in_check("source", PresetSource), name="ck_presets_source"),
        CheckConstraint("applied_count >= 0", name="ck_presets_applied_nonneg"),
        Index("idx_presets_strategy", "strategy_id"),
        Index(
            "idx_presets_active",
            "strategy_id",
            "is_active",
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
        Index("idx_presets_source", "source"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ParameterPreset {self.strategy_id}:{self.name}>"


class OptimizationAudit(Base):
    """Append-only audit trail: who changed which parameters, when, where."""

    __tablename__ = "optimization_audit"

    audit_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    run_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr,
        ForeignKey("optimization_runs.run_id", ondelete="SET NULL", name="fk_audit_opt_run"),
    )
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)

    action: Mapped[str] = mapped_column(String(50), nullable=False)
    action_details: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    old_params: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    new_params: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    params_diff: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    applied_to_bucket: Mapped[Optional[str]] = mapped_column(String(100))
    applied_to_mode: Mapped[Optional[str]] = mapped_column(String(20))
    runner_restarted: Mapped[Optional[bool]] = mapped_column(Boolean)

    expected_impact: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    actual_impact: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    approved_by: Mapped[Optional[str]] = mapped_column(String(100))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user_id: Mapped[Optional[str]] = mapped_column(String(100))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint(
            "applied_to_mode IS NULL OR applied_to_mode IN ('paper', 'live')",
            name="ck_audit_mode",
        ),
        Index("idx_audit_strategy", "strategy_id"),
        Index("idx_audit_timestamp", "timestamp"),
        Index("idx_audit_action", "action"),
        Index("idx_audit_user", "user_id"),
        Index("idx_audit_bucket", "applied_to_bucket"),
        Index("idx_audit_run", "run_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OptimizationAudit {self.action} {self.strategy_id}>"


#: Tables owned by the optimization engine (used for scoped ``create_all``).
OPTIMIZATION_TABLES = (
    OptimizationRun.__table__,
    OptimizationResult.__table__,
    ParameterPreset.__table__,
    OptimizationAudit.__table__,
)
# ---------------------------------------------------------------------------
# Portfolio Intelligence & Alerts (migration 005)
# ---------------------------------------------------------------------------


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class PortfolioAlert(Base):
    """Alert audit trail — one row per alert, updated through its lifecycle.

    Written by :class:`backtest.intelligence.persistence.IntelligencePersister`
    (fail-soft, off the tick thread). ``alert_key`` is the dedupe identity
    (``type:subject``); ``notified_strategies`` records which strategy
    subscribers were called and whether their callback succeeded.
    """

    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False)
    alert_key: Mapped[str] = mapped_column(String(200), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismissed_by: Mapped[Optional[str]] = mapped_column(String(100))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notified_strategies: Mapped[Optional[list[Any]]] = mapped_column(JSONVariant)

    __table_args__ = (
        CheckConstraint(_in_check("severity", AlertSeverity), name="ck_alerts_severity"),
        Index("idx_alerts_type", "alert_type"),
        Index("idx_alerts_created", text("created_at DESC")),
        Index("idx_alerts_active", "alert_type", "resolved_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PortfolioAlert {self.alert_type} {self.severity}>"


class PortfolioGreeksSnapshot(Base):
    """Periodic portfolio Greeks snapshot (historical analysis, not per tick).

    Units: ``net_delta`` share-equivalents, ``net_gamma`` Δ per 1% move,
    ``net_vega`` ₹ per IV point, ``net_theta`` ₹ per day.
    """

    __tablename__ = "portfolio_greeks_history"

    snapshot_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    net_delta: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2))
    net_gamma: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2))
    net_vega: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2))
    net_theta: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2))
    greeks_by_strategy: Mapped[Optional[list[Any]]] = mapped_column(JSONVariant)
    concentration_by_underlying: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONVariant)
    concentration_by_strike: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONVariant)

    __table_args__ = (Index("idx_greeks_time", text("timestamp DESC")),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PortfolioGreeksSnapshot {self.timestamp}>"


class MarketRegimeSnapshot(Base):
    """Market regime samples (5-minute cadence + every transition)."""

    __tablename__ = "market_regime_history"

    regime_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    regime: Mapped[str] = mapped_column(String(20), nullable=False)
    vix: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    realized_vol: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    source: Mapped[Optional[str]] = mapped_column(String(64))
    previous_regime: Mapped[Optional[str]] = mapped_column(String(20))
    regime_changed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (Index("idx_regime_time", text("timestamp DESC")),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MarketRegimeSnapshot {self.regime} {self.vix}>"


# ---------------------------------------------------------------------------
# 9. Cost & Risk Settings — broker cost-model profiles (certified panel, v2 §0)
# ---------------------------------------------------------------------------


class BrokerProfileRow(Base):
    """An editable broker cost model (Phase 1 of the certified settings panel).

    One row per broker profile. Built-in presets are seeded from
    ``config/brokers.yaml`` / ``BROKER_PRESETS`` with ``is_preset=True`` and
    are shown read-only in the UI; user overrides/edits are separate rows
    (or the preset row updated by an admin — the audit table records which).

    ``commission_model`` / ``statutory_rates`` are JSON dicts in the exact
    shape :class:`~backtest.simulator.fees.BrokerProfile` accepts (see
    ``to_profile()``), so the fee engine needs no adapter code.
    """

    __tablename__ = "broker_profiles"

    @classmethod
    def ensure_schema(cls, manager: Any) -> None:
        """Create just this table (same convention as the snapshot asset)."""
        engine = manager.engine if hasattr(manager, "engine") else manager
        Base.metadata.create_all(engine, tables=[cls.__table__])

    profile_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    profile_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_preset: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    default_segment: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="equity_delivery"
    )
    commission_model: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, server_default=text("'{}'")
    )
    statutory_rates: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, server_default=text("'{}'")
    )
    minimum_commission: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    validated_on: Mapped[Optional[date]] = mapped_column(Date)
    contract_note_ref: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    def to_profile_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``BrokerProfile(**kwargs)`` (validated there).

        The stored ``commission_model`` is the composite the panel edits
        (``default`` / ``delivery`` / ``options`` sub-models); it unpacks to
        the three separate BrokerProfile fields here.
        """
        composite = dict(self.commission_model or {})
        default = composite.get("default")
        if not isinstance(default, dict):
            default = composite if composite else {"model": "zero"}
        return {
            "name": self.profile_id,
            "commission_model": default,
            "delivery_commission_model": composite.get("delivery"),
            "options_commission_model": composite.get("options"),
            "fee_schedule": {"schedule": "india_equity", **(self.statutory_rates or {})},
            "minimum_commission": self.minimum_commission,
            "currency": self.currency,
            "default_segment": self.default_segment,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BrokerProfileRow {self.profile_id} preset={self.is_preset}>"


class BrokerProfileAudit(Base):
    """Append-only audit trail for every broker-profile change (v2 §0 #5).

    One row per field change: who, when, old → new. Never updated, never
    deleted — the tax-audit trail the contract-note validator stamps into.
    """

    __tablename__ = "broker_profile_audit"

    @classmethod
    def ensure_schema(cls, manager: Any) -> None:
        """Create just this table (same convention as the snapshot asset)."""
        engine = manager.engine if hasattr(manager, "engine") else manager
        Base.metadata.create_all(engine, tables=[cls.__table__])

    audit_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String(50), nullable=False)
    field_changed: Mapped[str] = mapped_column(String(100), nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    changed_by: Mapped[Optional[str]] = mapped_column(String(50), server_default="admin")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_broker_audit_profile", "profile_id", "changed_at"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BrokerProfileAudit {self.profile_id}.{self.field_changed}>"


# ---------------------------------------------------------------------------
# 10. Cost & Risk Settings — segments + global live kill-switch (Phase 2, v2 §0)
# ---------------------------------------------------------------------------


class SegmentRow(Base):
    """A trading segment — capital + mandate + broker + mode + risk limits.

    Certified v2 §0 #2: risk limits belong to SEGMENTS, not brokers. One row
    per named segment (``equity_intraday``, ``options_selling``, …) with:

    * ``allocated_capital`` — the segment's ring-fenced capital;
    * ``broker_profile_id`` — the execution venue (FK-shaped, not enforced);
    * ``mode`` — ``paper`` | ``live`` (per-segment half of the two-tier gate);
    * ``risk_limits`` — JSON: ``daily_loss_limit`` (abs ₹, MANDATORY before a
      segment can arm live — fail closed), ``max_drawdown_pct``,
      ``max_positions``, ``max_gross_exposure_pct``, ``max_leverage``.

    A missing/empty segment table means "panel not configured" — the live
    arming gates treat that as the legacy behavior (env-gated), not as a
    block, so a deployment without the panel keeps working.
    """

    __tablename__ = "segments"

    @classmethod
    def ensure_schema(cls, manager: Any) -> None:
        """Create just this table (same convention as the snapshot asset)."""
        engine = manager.engine if hasattr(manager, "engine") else manager
        Base.metadata.create_all(engine, tables=[cls.__table__])

    segment_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    segment_name: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default="paper")
    allocated_capital: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    broker_profile_id: Mapped[Optional[str]] = mapped_column(String(50))
    risk_limits: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    #: The risk-limit vocabulary the panel edits (v2 §0 #2).
    RISK_LIMIT_FIELDS: tuple = (
        "daily_loss_limit",
        "weekly_loss_limit",
        "max_drawdown_pct",
        "max_positions",
        "max_gross_exposure_pct",
        "max_leverage",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "segment_name": self.segment_name,
            "mode": self.mode,
            "allocated_capital": (
                float(self.allocated_capital) if self.allocated_capital is not None else None
            ),
            "broker_profile_id": self.broker_profile_id,
            "risk_limits": dict(self.risk_limits or {}),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SegmentRow {self.segment_id} mode={self.mode}>"


class SegmentAudit(Base):
    """Append-only audit trail for segment + kill-switch changes (v2 §0 #5)."""

    __tablename__ = "segment_audit"

    @classmethod
    def ensure_schema(cls, manager: Any) -> None:
        """Create just this table (same convention as the snapshot asset)."""
        engine = manager.engine if hasattr(manager, "engine") else manager
        Base.metadata.create_all(engine, tables=[cls.__table__])

    audit_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    segment_id: Mapped[str] = mapped_column(String(50), nullable=False)
    field_changed: Mapped[str] = mapped_column(String(100), nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    changed_by: Mapped[Optional[str]] = mapped_column(String(50), server_default="admin")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_segment_audit_segment", "segment_id", "changed_at"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SegmentAudit {self.segment_id}.{self.field_changed}>"


# ---------------------------------------------------------------------------
# 12. Strategy Metadata — descriptions, regime info, documentation (v2 strategy template)
# ---------------------------------------------------------------------------


class StrategyMetadata(Base):
    """Extended metadata for strategies — descriptions, docs, regime fit.

    This table stores rich strategy information beyond the class attributes.
    The ``strategy_name`` is the canonical name from ``Strategy.name`` and is
    unique per row. Descriptions are mandatory for new strategies; older
    strategies without descriptions will have backfilled data.

    The ``regime_vix_range`` column drives the Risk Board "strategy regime fit"
    display (low-volatility mean-reversion vs high-volatility momentum, etc.).
    ``eligible_instruments`` restricts which underlyings a strategy can trade.
    """

    __tablename__ = "strategy_metadata"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    #: Canonical strategy name (matches ``Strategy.name``, unique).
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: Rich description of what the strategy does, its logic, and when to use it.
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: Longer-form documentation / README content (Markdown allowed).
    documentation: Mapped[Optional[str]] = mapped_column(Text)
    #: Author / source of the strategy.
    author: Mapped[Optional[str]] = mapped_column(String(100))
    #: Version string (semver-like: "1.0", "2.1.3").
    version: Mapped[Optional[str]] = mapped_column(String(20))
    #: Signal kind: "equity" or "option" (derived from generate_market_view override).
    signal_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="equity")
    #: VIX range this strategy is designed for (e.g. (10, 20)).
    regime_vix_low: Mapped[Optional[float]] = mapped_column(Numeric(8, 2))
    regime_vix_high: Mapped[Optional[float]] = mapped_column(Numeric(8, 2))
    #: Comma-separated list of eligible instruments (NIFTY,BANKNIFTY).
    eligible_instruments: Mapped[Optional[str]] = mapped_column(String(255))
    #: Tags for filtering/searching (momentum, mean-reversion, breakout, etc.).
    tags: Mapped[Optional[str]] = mapped_column(String(255))
    #: When this metadata was created/updated.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("strategy_name", name="uq_strategy_metadata_name"),
        CheckConstraint(
            "signal_kind IN ('equity', 'option')", name="ck_strategy_metadata_signal_kind"
        ),
        CheckConstraint(
            "regime_vix_low IS NULL OR regime_vix_high IS NULL "
            "OR regime_vix_low <= regime_vix_high",
            name="ck_strategy_metadata_vix_range",
        ),
        Index("idx_strategy_metadata_name", "strategy_name"),
        Index("idx_strategy_metadata_signal_kind", "signal_kind"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StrategyMetadata {self.strategy_name} v{self.version}>"


# ---------------------------------------------------------------------------
# 13. Market Holidays — NSE holiday calendar for market status awareness
# ---------------------------------------------------------------------------


class MarketHoliday(Base):
    """Trading holiday for a market segment (non-weekend day when exchange is closed)."""

    __tablename__ = "market_holidays"

    @classmethod
    def ensure_schema(cls, manager: Any, seed: bool = True) -> None:
        """Create just this table and seed 2026 holidays if empty."""
        if hasattr(manager, "engine"):
            bind = manager.engine
        elif hasattr(manager, "get_bind"):
            bind = manager.get_bind()
        else:
            bind = manager
        Base.metadata.create_all(bind, tables=[cls.__table__])
        if seed:
            cls.seed_2026(manager)

    @classmethod
    def seed_2026(cls, manager: Any) -> int:
        """Seed 2026 NSE equity trading holidays if not already present."""
        from backtest.live.market_status import NSE_HOLIDAYS_2026

        def _do_seed(session: Any) -> int:
            cnt = 0
            for h_date, segment, desc in NSE_HOLIDAYS_2026:
                parsed_date = date.fromisoformat(h_date) if isinstance(h_date, str) else h_date
                existing = session.get(cls, parsed_date)
                if existing is None:
                    session.add(
                        cls(
                            holiday_date=parsed_date,
                            segment=segment,
                            description=desc,
                            is_trading_holiday=True,
                            source="nse",
                        )
                    )
                    cnt += 1
            if cnt:
                session.commit()
            return cnt

        try:
            if hasattr(manager, "session"):
                with manager.session() as session:
                    return _do_seed(session)
            elif hasattr(manager, "add") and hasattr(manager, "commit"):
                return _do_seed(manager)
            elif hasattr(manager, "connect"):
                from sqlalchemy.orm import Session
                with Session(manager) as session:
                    return _do_seed(session)
            return 0
        except Exception:
            return 0

    holiday_date: Mapped[date] = mapped_column(Date, primary_key=True)
    segment: Mapped[str] = mapped_column(
        String(32), nullable=False, default="equity", server_default="equity"
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    is_trading_holiday: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="nse", server_default="nse"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_market_holidays_date", "holiday_date"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MarketHoliday {self.holiday_date} {self.description}>"


# ---------------------------------------------------------------------------
# Backtest Run Ledger (docs/BACKTEST-RUN-PERSISTENCE-PRD.md, migration 018)
# ---------------------------------------------------------------------------


class BacktestRunKind(StrEnum):
    SINGLE = "single"
    COMPARE_SLOT = "compare_slot"
    OPTIMIZER_BASELINE = "optimizer_baseline"


class LedgerSeriesStatus(StrEnum):
    PRESENT = "present"
    EVICTED = "evicted"
    WRITE_FAILED = "write_failed"


class BacktestRun(Base):
    """One completed, successful backtest — immutable ledger row (PRD R1).

    Append-only: the same config re-run on re-fetched data is a new fact, not
    an update. Flat metric columns are a *projection* of ``metrics`` written
    through the optimizer's sanitizer; ``metrics`` stays the source of truth
    for full re-render.
    """

    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    #: sha256 of the canonical (sort_keys, allow_nan=False) config JSON.
    #: Display dedupe only — never a uniqueness constraint.
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'single'")
    )
    #: Set for compare slots. SET NULL: deleting a comparison must never
    #: delete the ledger facts about its individual runs.
    parent_compare_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr,
        ForeignKey(
            "backtest_compare_runs.compare_id",
            ondelete="SET NULL",
            name="fk_bt_runs_compare",
        ),
    )
    #: Set for optimizer baseline rows (PRD R6) only.
    optimization_run_id: Mapped[Optional[str]] = mapped_column(
        UUIDStr,
        ForeignKey(
            "optimization_runs.run_id", ondelete="SET NULL", name="fk_bt_runs_opt_run"
        ),
    )

    # -- economic determinants (also inside config_hash, flat for filtering) --
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    capital: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: backtest_driver | quick_screen — cross-engine aggregation is invalid.
    engine: Mapped[str] = mapped_column(String(30), nullable=False)

    #: Schema version of the stored payload. Bumped in BacktestAdapter (single
    #: canonical source); read endpoint refuses mismatched majors with 422.
    payload_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    params: Mapped[Any] = mapped_column(JSONVariant, nullable=False)
    #: payload["config"] verbatim (strategy/symbol/capital/stop_loss/
    #: take_profit/bars/strategy_params + timeframe/from/to/engine). The flat
    #: columns are its projection for filtering; read-back uses this so the
    #: reconstructed payload is shape-identical to the live response.
    config: Mapped[Any] = mapped_column(JSONVariant, nullable=False)
    readiness: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    cost_shock: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    metrics: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    # -- flat metric projection (same sanitizer as optimization results) ------
    sharpe: Mapped[Optional[Decimal]] = mapped_column(Score)
    sortino: Mapped[Optional[Decimal]] = mapped_column(Score)
    calmar: Mapped[Optional[Decimal]] = mapped_column(Score)
    total_return: Mapped[Optional[Decimal]] = mapped_column(Score)
    cagr: Mapped[Optional[Decimal]] = mapped_column(Score)
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Score)
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Score)
    #: Percentage: 54.20 = 54.2 % of closed trades were winners.
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    total_trades: Mapped[Optional[int]] = mapped_column(Integer)

    #: Lifecycle of the heavy series row (R2). write_failed until the series
    #: transaction commits; retention sets evicted. UI never shows a missing
    #: series as ``present`` because the status flips in the same txn as the
    #: DELETE (PRD R7 two-step sweep).
    series_status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'write_failed'")
    )

    # -- data attestation (flat columns + JSON written by one helper) --------
    data_source: Mapped[Optional[str]] = mapped_column(String(30))
    bars_count: Mapped[Optional[int]] = mapped_column(Integer)
    fetched_first_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fetched_last_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    data_fetch_date: Mapped[Optional[date]] = mapped_column(Date)
    provenance: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    #: Code-side attestation: {app_git_sha, strategy_sha256}. Non-null
    #: asserted at write — the ledger must tell apart same-name, different-code.
    code_fingerprint: Mapped[Any] = mapped_column(JSONVariant, nullable=False)

    created_by: Mapped[Optional[str]] = mapped_column(
        String(100), comment="Becomes NOT NULL when web auth ships."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in_check("kind", BacktestRunKind), name="ck_bt_runs_kind"),
        CheckConstraint(
            _in_check("series_status", LedgerSeriesStatus), name="ck_bt_runs_series_status"
        ),
        CheckConstraint(
            "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)",
            name="ck_bt_runs_win_rate",
        ),
        Index(
            "ix_backtest_runs_list",
            "strategy_id",
            "symbol",
            "timeframe",
            text("created_at DESC"),
        ),
        Index("ix_backtest_runs_config", "config_hash", text("created_at DESC")),
        Index("ix_backtest_runs_kind", "kind", text("created_at DESC")),
        Index(
            "ix_backtest_runs_opt",
            "optimization_run_id",
            postgresql_where=text("optimization_run_id IS NOT NULL"),
            sqlite_where=text("optimization_run_id IS NOT NULL"),
        ),
        Index(
            "ix_backtest_runs_compare",
            "parent_compare_id",
            postgresql_where=text("parent_compare_id IS NOT NULL"),
            sqlite_where=text("parent_compare_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BacktestRun {self.run_id[:8]} {self.strategy_id}:{self.symbol} {self.kind}>"


class BacktestCompareRun(Base):
    """One run-many comparison: shared attestation + stored comparison block (R1b)."""

    __tablename__ = "backtest_compare_runs"

    compare_id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=_uuid4_str)
    #: The literal values run_many stamps today (L732-734).
    comparison_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'strategies'")
    )
    #: Schema version of comparison_block; mismatched major → 422 on read-back.
    comparison_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    config_snapshot: Mapped[Any] = mapped_column(JSONVariant, nullable=False)

    data_source: Mapped[Optional[str]] = mapped_column(String(30))
    date_from: Mapped[Optional[date]] = mapped_column(Date)
    date_to: Mapped[Optional[date]] = mapped_column(Date)
    symbols_used: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    engines_used: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    provenance: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    #: correlation matrix + Sharpe significance, stored verbatim so review
    #: reads the same numbers the page showed at the time.
    comparison_block: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    slot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: {slot_id: {"error": msg}} — honest about partial comparisons.
    slot_errors: Mapped[Optional[Any]] = mapped_column(JSONVariant)

    created_by: Mapped[Optional[str]] = mapped_column(
        String(100), comment="Becomes NOT NULL when web auth ships."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "comparison_mode IN ('strategies','generalization')",
            name="ck_bt_compares_mode",
        ),
        CheckConstraint("slot_count >= 0", name="ck_bt_compares_slots_nonneg"),
        Index("ix_backtest_compares_created", text("created_at DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<BacktestCompareRun {self.compare_id[:8]} "
            f"{self.comparison_mode} x{self.slot_count}>"
        )


class BacktestRunSeries(Base):
    """Heavy per-run payloads (trades/equity/drawdown/signals), retention-capped (R2).

    Written in a transaction SEPARATE from the parent ledger row; the parent's
    ``series_status`` flips to ``present`` only when this row commits, so the
    ledger never claims a series it does not have.
    """

    __tablename__ = "backtest_run_series"

    run_id: Mapped[str] = mapped_column(
        UUIDStr,
        ForeignKey(
            "backtest_runs.run_id", ondelete="CASCADE", name="fk_bt_series_run"
        ),
        primary_key=True,
    )
    trades: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    equity: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    drawdown: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    signals: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    #: Non-curve payload parts needed for byte-identical read-back:
    #: {"benchmark": …, "monte_carlo": …}. Small; ride with the series.
    extras: Mapped[Optional[Any]] = mapped_column(JSONVariant)
    #: Serialized size in bytes — feeds retention sizing (R7/R8).
    bytes_written: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=False, server_default=text("0")
    )
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BacktestRunSeries {self.run_id[:8]} {self.bytes_written}B>"


#: Tables owned by the backtest run ledger (scoped ``create_all`` helper).
BACKTEST_LEDGER_TABLES = (
    BacktestCompareRun.__table__,
    BacktestRun.__table__,
    BacktestRunSeries.__table__,
)
