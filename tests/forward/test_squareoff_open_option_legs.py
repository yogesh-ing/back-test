"""``scripts/squareoff_open_option_legs.py`` — proven against a scratch state file.

The script re-prices paper positions that no DB row describes (the live
deployment is at alembic 022, so the option mirror tables do not exist), so the
tests that matter are: a mark below its own intrinsic value is refused and left
open rather than squared at an invented price, a sound mark closes with the
writer's arithmetic (gross realized, cash moved by notional minus exit fees,
``exit_reason`` of ``manual``), a dry run changes nothing, ``--execute`` leaves
a restorable ``bak_`` snapshot, and ``--rollback`` puts the file back byte for
byte.

Everything runs on a ``tmp_path`` JSON file — the real
``src/data/portfolio_state.json`` is never opened here.
"""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "squareoff_open_option_legs.py"


def _script():
    spec = importlib.util.spec_from_file_location("squareoff_open_option_legs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _leg(**overrides):
    leg = {
        "position_id": "pos_1",
        "structure_id": "struct_1",
        "trading_symbol": "NIFTY26NOV23350CE",
        "instrument_token": "64359",
        "underlying": "NIFTY",
        "option_type": "CE",
        "strike": "23350",
        "expiry": "2026-11-23",
        "lot_size": 65,
        "side": "BUY",
        "quantity": 1,
        "entry_price": "352.50",
        "current_price": "579.7",
        "status": "open",
        "realized_pnl": "0",
        "unrealized_pnl": "14768.00",
        "commission": "20.0",
        "opened_at": "2026-11-02T06:45:00+00:00",
        "closed_at": None,
        "last_updated": "2026-11-03T17:15:55+00:00",
        "mtm_failures": 0,
        "last_quote_error": None,
    }
    leg.update(overrides)
    return leg


def _structure(legs, **overrides):
    struct = {
        "structure_id": "struct_1",
        "structure_type": "long_call",
        "strategy_name": "CE_instant_buy",
        "underlying": "NIFTY",
        "expiry": "2026-11-23",
        "opened_at": "2026-11-02T06:45:00+00:00",
        "closed_at": None,
        "exit_reason": None,
        "session_date": None,
        "entry_session_date": "2026-11-02",
        "last_mark_session": "2026-11-03",
        "last_mark_net_pnl": "14714.19",
        "prior_close_net_pnl": "-1461.06",
        "statutory_fees": "33.81",
        "legs": legs,
    }
    struct.update(overrides)
    return struct


def _runner(iid, book, structures, spot="23810.85"):
    return {
        "instance_id": iid,
        "status": "STOPPED",
        "config": {"instance_id": iid, "name": f"book_{iid}"},
        "bridge": {
            "scalars": {"last_spot": spot, "open_structure_id": "struct_1"},
            "broker": {
                "capital": "1000000.0",
                "available_cash": book,
                "statutory_fees_paid": "1261.28",
                "structures": structures,
            },
        },
    }


@pytest.fixture()
def state_file(tmp_path):
    good = _leg()
    payload = {
        "schema": 1,
        "saved_at": "2026-11-03T19:29:45.606467+00:00",
        "manager": {"total_capital": 10400000.0},
        "runners": [
            _runner("good", "949291.22", [_structure([good])]),
        ],
    }
    path = tmp_path / "portfolio_state.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _book_of(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["runners"][0]["bridge"]["broker"]


def test_dry_run_writes_nothing(state_file):
    before = state_file.read_text(encoding="utf-8")
    assert _script().main(["--state-file", str(state_file)]) == 0
    assert state_file.read_text(encoding="utf-8") == before


def test_execute_closes_the_leg_with_the_writer_arithmetic(state_file):
    assert _script().main(["--state-file", str(state_file), "--execute", "--force"]) == 0
    book = _book_of(state_file)
    leg = book["structures"][0]["legs"][0]
    struct = book["structures"][0]

    units = Decimal(leg["quantity"]) * Decimal(leg["lot_size"])
    expected_gross = (Decimal("579.7") - Decimal("352.50")) * units
    assert leg["status"] == "closed"
    assert leg["closed_at"] is not None
    assert Decimal(leg["realized_pnl"]) == expected_gross
    assert Decimal(leg["unrealized_pnl"]) == 0
    assert struct["closed_at"] is not None
    assert struct["exit_reason"] == "manual"
    assert struct["session_date"]  # IST calendar date, stamped on close

    cash = Decimal(book["available_cash"])
    assert cash > Decimal("949291.22")  # the ₹579.7 x 65 notional came back
    fees = Decimal(struct["statutory_fees"]) - Decimal("33.81")
    assert fees > 0  # the exit side charged something real
    assert cash == Decimal("949291.22") + Decimal("579.7") * units - fees


def test_execute_snapshots_and_archives_then_rollback_restores(state_file, capsys):
    before = state_file.read_text(encoding="utf-8")
    assert _script().main(["--state-file", str(state_file), "--execute", "--force"]) == 0
    out = capsys.readouterr().out
    bak = next(
        line.split(": ", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("snapshot ")
    )
    archive = next(
        line.split(": ", 1)[1].strip() for line in out.splitlines() if line.startswith("archive ")
    )
    assert Path(bak).exists() and Path(archive).exists()
    assert json.loads(Path(bak).read_text(encoding="utf-8")) == json.loads(before)
    archived = json.loads(Path(archive).read_text(encoding="utf-8"))
    assert archived["books"][0]["bridge"]["broker"]["structures"]

    assert _script().main(
        ["--state-file", str(state_file), "--rollback", "--bak-file", bak, "--force"]
    ) == 0
    assert state_file.read_text(encoding="utf-8") == before


def test_mark_below_intrinsic_is_refused_and_left_open(state_file):
    """NIFTY 24,400 with a 23,350 CE marked ₹579.7 is the fabricated-ladder shape."""
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    runner = payload["runners"][0]
    runner["bridge"]["scalars"]["last_spot"] = "24400"
    state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    before = state_file.read_text(encoding="utf-8")

    assert _script().main(["--state-file", str(state_file), "--execute", "--force"]) == 0
    assert state_file.read_text(encoding="utf-8") == before
    book = _book_of(state_file)
    assert book["structures"][0]["legs"][0]["closed_at"] is None
    assert book["available_cash"] == "949291.22"


def test_zero_mark_is_refused(state_file):
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    payload["runners"][0]["bridge"]["broker"]["structures"][0]["legs"][0]["current_price"] = "0"
    state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    before = state_file.read_text(encoding="utf-8")

    assert _script().main(["--state-file", str(state_file), "--execute", "--force"]) == 0
    assert state_file.read_text(encoding="utf-8") == before
