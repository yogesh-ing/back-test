"""``scripts/repair_trade_duplicates.py`` — proven against a scratch SQLite file.

The script deletes rows from the permanent ledger, so the test that matters is
not "does it print a nice plan" but: dry-run changes nothing, ``--execute`` keeps
one survivor per round trip and re-keys it, and ``--rollback`` puts the deleted
copies back without touching rows written since.

Everything runs on a ``tmp_path`` SQLite file with ``FORWARD_TEST_DB_URL``
pointed at it — the live Postgres ledger is never opened here, and the script
resolves its URL at call time, so the developer's ``.env`` cannot leak in.
"""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backtest.db.models import Base, Portfolio, Trade

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "repair_trade_duplicates.py"

PORTFOLIO_ID = "pf-00000000-0000-0000-0000-000000000001"
#: The name ``LiveTradePersister`` gives a runner's row — kept identical so the
#: last test can hand the repaired ledger back to the writer.
RUNNER_NAME = "dup test"
INSTANCE_ID = "pf1-000000000"
PORTFOLIO_NAME = f"{RUNNER_NAME} [{INSTANCE_ID[:8]}]"
_BASE = {
    "portfolio_id": PORTFOLIO_ID,
    "symbol": "NIFTY long_call 23400",
    "direction": "long",
    "quantity": Decimal("65"),
    "entry_price": Decimal("154"),
    "exit_price": Decimal("155"),
    "entry_time": datetime(2026, 10, 28, 6, 31, 6, tzinfo=timezone.utc),
    "exit_time": datetime(2026, 10, 28, 6, 32, 6, tzinfo=timezone.utc),
    "gross_pnl": Decimal("-100"),
    "net_pnl": Decimal("-120"),
    "commission_total": Decimal("20"),
}


def _script():
    spec = importlib.util.spec_from_file_location("repair_trade_duplicates", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def dup_db(tmp_path, monkeypatch):
    """A file-backed SQLite ledger holding 2 copies of one trade + 1 distinct one."""
    url = f"sqlite:///{(tmp_path / 'ledger.sqlite3').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Portfolio(
                portfolio_id=PORTFOLIO_ID,
                name=PORTFOLIO_NAME,
                initial_capital=Decimal("1000"),
                current_cash=Decimal("950"),
                source="mstock",
            )
        )
        # Three copies of the SAME round trip, written by three restarts, and one
        # genuinely distinct trade. The copies carry no dedupe_key — that is the
        # shape a database is in the day 022 lands.
        for index, created in enumerate(
            (
                datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc),
                datetime(2026, 10, 30, 9, 31, tzinfo=timezone.utc),
            )
        ):
            session.add(
                Trade(
                    trade_id=f"dup-{index}",
                    created_at=created,
                    **_BASE,
                )
            )
        session.add(
            Trade(
                trade_id="other",
                created_at=datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc),
                **{**_BASE, "symbol": "RELIANCE", "net_pnl": Decimal("-5")},
            )
        )
        session.commit()
    monkeypatch.setenv("FORWARD_TEST_DB_URL", url)
    yield engine
    engine.dispose()


def _rows(engine) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT trade_id, dedupe_key FROM trades ORDER BY trade_id")
            ).mappings()
        ]


def _run(module, *argv, expected=0):
    import sys

    argv_backup = list(sys.argv)
    sys.argv = ["repair_trade_duplicates.py", *argv]
    try:
        code = module.main()
    finally:
        sys.argv = argv_backup
    assert code == expected
    return code


def test_dry_run_writes_nothing(dup_db):
    module = _script()
    before = _rows(dup_db)
    _run(module)  # no flags = dry-run
    assert _rows(dup_db) == before
    with dup_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM trades")).scalar_one() == 4


def test_execute_keeps_one_survivor_and_rekeys_it(dup_db):
    module = _script()
    _run(module, "--execute")
    rows = {r["trade_id"]: r["dedupe_key"] for r in _rows(dup_db)}
    assert set(rows) == {"dup-0", "other"}  # the earliest copy survived
    assert rows["dup-0"] is not None and len(rows["dup-0"]) == 64
    assert rows["other"] is not None
    with dup_db.connect() as conn:
        # The money the duplicates invented is gone; the survivor keeps its own.
        total = conn.execute(text("SELECT SUM(net_pnl) FROM trades")).scalar_one()
    assert Decimal(str(total)) == Decimal("-125")


def test_execute_is_a_no_op_on_a_repaired_ledger(dup_db, capsys):
    module = _script()
    _run(module, "--execute")
    capsys.readouterr()
    _run(module, "--execute")
    assert "nothing to do" in capsys.readouterr().out
    assert {r["trade_id"] for r in _rows(dup_db)} == {"dup-0", "other"}


def test_rollback_restores_only_the_deleted_copies(dup_db):
    module = _script()
    _run(module, "--execute")
    with dup_db.connect() as conn:
        bak = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE "
                "'bak_trades_dupes_%'"
            )
        ).scalar_one()
    # A trade booked after the repair must survive the rollback.
    with Session(dup_db) as session:
        session.add(
            Trade(
                trade_id="after-repair",
                created_at=datetime(2026, 10, 31, 9, 30, tzinfo=timezone.utc),
                **{**_BASE, "symbol": "INFY", "net_pnl": Decimal("7")},
            )
        )
        session.commit()
    _run(module, "--rollback", "--bak-table", bak)
    ids = {r["trade_id"] for r in _rows(dup_db)}
    assert ids == {"dup-0", "dup-1", "dup-2", "other", "after-repair"}


def test_refuses_an_unmigrated_database(dup_db):
    """The guardrail that keeps a pre-022 run from half-repairing the ledger."""
    module = _script()
    with dup_db.begin() as conn:
        # SQLite cannot leave an index pointing at a vanished column.
        conn.execute(text("DROP INDEX uq_trades_dedupe_key"))
        conn.execute(text("ALTER TABLE trades DROP COLUMN dedupe_key"))
    with pytest.raises(SystemExit) as excinfo:
        _run(module)
    assert "022" in str(excinfo.value.code)
    with dup_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM trades")).scalar_one() == 4


def test_survivors_are_the_same_group_the_writer_would_skip(dup_db):
    """The script's grouping and the writer's lookup must agree, exactly.

    If they diverged, the repaired rows would still re-insert on the next
    restart — and the whole point of B1 is that they cannot.
    """
    from backtest.forward.trade_persistence import LiveTradePersister

    class _Manager:
        """The bit of ``DatabaseManager`` the persister actually uses."""

        def __init__(self, engine):
            self.engine = engine

        @contextmanager
        def session(self):
            with Session(self.engine) as session:
                yield session
                session.commit()

    class _Runner:
        instance_id = INSTANCE_ID
        config = type(
            "C", (), {"name": RUNNER_NAME, "strategy_name": "s", "source": "mstock"}
        )()
        portfolio = type("P", (), {"current_cash": 950})()

        @property
        def closed_trades(self):
            return [
                {
                    "symbol": _BASE["symbol"],
                    "side": "LONG",
                    "qty": 65,
                    "units": 65,
                    "entry_price": 154.0,
                    "exit_price": 155.0,
                    "entry_ts": "2026-10-28T06:31:06",
                    "exit_ts": "2026-10-28T06:32:06",
                    "pnl": -100.0,
                    "net_pnl": -120.0,
                    "commission": 20.0,
                    "exit_reason": "stop",
                }
            ]

    module = _script()
    _run(module, "--execute")
    assert LiveTradePersister(_Manager(dup_db)).flush_runner(_Runner()) == 0
    with dup_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM trades")).scalar_one() == 2
