"""LiveTradePersister — synthetic tests (in-memory SQLite, no broker).

GAP-4: every closed runner trade must land in the `trades` table exactly once,
survive nothing (it's permanent), and never break on bad rows. All data here is
synthetic — no live market, no broker session.

B1 (2026-10-30) added the half that GAP-4 assumed: "exactly once" has to hold
across a **process boundary**, not just across two sweeps of one persister —
see ``TestResumeIsIdempotent``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from backtest.forward.trade_persistence import (
    LiveTradePersister,
    _map_exit_reason,
    trade_dedupe_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """In-memory SQLite with the full schema created."""
    from backtest.db import DatabaseManager
    from backtest.db.config import DatabaseConfig

    cfg = DatabaseConfig(url="sqlite://", echo=False)
    manager = DatabaseManager(cfg)
    manager.connect()
    from backtest.db.models import Base

    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


class _FakeConfig:
    def __init__(self, name="test runner", strategy="vwap_ema_rsi", capital=100000,
                 source="mstock"):
        self.name = name
        self.strategy_name = strategy
        self.allocated_capital = capital
        self.source = source


class _FakePortfolio:
    cash = 95000.0


class _FakeRunner:
    def __init__(self, trades, instance_id="inst123"):
        self.instance_id = instance_id
        self.config = _FakeConfig()
        self.portfolio = _FakePortfolio()
        self._closed = trades

    @property
    def closed_trades(self):
        return self._closed


def _trade(symbol="NIFTY long_call 23400", pnl=58.5, exit_reason="target",
           exit_ts="2026-09-21T06:32:06", entry_ts="2026-09-21T06:31:06"):
    return {
        "symbol": symbol,
        "kind": "option",
        "side": "LONG",
        "qty": 1,
        "units": 65,
        "entry_price": 154.0,
        "exit_price": 155.0,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "pnl": pnl,
        "win": pnl >= 0,
        "exit_reason": exit_reason,
        "commission": 20.0,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFlush:
    def test_trade_lands_in_db(self, db):
        p = LiveTradePersister(db)
        runner = _FakeRunner([_trade()])
        written = p.flush_runner(runner)
        assert written == 1

        rows = db.fetch_all(
            "SELECT symbol, gross_pnl, net_pnl, commission_total, exit_reason FROM trades"
        )
        assert len(rows) == 1
        assert "long_call" in rows[0]["symbol"]
        # A record that names its costs but not its net is netted, never
        # copied: BUG-26 was exactly one number written into both columns.
        assert float(rows[0]["gross_pnl"]) == pytest.approx(58.5)
        assert float(rows[0]["net_pnl"]) == pytest.approx(38.5)
        assert float(rows[0]["commission_total"]) == pytest.approx(20.0)
        assert rows[0]["exit_reason"] == "take_profit"

    def test_synthetic_source_runner_never_persists(self, db):
        """Owner decision 2026-09-22: fake-data fills stay OUT of the permanent
        DB — synthetic/replay trades would pollute the real trade history."""
        p = LiveTradePersister(db)
        for source in ("synthetic", "replay"):
            runner = _FakeRunner([_trade()], instance_id=f"inst-{source}")
            runner.config.source = source
            assert p.flush_runner(runner) == 0
        rows = db.fetch_all("SELECT symbol FROM trades")
        assert rows == []

    def test_mstock_source_runner_persists(self, db):
        """Real market data (mstock) trades DO persist."""
        p = LiveTradePersister(db)
        runner = _FakeRunner([_trade()])
        runner.config.source = "mstock"
        assert p.flush_runner(runner) == 1

    def test_no_duplicates_on_second_flush(self, db):
        p = LiveTradePersister(db)
        runner = _FakeRunner([_trade()])
        assert p.flush_runner(runner) == 1
        assert p.flush_runner(runner) == 0
        rows = db.fetch_all("SELECT COUNT(*) AS n FROM trades")
        assert rows[0]["n"] == 1

    def test_incremental_flush_new_trades_only(self, db):
        p = LiveTradePersister(db)
        runner = _FakeRunner([_trade(exit_ts="2026-09-21T06:32:06")])
        assert p.flush_runner(runner) == 1
        runner._closed.append(_trade(exit_ts="2026-09-21T06:35:06", pnl=-94.0,
                                     exit_reason="stop"))
        assert p.flush_runner(runner) == 1
        rows = db.fetch_all("SELECT COUNT(*) AS n FROM trades")
        assert rows[0]["n"] == 2

    def test_portfolio_row_created_once_per_runner(self, db):
        p = LiveTradePersister(db)
        runner = _FakeRunner([_trade(), _trade(exit_ts="2026-09-21T07:00:00")])
        p.flush_runner(runner)
        rows = db.fetch_all("SELECT COUNT(*) AS n FROM portfolios")
        assert rows[0]["n"] == 1

    def test_bad_trade_does_not_block_the_rest(self, db):
        p = LiveTradePersister(db)
        bad = _trade(entry_ts="not-a-timestamp")
        good = _trade(exit_ts="2026-09-21T06:40:00")
        runner = _FakeRunner([bad, good])
        # bad row may fail its insert; good must still land
        p.flush_runner(runner)
        rows = db.fetch_all("SELECT COUNT(*) AS n FROM trades")
        assert rows[0]["n"] >= 1

    def test_empty_runner_is_noop(self, db):
        p = LiveTradePersister(db)
        assert p.flush_runner(_FakeRunner([])) == 0


class TestResumeIsIdempotent:
    """B1: a restart must not re-insert the closed-trades cache.

    ``_flushed`` lives in memory, so on the next process it is empty while the
    DB still holds every row — which is how 28-Oct's 2,878 round trips became
    6,476 rows and ₹2,23,635.74 of the ledger phantom loss. The guard has to be
    the row's own identity, not the persister's memory.
    """

    def test_second_persister_writes_nothing(self, db):
        assert LiveTradePersister(db).flush_runner(_FakeRunner([_trade()])) == 1
        # New process: brand-new persister, same restored cache.
        resumed = LiveTradePersister(db)
        assert resumed.flush_runner(_FakeRunner([_trade()])) == 0
        assert resumed.flush_runner(_FakeRunner([_trade()])) == 0
        assert db.fetch_all("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 1

    def test_resume_survives_an_edited_cache_tail(self, db):
        """Only the already-written trades are skipped; a new close still lands."""
        LiveTradePersister(db).flush_runner(_FakeRunner([_trade()]))
        runner = _FakeRunner(
            [_trade(), _trade(exit_ts="2026-09-21T07:10:00", pnl=-40.0, exit_reason="stop")]
        )
        assert LiveTradePersister(db).flush_runner(runner) == 1
        assert db.fetch_all("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 2

    def test_distinct_round_trips_are_not_collapsed(self, db):
        """Non-vacuity: the guard skips duplicates, not trades that merely look alike.

        Same symbol, same price, same P&L — a second scalp ten minutes later is
        a real trade and must be written.
        """
        minutes = ("06:32:06", "06:42:06", "06:52:06")
        runner = _FakeRunner([_trade(exit_ts=f"2026-09-21T{m}") for m in minutes])
        persister = LiveTradePersister(db)
        assert persister.flush_runner(runner) == 3
        assert db.fetch_all("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 3
        # and a restart over that same three-trade cache adds nothing
        assert LiveTradePersister(db).flush_runner(runner) == 0

    def test_keyless_history_from_before_022_is_not_re_inserted(self, db):
        """The live shape on upgrade day: the rows exist, their keys are NULL.

        Revision 022 leaves ``dedupe_key`` NULL on every pre-022 row, and the
        unique index therefore cannot see them — so the column lookup, not the
        constraint, is what stops a resume from doubling the 2,878 real trades
        already in ``forward_test``. This is the test that fails if the lookup
        is removed while the index stays.
        """
        from backtest.db.models import Trade as TradeRow

        assert LiveTradePersister(db).flush_runner(_FakeRunner([_trade()])) == 1
        with db.session() as session:
            session.query(TradeRow).one().dedupe_key = None

        assert LiveTradePersister(db).flush_runner(_FakeRunner([_trade()])) == 0
        assert db.fetch_all("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 1

    def test_the_key_survives_a_clock_label_change(self, db):
        """The same instant under another offset is still the same trade.

        The cache is JSON, the column is TIMESTAMPTZ, and this box's session
        timezone is Asia/Calcutta — keying on the string would have made every
        restore a fresh insert.
        """
        naive = _trade(exit_ts="2026-09-21T06:32:06", entry_ts="2026-09-21T06:31:06")
        assert LiveTradePersister(db).flush_runner(_FakeRunner([naive])) == 1
        relabelled = _trade(
            exit_ts="2026-09-21T12:02:06+05:30", entry_ts="2026-09-21T12:01:06+05:30"
        )
        assert LiveTradePersister(db).flush_runner(_FakeRunner([relabelled])) == 0
        assert db.fetch_all("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 1

    def test_persisted_row_carries_its_identity_key(self, db):
        LiveTradePersister(db).flush_runner(_FakeRunner([_trade()]))
        rows = db.fetch_all("SELECT dedupe_key FROM trades")
        assert rows[0]["dedupe_key"] is not None
        assert len(rows[0]["dedupe_key"]) == 64

    def test_the_unique_index_is_the_last_line(self, db):
        """Even a writer that skipped the lookup cannot land the key twice."""
        from backtest.db.models import Trade as TradeRow

        LiveTradePersister(db).flush_runner(_FakeRunner([_trade()]))
        with db.session() as session:
            first = session.query(TradeRow).one()
            clone = TradeRow(
                **{c.key: getattr(first, c.key) for c in first.__table__.columns}
            )
            clone.trade_id = "a-different-id"
            session.add(clone)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()


class TestDedupeKey:
    """The key must be a function of the round trip's identity and nothing else.

    ``scripts/repair_trade_duplicates.py`` recomputes it from stored rows to
    backfill history, so a value that drifted between the writer's inputs and
    the DB's reading of them would leave the old duplicates invisible to the
    guard.
    """

    @staticmethod
    def _values(**overrides):
        base = {
            "portfolio_id": "pf-1",
            "symbol": "NIFTY long_call 23400",
            "direction": "long",
            "entry_time": datetime.fromisoformat("2026-09-21T06:31:06+00:00"),
            "exit_time": datetime.fromisoformat("2026-09-21T06:32:06+00:00"),
            "entry_price": Decimal("154.0"),
            "exit_price": Decimal("155"),
            "quantity": Decimal("65"),
        }
        base.update(overrides)
        return base

    def test_identical_round_trip_hashes_identically(self):
        assert trade_dedupe_key(self._values()) == trade_dedupe_key(self._values())

    def test_number_scale_and_clock_label_do_not_change_it(self):
        assert trade_dedupe_key(self._values()) == trade_dedupe_key(
            self._values(
                entry_price=Decimal("154.00000000"),
                exit_time=datetime.fromisoformat("2026-09-21T12:02:06+05:30"),
            )
        )

    def test_money_is_not_identity(self):
        """A corrected fee re-states the same trade, it does not create one."""
        costs = self._values(net_pnl=Decimal("-140.00"), gross_pnl=Decimal("-100.00"))
        assert trade_dedupe_key(costs) == trade_dedupe_key(
            dict(costs, net_pnl=Decimal("-100.00"), commission_total=Decimal("0"))
        )
        assert trade_dedupe_key(self._values()) != trade_dedupe_key(
            self._values(symbol="OTHER")
        )

    def test_missing_column_is_refused_not_hashed(self):
        for column in ("portfolio_id", "exit_time", "quantity"):
            values = self._values()
            del values[column]
            with pytest.raises(KeyError, match=column):
                trade_dedupe_key(values)


class TestExitReasonMapping:
    def test_known_reasons(self):
        assert _map_exit_reason("target 68 >= 5") == "take_profit"
        assert _map_exit_reason("stop -182 <= -5") == "stop_loss"
        assert _map_exit_reason("expiry settlement") == "eod_flat"
        assert _map_exit_reason("manual close") == "manual"

    def test_unknown_falls_back_to_signal(self):
        assert _map_exit_reason("mystery") == "signal"

    def test_empty_is_none(self):
        assert _map_exit_reason("") is None
        assert _map_exit_reason(None) is None
