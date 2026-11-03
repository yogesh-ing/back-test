"""``scripts/repair_net_pnl_fee_floor.py`` — proven against a scratch SQLite file.

This script re-prices money already in the permanent ledger, so the tests that
matter are: dry-run changes nothing, ``--execute`` moves only ``net_pnl`` (never
``gross_pnl`` or ``commission_total``), it refuses to run over B1's duplicate
copies, a row a writer changed mid-run is skipped rather than clobbered, and
``--rollback`` puts the pre-repair number back without touching rows written
since.

Everything runs on a ``tmp_path`` SQLite file with ``FORWARD_TEST_DB_URL``
pointed at it — the live Postgres ledger is never opened here, and the script
resolves its URL at call time, so the developer's ``.env`` cannot leak in.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

from backtest.db.models import Base, Portfolio, Trade

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "repair_net_pnl_fee_floor.py"

PORTFOLIO_ID = "pf-00000000-0000-0000-0000-000000000002"

#: The exact shape measured on the live ledger for 2026-10-23 → 28: a two-leg
#: structure whose ₹40 floor is sitting in ``commission_total`` while
#: ``net_pnl`` published gross realized P&L unchanged.
_REBATED = {
    "portfolio_id": PORTFOLIO_ID,
    "symbol": "NIFTY strangle",
    "direction": "short",
    "quantity": Decimal("65"),
    "entry_price": Decimal("154"),
    "exit_price": Decimal("155"),
    "entry_time": datetime(2026, 10, 28, 6, 31, 6, tzinfo=timezone.utc),
    "exit_time": datetime(2026, 10, 28, 6, 32, 6, tzinfo=timezone.utc),
    "gross_pnl": Decimal("-130.6500"),
    "net_pnl": Decimal("-130.6500"),
    "commission_total": Decimal("40.0000"),
}

#: A close written by the Ticket 2.2 writer: net already equals the contract.
_HONEST = {
    **_REBATED,
    "symbol": "BANKNIFTY strangle",
    "gross_pnl": Decimal("210.0000"),
    "net_pnl": Decimal("170.0000"),
    "commission_total": Decimal("40.0000"),
}


def _script():
    spec = importlib.util.spec_from_file_location("repair_net_pnl_fee_floor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine(tmp_path):
    url = f"sqlite:///{(tmp_path / 'ledger.sqlite3').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Portfolio(
                portfolio_id=PORTFOLIO_ID,
                name="netpnl test [pf2-0000000]",
                initial_capital=Decimal("1000"),
                current_cash=Decimal("950"),
                source="mstock",
            )
        )
        session.commit()
    return engine, url


@pytest.fixture()
def net_db(tmp_path, monkeypatch):
    """One rebated row, one honest row — no duplicate copies anywhere."""
    engine, url = _engine(tmp_path)
    with Session(engine) as session:
        session.add(
            Trade(
                trade_id="rebated",
                created_at=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
                **_REBATED,
            )
        )
        session.add(
            Trade(
                trade_id="honest",
                created_at=datetime(2026, 10, 28, 12, 1, tzinfo=timezone.utc),
                **_HONEST,
            )
        )
        session.commit()
    monkeypatch.setenv("FORWARD_TEST_DB_URL", url)
    yield engine
    engine.dispose()


@pytest.fixture()
def dup_db(tmp_path, monkeypatch):
    """The same ledger while B1's copies are still in it.

    Both rows carry every identity column — same symbol, same minute, same
    prices — because that sameness is what makes them copies rather than two
    round trips, and is what the guard has to notice.
    """
    engine, url = _engine(tmp_path)
    with Session(engine) as session:
        for index, created in enumerate(
            (
                datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc),
            )
        ):
            session.add(
                Trade(
                    trade_id=f"dup-{index}",
                    created_at=created,
                    **_REBATED,
                )
            )
        session.commit()
    monkeypatch.setenv("FORWARD_TEST_DB_URL", url)
    yield engine
    engine.dispose()


def _nets(engine) -> dict[str, Decimal]:
    with engine.connect() as conn:
        return {
            row["trade_id"]: Decimal(str(row["net_pnl"]))
            for row in conn.execute(
                text("SELECT trade_id, net_pnl FROM trades ORDER BY trade_id")
            ).mappings()
        }


def _run(module, *argv, expected=0):
    argv_backup = list(sys.argv)
    sys.argv = ["repair_net_pnl_fee_floor.py", *argv]
    try:
        code = module.main()
    finally:
        sys.argv = argv_backup
    assert code == expected
    return code


def test_dry_run_writes_nothing_and_names_the_floor(net_db, capsys):
    module = _script()
    _run(module)
    out = capsys.readouterr().out
    assert re.search(r"₹\s+40\.00 x\s+1 rows.*2 legs of the ₹20 floor", out)
    assert "net_pnl is overstated by ₹40.00" in out
    assert "stored net == gross on 1 of them" in out
    assert "dry-run — nothing written" in out
    assert _nets(net_db) == {"honest": Decimal("170"), "rebated": Decimal("-130.65")}


def test_execute_prices_only_net_pnl(net_db, capsys):
    module = _script()
    _run(module, "--execute")
    assert _nets(net_db) == {"honest": Decimal("170"), "rebated": Decimal("-170.65")}
    with net_db.connect() as conn:
        row = conn.execute(
            text("SELECT gross_pnl, commission_total FROM trades WHERE trade_id='rebated'")
        ).mappings().one()
        assert Decimal(str(row["gross_pnl"])) == Decimal("-130.65")
        assert Decimal(str(row["commission_total"])) == Decimal("40")
        bak = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE "
                 "'bak_trades_netpnl_%'")
        ).scalar_one()
    assert "violations 0 after repair" in capsys.readouterr().out
    assert bak


def test_execute_refuses_while_b1_copies_remain(dup_db):
    """Re-pricing survivors next to rebate copies would move the day total twice."""
    module = _script()
    before = _nets(dup_db)
    argv_backup = list(sys.argv)
    sys.argv = ["repair_net_pnl_fee_floor.py", "--execute"]
    try:
        with pytest.raises(SystemExit) as excinfo:
            module.main()
    finally:
        sys.argv = argv_backup
    assert "repair_trade_duplicates" in str(excinfo.value.code)
    assert _nets(dup_db) == before
    with dup_db.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'bak_trades_netpnl_%'")
        ).scalar_one() == 0


def test_execute_is_a_no_op_on_a_repaired_ledger(net_db, capsys):
    module = _script()
    _run(module, "--execute")
    capsys.readouterr()
    _run(module, "--execute")
    assert "nothing to do" in capsys.readouterr().out
    assert _nets(net_db) == {"honest": Decimal("170"), "rebated": Decimal("-170.65")}


def test_rollback_restores_the_pre_repair_net(net_db):
    module = _script()
    _run(module, "--execute")
    with net_db.connect() as conn:
        bak = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE "
                 "'bak_trades_netpnl_%'")
        ).scalar_one()
    # A close booked after the repair must keep the number the fixed writer gave it.
    with Session(net_db) as session:
        session.add(
            Trade(
                trade_id="after-repair",
                created_at=datetime(2026, 10, 31, 9, 30, tzinfo=timezone.utc),
                **{**_HONEST, "symbol": "RELIANCE", "net_pnl": Decimal("-5.0000"),
                   "gross_pnl": Decimal("35.0000")},
            )
        )
        session.commit()
    _run(module, "--rollback", "--bak-table", str(bak))
    assert _nets(net_db) == {
        "after-repair": Decimal("-5"),
        "honest": Decimal("170"),
        "rebated": Decimal("-130.65"),
    }


def test_a_row_a_writer_changed_mid_run_is_skipped_not_clobbered(net_db, capsys):
    """The UPDATE is matched on the old money too, so a stale plan cannot overwrite."""
    module = _script()
    rows = module.load_rows(net_db, True)
    row = next(r for r in rows if r["trade_id"] == "rebated")
    stored = Decimal(str(row["net_pnl"]))
    want = module.contract(row, True)
    with net_db.begin() as conn:
        conn.execute(
            update(Trade)
            .where(Trade.trade_id == "rebated")
            .values(net_pnl=Decimal("-145.0000"))
        )
    with pytest.raises(SystemExit):  # the row still breaks the contract, and says so
        module.execute(net_db, [(row, stored, want)], "midrun", False, True)
    out = capsys.readouterr().out
    assert "re-priced  0 rows" in out
    assert "skipped    1" in out
    assert _nets(net_db)["rebated"] == Decimal("-145")
