"""Schema proof for migration 026 ``option_chain_history`` (V3 Phase 4).

The ORM model, the alembic revision and the hand-applied SQLite twin must all
describe the same replay asset: bar-aligned chain quotes with a unique
contract-per-bar-per-source key, the CE/PE check, and the lookup index.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from backtest.db.models import Base, OptionChainHistory

REPO = Path(__file__).resolve().parents[1]


def _row(**over):
    base = dict(
        bar_ts=datetime(2026, 11, 20, 9, 15, tzinfo=timezone.utc),
        snapshot_ts=datetime(2026, 11, 20, 9, 16, tzinfo=timezone.utc),
        source="upstox",
        underlying="NIFTY",
        expiry=date(2026, 11, 26),
        strike=Decimal("25000"),
        option_type="CE",
        instrument_token="12345",
        trading_symbol="NIFTY26NOV25000CE",
        lot_size=75,
        ltp=Decimal("120.50"),
        bid=Decimal("120.00"),
        ask=Decimal("121.00"),
        iv=0.18,
        delta=0.52,
        gamma=0.001,
        theta=-15.0,
        vega=12.0,
        volume=1000,
        oi=50000,
        spot=Decimal("25010"),
        depth_json={"bid": [[120.0, 75]], "ask": [[121.0, 75]]},
    )
    base.update(over)
    return base


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[OptionChainHistory.__table__])
    yield eng
    eng.dispose()


def test_model_creates_and_roundtrips(engine):
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        s.add(OptionChainHistory(**_row()))
        s.commit()
    with Session(engine) as s:
        got = s.query(OptionChainHistory).one()
        assert got.underlying == "NIFTY"
        assert got.strike == Decimal("25000")
        assert got.iv == pytest.approx(0.18)
        assert got.depth_json["ask"][0][0] == 121.0


def test_unique_contract_per_bar_per_source(engine):
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        s.add(OptionChainHistory(**_row()))
        s.commit()
    with Session(engine) as s:
        s.add(OptionChainHistory(**_row()))  # same contract/bar/source
        with pytest.raises(IntegrityError):
            s.commit()


def test_different_source_is_a_distinct_row(engine):
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        s.add(OptionChainHistory(**_row()))
        s.add(OptionChainHistory(**_row(source="mstock")))
        s.commit()
    with Session(engine) as s:
        assert s.query(OptionChainHistory).count() == 2


def test_option_type_check_rejects_other(engine):
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        s.add(OptionChainHistory(**_row(option_type="XX")))
        with pytest.raises(IntegrityError):
            s.commit()


def test_sqlite_twin_matches_model_columns():
    eng = create_engine("sqlite:///:memory:")
    sql = (REPO / "db" / "migrations" / "026_option_chain_history.sqlite.sql").read_text()
    with eng.begin() as conn:
        for chunk in sql.split(";"):
            stmt = "\n".join(
                ln for ln in chunk.splitlines() if not ln.strip().startswith("--")
            ).strip()
            if stmt:
                conn.execute(text(stmt))
    cols = {c["name"] for c in inspect(eng).get_columns("option_chain_history")}
    model_cols = {c.name for c in OptionChainHistory.__table__.columns}
    assert cols == model_cols
    eng.dispose()


def test_alembic_026_chains_from_025():
    import importlib.util

    path = REPO / "db" / "alembic" / "versions" / "20261120_1200_026_option_chain_history.py"
    spec = importlib.util.spec_from_file_location("mig026", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "026"
    assert mod.down_revision == "025"