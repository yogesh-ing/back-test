"""B3 — the paper order ledger becomes durable at the transition itself.

QA 2026-10-30 measured ``select count(*) from orders`` = **0 rows** while the
Orders tab rendered real fills, and every ``trades`` row had NULL
``entry_order_id`` / ``exit_order_id`` / ``position_id``: executions existed
only in :class:`OrderLedger` memory, so the ledger money is reconciled against
vanished on every restart.

These tests drive a **real** ``OrderLedger`` against in-memory SQLite, because
the bug was precisely that the memory side and the durable side disagreed about
what had happened.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backtest.forward.paper_runner import (
    OrderLedger,
    OrderRequest,
    SIDE_BUY,
    SIDE_SELL,
)
from backtest.forward.trade_persistence import (
    LiveTradePersister,
    ledger_order_values,
)
from backtest.timekeeping import from_stored_utc

INSTANCE = "1a2b3c4d-0000-4000-8000-000000000001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    from backtest.db import DatabaseManager
    from backtest.db.config import DatabaseConfig
    from backtest.db.models import Base

    manager = DatabaseManager(DatabaseConfig(url="sqlite://", echo=False))
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


class _Config:
    def __init__(self, source="mstock"):
        self.name = "strangle book"
        self.strategy_name = "strangle_v1"
        self.allocated_capital = 100000
        self.source = source


class _Portfolio:
    current_cash = 95000.0


class _Runner:
    def __init__(self, source="mstock"):
        self.instance_id = INSTANCE
        self.config = _Config(source)
        self.portfolio = _Portfolio()


@pytest.fixture()
def ledger_persister(db):
    """A real ledger wired to a real persister, exactly as the manager wires it."""
    ledger = OrderLedger()
    persister = LiveTradePersister(db)
    persister.attach_order_ledger(ledger)
    runner = _Runner()
    ledger.register_runner(INSTANCE, runner)
    return ledger, persister, db, runner


def _leg(ledger, symbol="BANKNIFTY26NOV54700PE", side=SIDE_BUY, qty=490.0, price=765.0, **kw):
    """One option-style leg fill: synchronous, never a routed working order."""
    return ledger.record_filled(
        INSTANCE,
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        requested_price=kw.pop("requested_price", price),
        commission=kw.pop("commission", 20.0),
        tag=kw.pop("tag", {"kind": "option", "phase": "entry"}),
        **kw,
    )


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


class TestLegsLandInTheDurableLedger:
    def test_a_recorded_leg_writes_one_order_row(self, ledger_persister):
        ledger, _, db, _ = ledger_persister
        order = _leg(ledger)

        rows = db.fetch_all(
            "SELECT symbol, side, status, quantity, average_fill_price, client_order_id,"
            " portfolio_id, broker FROM orders"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "BANKNIFTY26NOV54700PE"
        # The DB enforces lowercase enums through CHECKs; a mirrored row that
        # said 'BUY' would never have inserted at all.
        assert row["side"] == "buy"
        assert row["status"] == "filled"
        assert float(row["quantity"]) == 490.0
        assert float(row["average_fill_price"]) == 765.0
        assert row["client_order_id"] == order.client_order_id
        assert row["broker"] == "paper"

    def test_a_filled_leg_writes_its_fill_with_the_book_s_brokerage(self, ledger_persister):
        ledger, _, db, _ = ledger_persister
        _leg(ledger, commission=24.5)

        rows = db.fetch_all("SELECT * FROM fills")
        assert len(rows) == 1
        assert float(rows[0]["fill_price"]) == 765.0
        assert float(rows[0]["commission"]) == pytest.approx(24.5)
        # The leg record carries brokerage only; the statutory stack is booked
        # per structure, so it is zero here rather than guessed at.
        assert float(rows[0]["regulatory_fees"]) == 0.0

    def test_the_order_row_is_the_runner_s_own_portfolio(self, ledger_persister):
        """B3's point: history can be joined back to the execution."""
        ledger, persister, db, runner = ledger_persister
        _leg(ledger)
        portfolio_id = persister._ensure_portfolio_row(runner)

        row = db.fetch_one(
            "SELECT p.name, o.portfolio_id FROM orders o"
            " JOIN portfolios p ON p.portfolio_id = o.portfolio_id"
        )
        assert row["portfolio_id"] == portfolio_id
        assert row["name"] == f"strangle book [{INSTANCE[:8]}]"

    def test_both_legs_of_a_structure_are_separate_rows(self, ledger_persister):
        ledger, _, db, _ = ledger_persister
        _leg(ledger, symbol="NIFTY26NOV22200PE", side=SIDE_BUY)
        _leg(ledger, symbol="NIFTY26NOV22800CE", side=SIDE_SELL)

        rows = db.fetch_all("SELECT symbol, side FROM orders ORDER BY symbol")
        assert [(r["symbol"], r["side"]) for r in rows] == [
            ("NIFTY26NOV22200PE", "buy"),
            ("NIFTY26NOV22800CE", "sell"),
        ]

    def test_timestamps_are_stored_on_one_clock(self, ledger_persister):
        ledger, _, db, _ = ledger_persister
        stamp = datetime(2026, 10, 30, 4, 45, tzinfo=timezone.utc).isoformat()
        _leg(ledger, ts=stamp)

        stored = db.fetch_one("SELECT submitted_at, filled_at FROM orders")
        assert from_stored_utc(stored["submitted_at"]) == datetime.fromisoformat(stamp)
        assert from_stored_utc(stored["filled_at"]) == datetime.fromisoformat(stamp)


