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

from datetime import date, datetime, timedelta, timezone

import pytest

from backtest.forward.paper_runner import (
    OrderLedger,
    OrderRequest,
    RunnerConfig,
    SIDE_BUY,
    SIDE_SELL,
    StrategyRunner,
)
from backtest.forward.trade_persistence import (
    LiveTradePersister,
    ledger_order_values,
)
from backtest.timekeeping import from_stored_utc

INSTANCE = "1a2b3c4d-0000-4000-8000-000000000001"
BASE_DAY = date(2026, 10, 30)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _deterministic_environment(monkeypatch):
    """Pin fee resolution and plugins — these tests trade, so they pay costs."""
    from backtest.plugins import discover_plugins

    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    discover_plugins()


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


# ---------------------------------------------------------------------------
# The executed book: positions, orders, fills, and the trade that joins them
# ---------------------------------------------------------------------------


def _bars(closes, symbol="RELIANCE"):
    return [
        {
            "ts": f"{(BASE_DAY + timedelta(days=i)).isoformat()}T09:15:00",
            "open": close - 1,
            "high": close + 5,
            "low": close - 5,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _equity_runner(source="mstock") -> StrategyRunner:
    """A real runner that has opened one share position through its broker."""
    runner = StrategyRunner(
        RunnerConfig(
            name="EQ",
            strategy_name="buy_and_hold",
            allocated_capital=1_000_000,
            symbols=["RELIANCE"],
            timeframe="1min",
            mode="paper",
            source=source,
        ),
        ledger=OrderLedger(),
    )
    runner.start()
    for bar in _bars([100.0] * 14):
        runner.process_candle_event("RELIANCE", bar)
    assert runner.positions, "fixture failed to open a position"
    return runner


def _counts(db):
    return {
        table: db.fetch_one(f"SELECT count(*) AS n FROM {table}")["n"]
        for table in ("positions", "orders", "fills", "trades")
    }


class TestExecutedBookIsDurable:
    def test_an_open_position_lands_before_any_trade_closes(
        self, db
    ):
        """The entry leg of a round trip is an execution. Waiting for the exit
        to write it would leave the fills unjoinable for the whole life of the
        trade — and nothing at all if the process died holding the book."""
        runner = _equity_runner()
        LiveTradePersister(db).flush_runner(runner)

        counts = _counts(db)
        assert counts["positions"] == 1
        assert counts["orders"] == 1 and counts["fills"] == 1
        assert counts["trades"] == 0
        row = db.fetch_one("SELECT symbol, status, quantity FROM positions")
        assert (row["symbol"], row["status"]) == ("RELIANCE", "open")
        assert float(row["quantity"]) > 0

    def test_a_closed_round_trip_joins_its_trade_to_its_position(self, db):
        runner = _equity_runner()
        runner.last_price["RELIANCE"] = 112.0
        runner.close_position("RELIANCE", reason="manual_target")
        written = LiveTradePersister(db).flush_runner(runner)
        assert written == 1

        row = db.fetch_one(
            "SELECT t.trade_id, t.position_id, p.symbol, p.status, p.closed_at"
            " FROM trades t JOIN positions p ON p.position_id = t.position_id"
        )
        assert row is not None, "the trade names no position row"
        assert row["symbol"] == "RELIANCE"
        assert row["status"] == "closed"
        assert row["closed_at"] is not None

    def test_both_legs_are_rows_with_the_fees_the_book_charged(self, db):
        runner = _equity_runner()
        runner.last_price["RELIANCE"] = 112.0
        runner.close_position("RELIANCE", reason="manual_target")
        LiveTradePersister(db).flush_runner(runner)

        rows = db.fetch_all("SELECT side, status, client_order_id FROM orders ORDER BY side")
        assert [r["side"] for r in rows] == ["buy", "sell"]
        assert {r["status"] for r in rows} == {"filled"}
        # The same client order id the Orders tab shows, now in the table.
        assert all(str(r["client_order_id"]).startswith("PRT-") for r in rows)

        fills = db.fetch_all("SELECT commission, exchange_fees, regulatory_fees FROM fills")
        assert len(fills) == 2
        for fill in fills:
            # BUG-26's costed executor: a paper equity leg is never free.
            assert float(fill["commission"]) + float(fill["exchange_fees"]) + float(
                fill["regulatory_fees"]
            ) > 0

    def test_a_repeated_sweep_and_a_restart_both_write_nothing_new(self, db):
        runner = _equity_runner()
        runner.last_price["RELIANCE"] = 112.0
        runner.close_position("RELIANCE", reason="manual_target")

        persister = LiveTradePersister(db)
        persister.flush_runner(runner)
        first = _counts(db)
        persister.flush_runner(runner)  # same process, next sweep
        assert _counts(db) == first

        # A new process: no key set, only the durable rows. The upserts land on
        # the same primary keys, so a resume cannot duplicate the ledger.
        LiveTradePersister(db).flush_runner(runner)
        assert _counts(db) == first

    def test_the_book_adopts_the_identity_the_database_knows(self, db):
        runner = _equity_runner()
        persister = LiveTradePersister(db)
        persister.flush_runner(runner)

        portfolio_id = db.fetch_one("SELECT portfolio_id FROM portfolios")["portfolio_id"]
        assert runner.portfolio.portfolio_id == portfolio_id
        # Every child row hangs off that one id, which is what makes the Orders
        # and Positions tabs one book's history rather than two orphan sets.
        assert db.fetch_one("SELECT portfolio_id FROM positions")["portfolio_id"] == portfolio_id
        assert db.fetch_one("SELECT portfolio_id FROM orders")["portfolio_id"] == portfolio_id

    def test_a_synthetic_book_stays_out_of_the_durable_ledger(self, db):
        runner = _equity_runner(source="synthetic")
        LiveTradePersister(db).flush_runner(runner)
        assert _counts(db) == {
            "positions": 0,
            "orders": 0,
            "fills": 0,
            "trades": 0,
        }


class TestSliceOrdering:
    """The graph rules that only a slice has to respect, on the real schema."""

    #: The slice never writes the ``portfolios`` row, so the book under test has
    #: to hang off a portfolio that is already there — exactly how the live
    #: persister finds it.
    PORTFOLIO_ID = "portfolio-id"

    @pytest.fixture()
    def book_db(self, db):
        from decimal import Decimal

        from backtest.db.models import Portfolio as PortfolioRow

        with db.session() as session:
            session.add(
                PortfolioRow(
                    portfolio_id=self.PORTFOLIO_ID,
                    name="EQ",
                    initial_capital=Decimal(1_000_000),
                    current_cash=Decimal(1_000_000),
                    source="mstock",
                )
            )
        return db

    def _book(self):
        from backtest.simulator.fill import Fill
        from backtest.simulator.order import Order
        from backtest.simulator.portfolio import Portfolio

        book = Portfolio(
            name="EQ",
            initial_capital=1_000_000,
            portfolio_id=self.PORTFOLIO_ID,
            mode="paper",
        )
        order = Order(symbol="RELIANCE", side="buy", quantity=10, portfolio_id=book.portfolio_id)
        order.validate()
        order.submit()
        # The executor stamps the order id onto the fill it produces; fills are
        # born knowing which order they answer, and ``fills.order_id`` is NOT NULL.
        fill = Fill(
            symbol="RELIANCE",
            side="buy",
            quantity=10,
            fill_price=100,
            commission=20,
            order_id=order.order_id,
        )
        book.apply_fill(fill)
        order.add_fill(fill)
        book.add_order(order)
        return book

    def test_a_symbol_closed_and_reopened_keeps_one_open_row(self, book_db):
        """uq_positions_one_open_per_symbol rejects two open rows for one
        symbol. The hazard is the re-trade: the closing row is already in the
        table as ``open``, so the slice must flip it before inserting the new
        open row — same transaction, same sweep."""
        from backtest.simulator.position import Position

        book = self._book()
        written: set[str] = set()
        book.save_ledger_slice_to_db(book_db, written)
        assert book_db.fetch_one("SELECT count(*) AS n FROM positions")["n"] == 1

        closed = book.positions.pop("RELIANCE")
        closed.reduce_shares(10, 110)
        book.closed_positions.append(closed)

        reopened = Position(symbol="RELIANCE", quantity=5, average_entry_price=115)
        book.positions["RELIANCE"] = reopened
        assert reopened.position_id != closed.position_id

        book.save_ledger_slice_to_db(book_db, written)

        assert book_db.fetch_one("SELECT count(*) AS n FROM positions")["n"] == 2
        rows = book_db.fetch_all("SELECT position_id, status FROM positions")
        by_status = {r["status"]: r["position_id"] for r in rows}
        assert by_status["closed"] == closed.position_id
        assert by_status["open"] == reopened.position_id

    def test_the_key_set_makes_the_next_slice_free(self, book_db):
        book = self._book()
        written: set[str] = set()
        first = book.save_ledger_slice_to_db(book_db, written)
        assert first > 0 and written

        assert book.save_ledger_slice_to_db(book_db, written) == 0

    def test_a_rolled_back_slice_stays_retryable(self, book_db, monkeypatch):
        """Keys are only recorded once the transaction commits, so a failed
        sweep retries instead of quietly deciding the rows were sent."""
        book = self._book()
        written: set[str] = set()

        real_session = book_db.session

        def explode(*args, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(book_db, "session", explode)
        try:
            book.save_ledger_slice_to_db(book_db, written)
        except RuntimeError:
            pass
        assert written == set(), "a rolled-back slice must not claim its rows"
        assert book_db.fetch_one("SELECT count(*) AS n FROM positions")["n"] == 0

        monkeypatch.setattr(book_db, "session", real_session)
        assert book.save_ledger_slice_to_db(book_db, written) > 0
        assert book_db.fetch_one("SELECT count(*) AS n FROM positions")["n"] == 1
