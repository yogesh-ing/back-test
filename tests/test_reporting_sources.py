"""Where a report's trades come from — DB, memory, demo and the rules between.

The PRD's V1 scope is "platform DB only", so the DB read path is the one that
has to be right: broker attribution must come from a real record or say
``unattributed``, the paper book must keep its zero costs, and a live trade
whose persister wrote brokerage only must have its statutory stack estimated
*and flagged*. The demo book exists for previews and must never appear unless
somebody asked for it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from backtest.reporting.records import (
    FEE_BASIS_ESTIMATED,
    FEE_BASIS_NONE,
    FEE_BASIS_RECORDED,
    Period,
)
from backtest.reporting.sources import (
    DEMO_ENV,
    UNATTRIBUTED_BROKER,
    DatabaseTradeSource,
    DemoTradeSource,
    MemoryTradeSource,
    resolve_sources,
)

JUL_1 = date(2026, 7, 1)
JUL_31 = date(2026, 7, 31)
JULY = Period(JUL_1, JUL_31)


# ---------------------------------------------------------------------------
# Fixtures: a real (SQLite) database with the real ORM schema
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    from backtest.db import DatabaseManager
    from backtest.db.models import Base

    manager = DatabaseManager.from_env(url=f"sqlite+pysqlite:///{tmp_path}/trades.db")
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


@pytest.fixture()
def empty_db(tmp_path):
    """Connected, but no schema — a fresh install with a DB configured."""
    from backtest.db import DatabaseManager

    manager = DatabaseManager.from_env(url=f"sqlite+pysqlite:///{tmp_path}/empty.db")
    manager.connect()
    yield manager
    manager.disconnect()


def add_portfolio(db, *, name="Book", mode="paper", source="synthetic", pid="p-1"):
    from backtest.db.models import Portfolio

    with db.session() as session:
        session.add(
            Portfolio(
                portfolio_id=pid,
                name=name,
                initial_capital=Decimal("100000"),
                current_cash=Decimal("100000"),
                mode=mode,
                source=source,
            )
        )


def add_trade(
    db,
    *,
    portfolio_id="p-1",
    trade_id="t-1",
    symbol="INFY",
    quantity="10",
    entry=Decimal("100"),
    exit_=Decimal("110"),
    commission="0",
    holding_minutes=60,
    exit_time=datetime(2026, 7, 10, 15, 0),
    net_pnl=None,
):
    from backtest.db.models import Trade

    gross = (exit_ - entry) * Decimal(quantity)
    with db.session() as session:
        session.add(
            Trade(
                trade_id=trade_id,
                portfolio_id=portfolio_id,
                symbol=symbol,
                strategy_name="rsi_reversion",
                direction="long",
                quantity=Decimal(quantity),
                entry_price=entry,
                exit_price=exit_,
                entry_time=exit_time.replace(hour=10),
                exit_time=exit_time,
                gross_pnl=gross,
                net_pnl=gross if net_pnl is None else net_pnl,
                commission_total=Decimal(commission),
                slippage_total=Decimal("0"),
                holding_period_minutes=holding_minutes,
                exit_reason="take_profit",
            )
        )


class _Config:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Runner:
    def __init__(self, config):
        self.config = config
        self.closed_trades = []


class _Manager:
    """The bits of PortfolioManager the sources actually use."""

    def __init__(self, entries=None, runners=None):
        self._entries = entries or []
        self._runners = runners or {}

    def list_instances(self):
        return list(self._entries)

    def get_runner(self, instance_id):
        return self._runners.get(instance_id)


# ---------------------------------------------------------------------------
# Database source
# ---------------------------------------------------------------------------


class TestDatabaseTradeSource:
    def test_reads_the_period_and_labelles_what_it_cannot_know(self, db):
        add_portfolio(db, mode="live", source="synthetic")
        add_trade(db, commission="120", holding_minutes=60)

        records = DatabaseTradeSource(db_manager=db).fetch(JULY)

        assert len(records) == 1
        record = records[0]
        assert record.mode == "live"
        assert record.source == "db"
        # No broker column exists in this schema: say so, never guess.
        assert record.broker == UNATTRIBUTED_BROKER
        assert any("never guessed" in note for note in record.notes)
        # Persister wrote brokerage only → statutory stack estimated + flagged.
        assert record.fees_basis == FEE_BASIS_ESTIMATED
        assert record.fees.get("brokerage") == Decimal("120")
        assert record.fees.total > Decimal("120")
        # net_pnl mirrors gross in the persister's rows → recomputed, not trusted.
        assert any("net recomputed" in note for note in record.notes)
        # synthetic bars wearing a live mode: the tag says so.
        assert record.tag == "SIMULATED DATA"

    def test_a_paper_book_keeps_its_zero_costs(self, db):
        add_portfolio(db, name="Paper Book", mode="paper", source="synthetic")
        add_trade(db, commission="0")

        record = DatabaseTradeSource(db_manager=db).fetch(JULY)[0]

        assert record.mode == "paper"
        assert record.broker == "paper"
        assert record.fees.total == Decimal("0")
        assert record.fees_basis == FEE_BASIS_NONE
        # Synthetic bars outrank the mode in the tag: the stronger warning wins.
        assert record.tag == "SIMULATED DATA"

    def test_paper_brokerage_is_recorded_but_flagged(self, db):
        add_portfolio(db, mode="paper")
        add_trade(db, commission="20")

        record = DatabaseTradeSource(db_manager=db).fetch(JULY)[0]

        assert record.fees_basis == FEE_BASIS_RECORDED
        assert record.fees.get("brokerage") == Decimal("20")
        assert any("non-zero brokerage" in note for note in record.notes)

    def test_a_recorded_net_is_authoritative_when_it_differs_from_gross(self, db):
        add_portfolio(db, mode="paper")
        add_trade(db, commission="0", net_pnl=Decimal("95"))

        record = DatabaseTradeSource(db_manager=db).fetch(JULY)[0]

        assert record.net_authoritative is True
        assert record.net_pnl_recorded == Decimal("95")

    def test_a_matching_runner_recovers_the_broker(self, db):
        """The persister names portfolios ``<run name> [<instance8>]``."""
        add_portfolio(db, mode="live", name="Live Dhan [abc12345]")
        add_trade(db, commission="120")
        config = _Config(execution_broker="dhan", mode="live", strategy_name="rsi_reversion")
        manager = _Manager(
            entries=[{"instance_id": "abc12345ef", "name": "Live Dhan", "mode": "live"}],
            runners={"abc12345ef": _Runner(config)},
        )

        record = DatabaseTradeSource(db_manager=db, manager=manager).fetch(JULY)[0]

        assert record.broker == "dhan"

    def test_segments_are_inferred_from_symbol_and_session(self, db):
        add_portfolio(db, name="P1", mode="paper", pid="p-1")
        add_portfolio(db, name="P2", mode="paper", pid="p-2")
        add_portfolio(db, name="P3", mode="paper", pid="p-3")
        add_trade(db, portfolio_id="p-1", trade_id="t-1", symbol="NIFTY26AUG24500PE")
        add_trade(db, portfolio_id="p-2", trade_id="t-2", symbol="TATAMOTORS", holding_minutes=45)
        add_trade(
            db, portfolio_id="p-3", trade_id="t-3", symbol="ITC", holding_minutes=7 * 24 * 60
        )

        by_symbol = {
            record.symbol: record.segment
            for record in DatabaseTradeSource(db_manager=db).fetch(JULY)
        }

        assert by_symbol["NIFTY26AUG24500PE"] == "options"
        assert by_symbol["TATAMOTORS"] == "equity_intraday"
        assert by_symbol["ITC"] == "equity_delivery"

    def test_trades_outside_the_period_are_not_returned(self, db):
        add_portfolio(db, mode="paper")
        add_trade(db, exit_time=datetime(2026, 6, 30, 15, 0))

        assert DatabaseTradeSource(db_manager=db).fetch(JULY) == []

    def test_an_uninitialised_database_is_a_note_not_an_error(self, empty_db):
        source = DatabaseTradeSource(db_manager=empty_db)

        assert source.fetch(JULY) == []
        assert "schema not initialised" in source.describe()["note"]

    def test_a_failing_source_never_kills_the_report(self, db):
        source = DatabaseTradeSource(db_manager=db)
        db.disconnect()  # the manager is now unusable

        assert source.fetch(JULY) == []
        assert "query failed" in source.describe()["note"]


# ---------------------------------------------------------------------------
# Memory source
# ---------------------------------------------------------------------------


class TestMemoryTradeSource:
    def _manager(self, *, mode="paper", source="synthetic", trades=None, config_extra=None):
        config = _Config(
            mode=mode,
            source=source,
            strategy_name="rsi_reversion",
            execution_broker="dhan",
            **(config_extra or {}),
        )
        runner = _Runner(config)
        runner.closed_trades = list(trades or [])
        return _Manager(
            entries=[{"instance_id": "i-1", "name": "Book", "mode": mode}],
            runners={"i-1": runner},
        )

    def test_paper_trades_keep_their_zero_costs(self):
        trade = {
            "symbol": "INFY",
            "entry_ts": "2026-07-10T10:00:00",
            "exit_ts": "2026-07-10T14:00:00",
            "units": 10,
            "entry_price": 100,
            "exit_price": 110,
            "pnl": 100,
            "side": "long",
        }
        source = MemoryTradeSource(manager=self._manager(trades=[trade]))

        record = source.fetch(JULY)[0]

        assert record.mode == "paper"
        assert record.broker == "paper"
        assert record.fees.total == Decimal("0")
        assert record.source == "memory"
        assert record.net_pnl == Decimal("100")

    def test_live_trades_get_the_statutory_stack_estimated(self):
        trade = {
            "symbol": "INFY",
            "entry_ts": "2026-07-10T10:00:00",
            "exit_ts": "2026-07-10T14:00:00",
            "units": 10,
            "entry_price": 100,
            "exit_price": 110,
            "pnl": 100,
            "commission": 20,
            "side": "long",
        }
        source = MemoryTradeSource(manager=self._manager(mode="live", trades=[trade]))

        record = source.fetch(JULY)[0]

        assert record.broker == "dhan"
        assert record.fees_basis == FEE_BASIS_ESTIMATED
        assert record.fees.total > Decimal("20")

    def test_option_structures_carry_their_realised_net(self):
        """Option P&L is recorded net of commission, so gross is rebuilt."""
        trade = {
            "symbol": "NIFTY26AUG24500PE",
            "kind": "option",
            "entry_ts": "2026-07-10T10:00:00",
            "exit_ts": "2026-07-10T14:00:00",
            "units": 75,
            "entry_price": 100,
            "exit_price": 120,
            "pnl": 1500,
            "commission": 40,
            "side": "long",
        }
        source = MemoryTradeSource(manager=self._manager(mode="live", trades=[trade]))

        record = source.fetch(JULY)[0]

        assert record.segment == "options"
        assert record.gross_pnl == Decimal("1540")
        assert record.net_pnl == Decimal("1500")
        assert record.net_authoritative is True

    def test_a_live_runner_on_synthetic_bars_is_flagged(self):
        trade = {
            "symbol": "INFY",
            "entry_ts": "2026-07-10T10:00:00",
            "exit_ts": "2026-07-10T14:00:00",
            "units": 10,
            "entry_price": 100,
            "exit_price": 110,
            "pnl": 100,
        }
        source = MemoryTradeSource(manager=self._manager(mode="live", trades=[trade]))

        record = source.fetch(JULY)[0]

        assert record.tag == "SIMULATED DATA"
        assert any("do not read this as real fills" in note for note in record.notes)

    def test_trades_outside_the_period_are_dropped(self):
        trade = {
            "symbol": "INFY",
            "entry_ts": "2026-06-01T10:00:00",
            "exit_ts": "2026-06-30T14:00:00",
            "units": 10,
            "pnl": 100,
        }
        source = MemoryTradeSource(manager=self._manager(trades=[trade]))

        assert source.fetch(JULY) == []

    def test_no_manager_is_a_note_not_an_error(self):
        class Broken:
            def list_instances(self):
                raise RuntimeError("no manager in this deployment")

        source = MemoryTradeSource(manager=Broken())

        assert source.fetch(JULY) == []
        assert "no portfolio manager" in source.describe()["note"]


# ---------------------------------------------------------------------------
# Demo book + resolution
# ---------------------------------------------------------------------------


class TestDemoAndResolution:
    def test_demo_book_is_deterministic_and_labelled(self):
        first = DemoTradeSource(seed=3).fetch(JULY)
        second = DemoTradeSource(seed=3).fetch(JULY)

        assert [t.trade_id for t in first] == [t.trade_id for t in second]
        assert [t.gross_pnl for t in first] == [t.gross_pnl for t in second]
        assert all("SIMULATED [demo]" == t.tag for t in first)
        assert all(t.source == "demo" for t in first)

    def test_demo_is_never_resolved_unless_asked(self, monkeypatch):
        monkeypatch.delenv(DEMO_ENV, raising=False)
        assert resolve_sources(include_db=False, include_memory=False).sources == []

        monkeypatch.setenv(DEMO_ENV, "1")
        env_bundle = resolve_sources(include_db=False, include_memory=False)
        assert [s.name for s in env_bundle.sources] == ["demo"]

        explicit = resolve_sources(include_db=False, include_memory=False, demo=True)
        assert [s.name for s in explicit.sources] == ["demo"]

    def test_demo_alongside_real_sources_warns(self, monkeypatch):
        monkeypatch.delenv(DEMO_ENV, raising=False)
        bundle = resolve_sources(include_db=False, demo=True)

        assert [s.name for s in bundle.sources] == ["memory", "demo"]
        assert any("SIMULATED" in warning for warning in bundle.warnings)

    def test_a_bundle_with_no_sources_says_so(self, monkeypatch):
        monkeypatch.delenv(DEMO_ENV, raising=False)
        bundle = resolve_sources(include_db=False, include_memory=False)

        assert bundle.sources == []
        assert any("no trade sources" in warning for warning in bundle.warnings)

    def test_one_dead_source_becomes_a_warning(self):
        class Exploding:
            name = "boom"

            def describe(self):
                return {"name": self.name, "kind": "test", "note": ""}

            def fetch(self, period):
                raise RuntimeError("connection reset")

        bundle = resolve_sources(include_db=False, include_memory=False)
        bundle.sources.append(Exploding())

        records, warnings = bundle.fetch(JULY)

        assert records == []
        assert any("source boom failed: connection reset" in w for w in warnings)
