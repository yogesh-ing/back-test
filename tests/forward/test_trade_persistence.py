"""LiveTradePersister — synthetic tests (in-memory SQLite, no broker).

GAP-4: every closed runner trade must land in the `trades` table exactly once,
survive nothing (it's permanent), and never break on bad rows. All data here is
synthetic — no live market, no broker session.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backtest.forward.trade_persistence import LiveTradePersister, _map_exit_reason


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

        rows = db.fetch_all("SELECT symbol, net_pnl, exit_reason FROM trades")
        assert len(rows) == 1
        assert "long_call" in rows[0]["symbol"]
        assert float(rows[0]["net_pnl"]) == pytest.approx(58.5)

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
