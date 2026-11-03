"""Ticket 2.4 item 3 — retire portfolio rows no runner owns (BUG-37).

``sum(initial_capital) WHERE status='active'`` is the capital ledger, and rows
survive their runners: deleted books, re-spawns that get a new instance id, test
fixtures. Measured 2026-10-30: 63 active rows against 14 live runners. The boot
reaper in :meth:`PortfolioManager._reap_orphan_portfolio_rows` claims rows
against the runners that booted and stops the rest.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.db.models import Portfolio as PortfolioRow
from backtest.db.models import Trade as TradeRow
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.portfolio_reaper import (
    RETIRED_STATUS,
    reap_orphan_portfolio_rows,
    runner_claim_keys,
)
from backtest.timekeeping import now_utc

INSTANCE_ID = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6"
BOOK_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def db():
    manager = DatabaseManager(
        DatabaseConfig(url="sqlite:///:memory:", pool_min_size=1, pool_max_size=5)
    )
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


def add_book(db, name, capital, status="active", portfolio_id=None):
    with db.session() as session:
        row = PortfolioRow(
            name=name,
            initial_capital=Decimal(capital),
            current_cash=Decimal(capital),
            status=status,
        )
        if portfolio_id:
            row.portfolio_id = portfolio_id
        session.add(row)
        session.flush()
        return row.portfolio_id


def status_of(db, portfolio_id):
    with db.session() as session:
        row = session.get(PortfolioRow, portfolio_id)
        return row.status if row else None


def active_ledger(db):
    with db.session() as session:
        total = session.scalar(
            select(func.sum(PortfolioRow.initial_capital)).where(PortfolioRow.status == "active")
        )
        return Decimal(str(total or 0))


def live_runner():
    """The claim shape :func:`runner_claim_keys` reads off a StrategyRunner."""
    return SimpleNamespace(
        instance_id=INSTANCE_ID,
        config=SimpleNamespace(name="NIFTY STRANGLE"),
        portfolio=SimpleNamespace(portfolio_id=BOOK_ID),
        _db_portfolio_id=None,
    )


def test_orphan_row_is_retired_and_its_history_kept(db):
    live = add_book(db, f"NIFTY STRANGLE [{INSTANCE_ID[:8]}]", "500000.00")
    ghost = add_book(db, "LIVE-STRANGLE-7", "100000.00")
    with db.session() as session:
        session.add(
            TradeRow(
                portfolio_id=ghost,
                symbol="BANKNIFTY",
                entry_price=Decimal("100.00"),
                exit_price=Decimal("90.00"),
                quantity=Decimal("10"),
                entry_time=now_utc(),
                exit_time=now_utc(),
                gross_pnl=Decimal("-100.00"),
                net_pnl=Decimal("-105.00"),
            )
        )

    with db.session() as session:
        report = reap_orphan_portfolio_rows(session, runner_claim_keys([live_runner()]))

    assert report.reaped == ["LIVE-STRANGLE-7"]
    assert report.kept == [f"NIFTY STRANGLE [{INSTANCE_ID[:8]}]"]
    assert status_of(db, ghost) == RETIRED_STATUS
    assert status_of(db, live) == "active"
    with db.session() as session:
        assert session.query(TradeRow).filter(TradeRow.portfolio_id == ghost).count() == 1


def test_active_capital_collapses_to_the_books_that_exist(db):
    add_book(db, f"NIFTY STRANGLE [{INSTANCE_ID[:8]}]", "500000.00")
    for index in range(13):
        add_book(db, f"LIVE-STRANGLE-{index}", "100000.00")
    assert active_ledger(db) == Decimal("1800000.00")

    with db.session() as session:
        reap_orphan_portfolio_rows(session, runner_claim_keys([live_runner()]))

    assert active_ledger(db) == Decimal("500000.00")


def test_reap_is_idempotent(db):
    add_book(db, "LIVE-STRANGLE-1", "100000.00")
    claims = runner_claim_keys([live_runner()])
    with db.session() as session:
        assert reap_orphan_portfolio_rows(session, claims).changed == 1
    with db.session() as session:
        second = reap_orphan_portfolio_rows(session, claims)
        assert second.changed == 0
        assert second.scanned == 0


def test_only_active_rows_are_reaped(db):
    paused = add_book(db, "someone-paused", "100000.00", status="paused")
    stopped = add_book(db, "someone-stopped", "100000.00", status="stopped")
    with db.session() as session:
        assert reap_orphan_portfolio_rows(session, set()).changed == 0
    assert status_of(db, paused) == "paused"
    assert status_of(db, stopped) == "stopped"


def test_claim_can_come_from_the_row_id_instead_of_the_name(db):
    """A runner's row may be keyed by id only — the persister caches that id."""
    row_id = add_book(db, "renamed-book", "100000.00")
    runner = SimpleNamespace(
        instance_id=INSTANCE_ID,
        config=SimpleNamespace(name="other-name"),
        portfolio=SimpleNamespace(portfolio_id=row_id),
        _db_portfolio_id=None,
    )
    with db.session() as session:
        report = reap_orphan_portfolio_rows(session, runner_claim_keys([runner]))
    assert report.changed == 0
    assert status_of(db, row_id) == "active"


def test_instance_prefix_suffix_counts_as_claimed(db):
    row_id = add_book(db, f"QA12_hf_ICICIBANK [{INSTANCE_ID[:8]}]", "100000.00")
    with db.session() as session:
        assert reap_orphan_portfolio_rows(session, {INSTANCE_ID[:8]}).changed == 0
    assert status_of(db, row_id) == "active"


def reap_at_boot(db_manager, runners):
    """Run the boot hook against a stand-in for the manager's own state."""
    persister = SimpleNamespace(db=db_manager) if db_manager is not None else None
    stub = SimpleNamespace(_trade_persister=persister, _runners=runners)
    PortfolioManager._reap_orphan_portfolio_rows(stub)
    return stub


def test_manager_reaps_through_the_persister_db(db):
    add_book(db, f"NIFTY STRANGLE [{INSTANCE_ID[:8]}]", "500000.00")
    add_book(db, "fixture-left-behind", "100000.00")

    reap_at_boot(db, {INSTANCE_ID: live_runner()})

    assert active_ledger(db) == Decimal("500000.00")


def test_empty_runner_list_cannot_retire_the_ledger(db):
    """The 2026-10-27 shape: a refused restore leaves no runners.

    Reaping then would retire every real book, so the guard makes the boot
    reaper a no-op instead.
    """
    live = add_book(db, f"NIFTY STRANGLE [{INSTANCE_ID[:8]}]", "500000.00")

    reap_at_boot(db, {})

    assert status_of(db, live) == "active"


def test_no_database_is_a_quiet_no_op():
    reap_at_boot(None, {INSTANCE_ID: live_runner()})
