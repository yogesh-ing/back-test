"""Ticket 2.4 item 2 — logging a signal must not fabricate capital (BUG-37).

``strategy_signals.portfolio_id`` is NOT NULL, so a runner whose book had no
``portfolios`` row yet used to get one created as a side effect of logging a
signal, booked at the book's full initial capital with ``status='active'``.
Those rows are what made ``sum(initial_capital)`` in the live ledger drift away
from the runners that actually exist. Signal logging now creates a placeholder
that owns nothing, or drops the signal — never capital.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.db.models import Portfolio as PortfolioRow
from backtest.db.models import StrategySignal
from backtest.forward.strategy_adapter import (
    SHADOW_FK_CAPITAL,
    SHADOW_FK_PREFIX,
    SHADOW_FK_STATUS,
    Signal,
    StrategyAdapter,
)
from backtest.simulator.portfolio import Portfolio
from backtest.strategy.base import Strategy


class AlwaysLong(Strategy):
    # Empty class name keeps __init_subclass__ from polluting the global registry.
    name = ""
    params = {}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "always_long"

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        return pd.Series(1, index=candles.index)


@pytest.fixture
def db():
    manager = DatabaseManager(
        DatabaseConfig(url="sqlite:///:memory:", pool_min_size=1, pool_max_size=5)
    )
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


def adapter_for(db, book: Portfolio) -> StrategyAdapter:
    return StrategyAdapter(
        strategy=AlwaysLong(), portfolio=book, symbols=["INFY"], db_manager=db, min_bars=1
    )


def active_capital(db) -> Decimal:
    with db.session() as session:
        rows = session.query(PortfolioRow).filter(PortfolioRow.status == "active").all()
        return sum((r.initial_capital for r in rows), Decimal("0"))


def test_placeholder_books_no_capital(db):
    book = Portfolio(name="ghost-book", initial_capital=100000)
    signal_id = adapter_for(db, book)._save_signal_to_db(Signal(symbol="INFY", action="BUY"))
    assert signal_id

    with db.session() as session:
        row = session.get(PortfolioRow, book.portfolio_id)
        assert row is not None
        assert row.status == SHADOW_FK_STATUS
        assert row.initial_capital == SHADOW_FK_CAPITAL
        assert row.name == f"{SHADOW_FK_PREFIX}{book.portfolio_id}"
    # The ledger BUG-37 reads: nothing may show up as active capital.
    assert active_capital(db) == Decimal("0")


def test_signal_stays_attributed_to_its_own_book(db):
    book = Portfolio(name="keep-attribution", initial_capital=100000)
    adapter_for(db, book)._save_signal_to_db(Signal(symbol="INFY", action="BUY"))

    with db.session() as session:
        signals = session.query(StrategySignal).all()
        assert len(signals) == 1
        assert signals[0].portfolio_id == book.portfolio_id


def test_registered_book_is_never_rewritten(db):
    book = Portfolio(name="real-book", initial_capital=100000)
    with db.session() as session:
        session.add(
            PortfolioRow(
                portfolio_id=book.portfolio_id,
                name="real-book [abcd1234]",
                initial_capital=Decimal("500000.00"),
                current_cash=Decimal("500000.00"),
                status="active",
            )
        )

    adapter_for(db, book)._save_signal_to_db(Signal(symbol="INFY", action="BUY"))

    with db.session() as session:
        rows = session.query(PortfolioRow).all()
        assert len(rows) == 1
        assert rows[0].status == "active"
        assert rows[0].initial_capital == Decimal("500000.00")
    assert active_capital(db) == Decimal("500000.00")


def test_many_signals_across_many_books_do_not_move_the_ledger(db):
    for index in range(5):
        book = Portfolio(name=f"fixture-{index}", initial_capital=100000)
        adapter = adapter_for(db, book)
        for _ in range(3):
            adapter._save_signal_to_db(Signal(symbol="INFY", action="BUY"))

    with db.session() as session:
        assert session.query(StrategySignal).count() == 15
        placeholders = (
            session.query(PortfolioRow).filter(PortfolioRow.name.like(f"{SHADOW_FK_PREFIX}%")).all()
        )
        # Repeat signals for one book reuse its placeholder.
        assert len(placeholders) == 5
    assert active_capital(db) == Decimal("0")


def test_blocked_placeholder_drops_the_signal_without_raising(db):
    """The failure mode the old code hit: a UNIQUE name clash poisoned the session.

    An existing row already owns the placeholder name, so the insert cannot
    succeed. The signal must be dropped with a warning instead of raising, and
    nothing may be invented behind it.
    """
    book = Portfolio(name="clashing-book", initial_capital=100000)
    with db.session() as session:
        session.add(
            PortfolioRow(
                portfolio_id="11111111-1111-1111-1111-111111111111",
                name=f"{SHADOW_FK_PREFIX}{book.portfolio_id}",
                initial_capital=Decimal("0.01"),
                current_cash=Decimal("0.01"),
                status=SHADOW_FK_STATUS,
            )
        )

    assert adapter_for(db, book)._save_signal_to_db(Signal(symbol="INFY", action="BUY")) == ""

    with db.session() as session:
        assert session.query(StrategySignal).count() == 0
        assert session.get(PortfolioRow, book.portfolio_id) is None


def test_book_without_an_id_logs_nothing(db):
    book = Portfolio(name="idless", initial_capital=100000)
    book.portfolio_id = None

    assert adapter_for(db, book)._save_signal_to_db(Signal(symbol="INFY", action="BUY")) == ""
    with db.session() as session:
        assert session.query(PortfolioRow).count() == 0


def test_placeholder_status_is_one_the_schema_already_allows():
    """A new status needs a migration: ``ck_portfolios_status`` lists these three."""
    assert SHADOW_FK_STATUS in {"active", "paused", "stopped"}
    assert SHADOW_FK_STATUS != "active"
    assert SHADOW_FK_CAPITAL > Decimal("0")
