"""Schema tests for the forward testing database layer (Step 1).

These run entirely on in-memory SQLite so they need no external services.
PostgreSQL-specific behaviour (partial indexes, JSONB, triggers) is exercised
manually via ``db/verify_schema.sql``; see ``db/DB-IMPLEMENTATION-GUIDE.md``.

What is covered here:

* every expected table/column exists on a fresh ``create_all()``
* the CHECK constraints actually reject bad data
* foreign keys cascade and set-null as designed
* the hand-written SQLite migration agrees with the ORM models
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backtest.db.models import (
    Base,
    EquityCurve,
    Fill,
    MarketDataCache,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Portfolio,
    PortfolioStatus,
    Position,
    PositionStatus,
    PositionType,
    StrategySignal,
    SystemLog,
    Trade,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SQLITE_MIGRATION = REPO_ROOT / "db" / "migrations" / "001_initial_schema.sqlite.sql"
PG_MIGRATION = REPO_ROOT / "db" / "migrations" / "001_initial_schema.sql"

EXPECTED_TABLES = {
    "portfolios",
    "positions",
    "orders",
    "fills",
    "trades",
    "equity_curve",
    "market_data_cache",
    "performance_metrics",
    "strategy_signals",
    "system_logs",
}

UTC_NOW = datetime.now(timezone.utc)


@pytest.fixture()
def engine():
    """In-memory SQLite engine with foreign key enforcement switched on."""
    eng = create_engine("sqlite://")

    # SQLite ignores foreign keys unless asked, on every connection.
    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture()
def portfolio(session):
    p = Portfolio(
        name="Test Portfolio",
        initial_capital=Decimal("100000"),
        current_cash=Decimal("100000"),
    )
    session.add(p)
    session.commit()
    return p


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_all_expected_tables_created(engine):
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


def test_no_money_column_is_a_float(engine):
    """Money must be NUMERIC/Decimal. Floats silently break reconciliation."""
    insp = inspect(engine)
    offenders = []
    for table in EXPECTED_TABLES:
        for col in insp.get_columns(table):
            name = col["name"]
            if any(k in name for k in ("price", "pnl", "cash", "equity", "capital", "commission")):
                if "FLOAT" in str(col["type"]).upper() or "REAL" in str(col["type"]).upper():
                    offenders.append(f"{table}.{name}={col['type']}")
    assert not offenders, f"float-typed money columns: {offenders}"


def test_foreign_keys_declared(engine):
    insp = inspect(engine)
    assert {fk["referred_table"] for fk in insp.get_foreign_keys("positions")} == {"portfolios"}
    assert {fk["referred_table"] for fk in insp.get_foreign_keys("fills")} == {"orders", "positions"}
    assert {fk["referred_table"] for fk in insp.get_foreign_keys("trades")} == {
        "portfolios",
        "positions",
        "orders",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_full_lifecycle_portfolio_to_trade(session, portfolio):
    """Portfolio -> signal -> order -> position -> fill -> trade -> equity."""
    signal = StrategySignal(
        portfolio_id=portfolio.portfolio_id,
        symbol="INFY",
        strategy_name="sma_crossover",
        signal_type="entry",
        direction="long",
        strength=Decimal("0.8"),
        target_position=Decimal("1.0"),
        bar_ts=UTC_NOW - timedelta(minutes=5),
        indicators_snapshot={"sma_fast": 1502.3, "sma_slow": 1488.1},
    )
    order = Order(
        portfolio_id=portfolio.portfolio_id,
        symbol="INFY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        status=OrderStatus.FILLED,
        filled_at=UTC_NOW,
        client_order_id="test-001",
    )
    position = Position(
        portfolio_id=portfolio.portfolio_id,
        symbol="INFY",
        position_type=PositionType.LONG,
        quantity=Decimal("10"),
        average_entry_price=Decimal("1500.50"),
        current_price=Decimal("1512.00"),
    )
    session.add_all([signal, order, position])
    session.commit()

    session.add(
        Fill(
            order_id=order.order_id,
            position_id=position.position_id,
            symbol="INFY",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            fill_price=Decimal("1500.50"),
            commission=Decimal("4.50"),
            reference_price=Decimal("1500.00"),
        )
    )
    session.add(
        Trade(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            entry_order_id=order.order_id,
            quantity=Decimal("10"),
            entry_price=Decimal("1500.50"),
            exit_price=Decimal("1512.00"),
            entry_time=UTC_NOW - timedelta(hours=1),
            exit_time=UTC_NOW,
            gross_pnl=Decimal("115.00"),
            net_pnl=Decimal("110.50"),
            exit_reason="signal",
        )
    )
    session.add(
        EquityCurve(
            portfolio_id=portfolio.portfolio_id,
            ts=UTC_NOW,
            total_equity=Decimal("100110.50"),
            cash=Decimal("84990.50"),
            position_value=Decimal("15120.00"),
        )
    )
    session.commit()

    assert session.query(Trade).count() == 1
    assert session.query(Fill).count() == 1
    assert order.fills[0].total_fees == Decimal("4.50")
    assert order.remaining_quantity == Decimal("0")


def test_jsonb_roundtrip(session, portfolio):
    payload = {"rsi": 63.2, "sma": [10, 20, 30], "nested": {"ok": True}}
    session.add(
        StrategySignal(
            portfolio_id=portfolio.portfolio_id,
            symbol="TCS",
            signal_type="entry",
            direction="long",
            indicators_snapshot=payload,
        )
    )
    session.commit()
    assert session.query(StrategySignal).one().indicators_snapshot == payload


def test_order_remaining_quantity_is_derived():
    order = Order(
        portfolio_id="x",
        symbol="INFY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        filled_quantity=Decimal("3"),
    )
    assert order.remaining_quantity == Decimal("7")


# ---------------------------------------------------------------------------
# Constraints — each of these MUST be rejected
# ---------------------------------------------------------------------------


def _reject(session, obj):
    session.add(obj)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_only_one_open_position_per_symbol(session, portfolio):
    session.add(
        Position(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            quantity=Decimal("10"),
            average_entry_price=Decimal("100"),
        )
    )
    session.commit()
    _reject(
        session,
        Position(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            quantity=Decimal("5"),
            average_entry_price=Decimal("101"),
        ),
    )


def test_closed_position_may_reuse_symbol(session, portfolio):
    """The partial index must still allow closed history for the same symbol."""
    old = Position(
        portfolio_id=portfolio.portfolio_id,
        symbol="INFY",
        quantity=Decimal("0"),
        average_entry_price=Decimal("100"),
        status=PositionStatus.CLOSED,
        closed_at=UTC_NOW,
    )
    session.add(old)
    session.commit()
    session.add(
        Position(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            quantity=Decimal("7"),
            average_entry_price=Decimal("102"),
        )
    )
    session.commit()
    assert session.query(Position).count() == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(dict(position_type="long", quantity=Decimal("-5")), id="long_negative_qty"),
        pytest.param(dict(position_type="short", quantity=Decimal("5")), id="short_positive_qty"),
        pytest.param(dict(status="closed"), id="closed_without_closed_at"),
        pytest.param(dict(position_type="sideways"), id="invalid_position_type"),
    ],
)
def test_position_constraints(session, portfolio, kwargs):
    base = dict(
        portfolio_id=portfolio.portfolio_id,
        symbol="BAD",
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
    )
    _reject(session, Position(**{**base, **kwargs}))


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(dict(order_type="limit"), id="limit_without_limit_price"),
        pytest.param(dict(order_type="stop"), id="stop_without_stop_price"),
        pytest.param(dict(order_type="trailing_stop"), id="trailing_without_amount"),
        pytest.param(dict(quantity=Decimal("0")), id="zero_quantity"),
        pytest.param(dict(filled_quantity=Decimal("99")), id="overfilled"),
        pytest.param(dict(status="rejected"), id="rejected_without_reason"),
        pytest.param(dict(status="filled", filled_at=UTC_NOW), id="filled_but_not_complete"),
        pytest.param(dict(side="hodl"), id="invalid_side"),
        pytest.param(dict(time_in_force="forever"), id="invalid_tif"),
    ],
)
def test_order_constraints(session, portfolio, kwargs):
    base = dict(
        portfolio_id=portfolio.portfolio_id,
        symbol="INFY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
    )
    _reject(session, Order(**{**base, **kwargs}))


def test_duplicate_client_order_id_rejected(session, portfolio):
    def mk():
        return Order(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("10"),
            client_order_id="dup-1",
        )

    session.add(mk())
    session.commit()
    _reject(session, mk())


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(dict(high=Decimal("90"), low=Decimal("95")), id="high_below_low"),
        pytest.param(dict(open=Decimal("200")), id="open_above_high"),
        pytest.param(dict(timeframe="7min"), id="invalid_timeframe"),
        pytest.param(dict(close=Decimal("-1")), id="negative_price"),
        pytest.param(dict(volume=Decimal("-5")), id="negative_volume"),
        pytest.param(dict(bid=Decimal("101"), ask=Decimal("100")), id="crossed_quote"),
    ],
)
def test_market_data_constraints(session, kwargs):
    base = dict(
        symbol="INFY",
        timeframe="5min",
        ts=UTC_NOW,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("95"),
        close=Decimal("100"),
    )
    _reject(session, MarketDataCache(**{**base, **kwargs}))


def test_duplicate_bar_rejected(session):
    def mk():
        return MarketDataCache(
            symbol="INFY",
            timeframe="5min",
            ts=UTC_NOW,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("95"),
            close=Decimal("100"),
        )

    session.add(mk())
    session.commit()
    _reject(session, mk())


def test_trade_exit_before_entry_rejected(session, portfolio):
    _reject(
        session,
        Trade(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            quantity=Decimal("10"),
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            entry_time=UTC_NOW,
            exit_time=UTC_NOW - timedelta(days=1),
            gross_pnl=Decimal("100"),
            net_pnl=Decimal("90"),
        ),
    )


def test_invalid_exit_reason_rejected(session, portfolio):
    _reject(
        session,
        Trade(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            quantity=Decimal("10"),
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            entry_time=UTC_NOW - timedelta(hours=1),
            exit_time=UTC_NOW,
            gross_pnl=Decimal("100"),
            net_pnl=Decimal("90"),
            exit_reason="because_i_felt_like_it",
        ),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(dict(strength=Decimal("1.5")), id="strength_above_1"),
        pytest.param(dict(target_position=Decimal("2")), id="target_outside_range"),
        pytest.param(dict(signal_type="maybe"), id="invalid_signal_type"),
        pytest.param(dict(direction="sideways"), id="invalid_direction"),
    ],
)
def test_signal_constraints(session, portfolio, kwargs):
    base = dict(
        portfolio_id=portfolio.portfolio_id,
        symbol="INFY",
        signal_type="entry",
        direction="long",
    )
    _reject(session, StrategySignal(**{**base, **kwargs}))


def test_invalid_log_level_rejected(session):
    _reject(session, SystemLog(log_level="trace", component="x", message="y"))


def test_duplicate_equity_timestamp_rejected(session, portfolio):
    def mk():
        return EquityCurve(
            portfolio_id=portfolio.portfolio_id,
            ts=UTC_NOW,
            total_equity=Decimal("1"),
            cash=Decimal("1"),
        )

    session.add(mk())
    session.commit()
    _reject(session, mk())


def test_portfolio_constraints(session):
    _reject(session, Portfolio(name="z", initial_capital=Decimal("0"), current_cash=Decimal("0")))
    session.add(
        Portfolio(name="dup", initial_capital=Decimal("1"), current_cash=Decimal("1"))
    )
    session.commit()
    _reject(
        session, Portfolio(name="dup", initial_capital=Decimal("1"), current_cash=Decimal("1"))
    )


def test_orphan_foreign_key_rejected(session):
    _reject(
        session,
        Position(
            portfolio_id="00000000-0000-0000-0000-000000000000",
            symbol="GHOST",
            quantity=Decimal("1"),
            average_entry_price=Decimal("1"),
        ),
    )


# ---------------------------------------------------------------------------
# Cascade behaviour
# ---------------------------------------------------------------------------


def test_deleting_portfolio_cascades_but_preserves_logs(engine):
    with Session(engine) as s:
        p = Portfolio(
            name="Doomed", initial_capital=Decimal("1000"), current_cash=Decimal("1000")
        )
        s.add(p)
        s.commit()
        pid = p.portfolio_id

        s.add(
            Position(
                portfolio_id=pid,
                symbol="INFY",
                quantity=Decimal("1"),
                average_entry_price=Decimal("1"),
            )
        )
        s.add(SystemLog(portfolio_id=pid, log_level="info", component="test", message="hi"))
        s.commit()

        s.execute(Base.metadata.tables["portfolios"].delete())
        s.commit()

        assert s.query(Position).count() == 0, "positions should cascade"
        remaining = s.query(SystemLog).all()
        assert len(remaining) == 1, "logs must survive the portfolio"
        assert remaining[0].portfolio_id is None, "log FK should be set to NULL"


# ---------------------------------------------------------------------------
# The hand-written SQL must agree with the ORM
# ---------------------------------------------------------------------------


def test_sqlite_migration_file_matches_orm(tmp_path):
    """Applying the .sql file by hand yields the same tables/columns as the ORM."""
    db_path = tmp_path / "hand.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SQLITE_MIGRATION.read_text())
    conn.close()

    hand = inspect(create_engine(f"sqlite:///{db_path}"))
    orm_engine = create_engine("sqlite://")
    Base.metadata.create_all(orm_engine)
    orm = inspect(orm_engine)

    for table in sorted(EXPECTED_TABLES):
        hand_cols = {c["name"] for c in hand.get_columns(table)}
        orm_cols = {c["name"] for c in orm.get_columns(table)}
        assert hand_cols == orm_cols, (
            f"{table}: sql-only={sorted(hand_cols - orm_cols)} "
            f"orm-only={sorted(orm_cols - hand_cols)}"
        )


def test_sqlite_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "twice.db"
    script = SQLITE_MIGRATION.read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(script)
    conn.executescript(script)  # must not raise
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert EXPECTED_TABLES <= tables


def test_migration_files_exist():
    assert PG_MIGRATION.exists()
    assert SQLITE_MIGRATION.exists()
    assert (REPO_ROOT / "db" / "migrations" / "001_initial_schema_rollback.sql").exists()
    assert (REPO_ROOT / "db" / "DB-IMPLEMENTATION-GUIDE.md").exists()


def test_enum_values_match_sql_check_constraints():
    """The Python enums and the SQL CHECK lists must not drift apart."""
    sql = PG_MIGRATION.read_text()
    assert "status IN ('active','paused','stopped')" in sql
    assert set(PortfolioStatus.values()) == {"active", "paused", "stopped"}
    assert "side IN ('buy','sell')" in sql
    assert set(OrderSide.values()) == {"buy", "sell"}
    for value in OrderType.values():
        assert f"'{value}'" in sql, f"OrderType.{value} missing from SQL CHECK"
    for value in OrderStatus.values():
        assert f"'{value}'" in sql, f"OrderStatus.{value} missing from SQL CHECK"