# ---------------------------------------------------------------------------
# Refusals and cancels
# ---------------------------------------------------------------------------


class TestTerminalNonFillStates:
    def test_a_rejected_order_lands_as_rejected_with_its_reason(self, ledger_persister):
        ledger, _, db, _ = ledger_persister
        order = ledger.submit(INSTANCE, OrderRequest(symbol="INFY", side=SIDE_BUY, quantity=10))
        assert ledger.reject(order.client_order_id, "paper fill did not execute: REJECTED")

        row = db.fetch_one("SELECT status, rejection_reason, filled_at FROM orders")
        assert row["status"] == "rejected"
        # ck_orders_rejection_reason: a rejected row without a reason is refused
        # by the schema, which would silently drop the failure being audited.
        assert "did not execute" in row["rejection_reason"]
        assert row["filled_at"] is None

    def test_a_cancelled_order_lands_as_cancelled(self, ledger_persister):
        ledger, _, db, _ = ledger_persister
        order = ledger.submit(INSTANCE, OrderRequest(symbol="INFY", side=SIDE_SELL, quantity=10))
        assert ledger.cancel(order.client_order_id)

        row = db.fetch_one("SELECT status, cancelled_at FROM orders")
        assert row["status"] == "cancelled"
        assert row["cancelled_at"] is not None

    def test_a_submit_alone_is_not_mirrored(self, ledger_persister):
        """Paper orders terminate inside one call; a PENDING mirror row would be
        a row that is only ever seen mid-transaction, and after a restart it
        would read as a working order that no longer exists."""
        ledger, _, db, _ = ledger_persister
        ledger.submit(INSTANCE, OrderRequest(symbol="INFY", side=SIDE_BUY, quantity=10))
        assert db.fetch_one("SELECT count(*) AS n FROM orders")["n"] == 0

    def test_an_equity_fill_is_owned_by_the_sim_book_not_the_mirror(
        self, ledger_persister
    ):
        """The routed equity order also lives in ``runner.portfolio`` under the
        same client order id. Mirroring both writers would fight over
        ``uq_orders_client_order_id``."""
        ledger, _, db, _ = ledger_persister
        order = ledger.submit(INSTANCE, OrderRequest(symbol="INFY", side=SIDE_BUY, quantity=10))
        ledger.apply_fill(order.client_order_id, 1550.0, 10.0, commission=20.0)
        assert db.fetch_one("SELECT count(*) AS n FROM orders")["n"] == 0


# ---------------------------------------------------------------------------
# Idempotency and the fail-soft contract
# ---------------------------------------------------------------------------


class TestMirrorIsIdempotentAndHarmless:
    def test_replaying_the_same_event_writes_one_row_of_each(self, ledger_persister):
        ledger, persister, db, _ = ledger_persister
        order = _leg(ledger)
        first = db.fetch_one("SELECT order_id FROM orders")

        persister.mirror_order(order)  # e.g. a re-delivered event
        assert db.fetch_one("SELECT count(*) AS n FROM orders")["n"] == 1
        assert db.fetch_one("SELECT order_id FROM orders")["order_id"] == first["order_id"]
        # Append-only: the fill booked first is the record, never rewritten.
        assert db.fetch_one("SELECT count(*) AS n FROM fills")["n"] == 1

    def test_a_second_persister_updates_the_row_it_did_not_write(self, ledger_persister):
        """The primary key is a function of the client order id, so a writer
        that meets an execution it never booked still lands on the same row —
        one execution, one row, however many processes touch it."""
        ledger, _, db, _ = ledger_persister
        order = _leg(ledger)
        written = db.fetch_one("SELECT order_id, portfolio_id FROM orders")

        other = LiveTradePersister(db)  # e.g. the same book in the next process
        other._ledger = ledger
        other.mirror_order(order)

        assert db.fetch_one("SELECT count(*) AS n FROM orders")["n"] == 1
        again = db.fetch_one("SELECT order_id, portfolio_id FROM orders")
        assert again["order_id"] == written["order_id"]
        # Same runner name → the same portfolio row, not a second book.
        assert again["portfolio_id"] == written["portfolio_id"]

    def test_a_failing_mirror_never_costs_the_fill(self, ledger_persister):
        ledger, persister, db, _ = ledger_persister

        def explode(*args, **kwargs):
            raise RuntimeError("postgres is down")

        persister._write_ledger_order = explode
        order = _leg(ledger)  # must not raise

        assert order.status == "FILLED"
        assert ledger.get_order(order.client_order_id) is order
        assert db.fetch_one("SELECT count(*) AS n FROM orders")["n"] == 0

    def test_a_synthetic_book_writes_nothing(self, db):
        ledger = OrderLedger()
        persister = LiveTradePersister(db)
        persister.attach_order_ledger(ledger)
        runner = _Runner(source="synthetic")
        ledger.register_runner(INSTANCE, runner)

        ledger.record_filled(
            INSTANCE, symbol="NIFTY26NOV22200PE", side=SIDE_BUY, quantity=30.0, price=103.1
        )
        assert db.fetch_one("SELECT count(*) AS n FROM orders")["n"] == 0
        assert db.fetch_one("SELECT count(*) AS n FROM portfolios")["n"] == 0

    def test_an_unregistered_instance_is_skipped(self, db):
        """No runner → no portfolio row → nothing to own the order."""
        ledger = OrderLedger()
        persister = LiveTradePersister(db)
        persister.attach_order_ledger(ledger)
        ledger.record_filled(
            "ghost-instance-id", symbol="INFY", side=SIDE_BUY, quantity=10.0, price=1550.0
        )
        assert db.fetch_one("SELECT count(*) AS n FROM orders")["n"] == 0

    def test_a_persister_without_a_db_leaves_the_ledger_alone(self):
        ledger = OrderLedger()
        LiveTradePersister(None).attach_order_ledger(ledger)
        assert ledger._persistence_sink is None


# ---------------------------------------------------------------------------
# Vocabulary translation (the pure half)
# ---------------------------------------------------------------------------


class TestLedgerOrderValues:
    """The vocabulary translation, away from the database that enforces it."""

    def _order(self, **request):
        ledger = OrderLedger()
        return ledger.submit(INSTANCE, OrderRequest(symbol="INFY", side=SIDE_BUY, quantity=10,
                                                     **request))

    def _values(self, order):
        return ledger_order_values(order, "portfolio-id")

    def test_a_short_fill_is_partial_not_filled(self):
        """``ck_orders_filled_consistency`` demands filled_quantity = quantity,
        which is exactly the truth the ledger's single FILLED label hides when
        a venue fills part of an order."""
        ledger = OrderLedger()
        order = ledger.submit(
            INSTANCE, OrderRequest(symbol="INFY", side=SIDE_BUY, quantity=10)
        )
        ledger.apply_fill(order.client_order_id, 1550.0, 4.0)
        values = self._values(order)
        assert values["status"] == "partial"
        assert float(values["filled_quantity"]) == 4.0

    def test_a_zero_fill_is_pending(self):
        order = self._order()
        order.status = "FILLED"
        assert self._values(order)["status"] == "pending"

    def test_a_limit_without_a_price_is_written_as_market(self):
        """The schema refuses a limit row with no limit_price; keeping the row
        and its side/quantity beats dropping the execution."""
        order = self._order(order_type="LIMIT")
        values = self._values(order)
        assert values["order_type"] == "market"
        assert values["limit_price"] is None

    def test_a_limit_that_carried_its_price_keeps_the_type(self):
        order = self._order(order_type="LIMIT", limit_price=1551.5)
        values = self._values(order)
        assert values["order_type"] == "limit"
        assert float(values["limit_price"]) == 1551.5

    def test_an_unknown_status_is_still_working(self):
        order = self._order()
        order.status = "SOMETHING_NEW"
        assert self._values(order)["status"] == "pending"
