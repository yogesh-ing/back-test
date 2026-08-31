"""Migration 002 — ``portfolios.mode`` / ``portfolios.source`` (ticket P1.1).

Runs entirely on file-backed SQLite (``tmp_path``) so the hand-written
migration files are executed **verbatim** with ``executescript()`` — the same
sequence a developer applies locally: ``001_initial_schema.sqlite.sql`` first,
then ``002_add_mode_source.sqlite.sql``.

Covered (ticket acceptance criteria):

* the migration applies cleanly on a fresh DB (001 only) AND on an existing
  DB (001 already applied, rows present);
* existing (legacy) rows are backfilled to ``paper``/``synthetic``;
* the CHECK constraints reject ``mode='bogus'`` and any bad ``source``;
* new rows pick up the server defaults when the columns are omitted;
* the ``schema_migrations`` ledger records ``002``;
* the hand-written 001+002 schema still matches the ORM models (repo rule:
  SQL is the source of truth, the ORM mirrors it).

PostgreSQL-specific behaviour (``ADD COLUMN IF NOT EXISTS``, table-level
CHECKs, COMMENTs) cannot be exercised on SQLite; the PG file is verified
textually, matching the repo convention in ``tests/test_db_schema.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from backtest.db.models import Base, Portfolio

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
PG_001 = MIGRATIONS / "001_initial_schema.sql"
SQL_001 = MIGRATIONS / "001_initial_schema.sqlite.sql"
SQL_002 = MIGRATIONS / "002_add_mode_source.sqlite.sql"
PG_002 = MIGRATIONS / "002_add_mode_source.sql"

LEGACY_ID = "11111111-1111-1111-1111-111111111111"
LEGACY_NAME = "Legacy Run"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _apply(conn: sqlite3.Connection, *files: Path) -> None:
    for path in files:
        assert path.exists(), f"missing migration file: {path}"
        conn.executescript(path.read_text())


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    """A 'fresh' deployment: 001 applied, zero rows, 002 not yet run."""
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_001)
    finally:
        conn.close()
    return db


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    """An 'existing' deployment: 001 applied with a pre-002 portfolios row."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_001)
        conn.execute(
            "INSERT INTO portfolios (portfolio_id, name, initial_capital, current_cash) "
            "VALUES (?, ?, 100000, 100000)",
            (LEGACY_ID, LEGACY_NAME),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _apply_002(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_002)
    finally:
        conn.close()


def _portfolios_columns(db: Path) -> dict[str, dict]:
    engine = create_engine(f"sqlite:///{db}")
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("portfolios")}
    finally:
        engine.dispose()


def _fetch_one(db: Path, sql: str, params: tuple = ()) -> tuple:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(sql, params).fetchone()
        assert row is not None, f"query returned no row: {sql}"
        return row
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The ticket's test: fresh + backfill + CHECK rejection
# ---------------------------------------------------------------------------


def test_migration_applies_fresh_and_backfills(fresh_db: Path, legacy_db: Path):
    # 1) apply on an empty DB -> columns exist, defaults are 'paper'/'synthetic'
    _apply_002(fresh_db)
    cols = _portfolios_columns(fresh_db)
    assert "mode" in cols and "source" in cols
    assert cols["mode"]["default"] == "'paper'"
    assert cols["source"]["default"] == "'synthetic'"
    assert cols["mode"]["nullable"] is False
    assert cols["source"]["nullable"] is False

    # a new row that omits the columns gets the defaults
    conn = sqlite3.connect(fresh_db)
    try:
        conn.execute(
            "INSERT INTO portfolios (portfolio_id, name, initial_capital, current_cash) "
            "VALUES ('22222222-2222-2222-2222-222222222222', 'Fresh Run', 1000, 1000)"
        )
        conn.commit()
        mode, source = conn.execute(
            "SELECT mode, source FROM portfolios WHERE name = 'Fresh Run'"
        ).fetchone()
        assert (mode, source) == ("paper", "synthetic")
    finally:
        conn.close()

    # 2) apply on an existing DB -> the legacy row is backfilled
    _apply_002(legacy_db)
    mode, source = _fetch_one(
        legacy_db,
        "SELECT mode, source FROM portfolios WHERE portfolio_id = ?",
        (LEGACY_ID,),
    )
    assert (mode, source) == ("paper", "synthetic")

    # 3) CHECK constraints reject mode='foo'
    with pytest.raises(IntegrityError):
        engine = create_engine(f"sqlite:///{legacy_db}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios "
                        "(portfolio_id, name, initial_capital, current_cash, mode) "
                        "VALUES ('33333333-3333-3333-3333-333333333333', 'Bad Mode', 1000, 1000, 'foo')"
                    )
                )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Acceptance: applies on fresh AND existing DB
# ---------------------------------------------------------------------------


def test_applies_on_fresh_db(fresh_db: Path):
    _apply_002(fresh_db)  # must not raise
    assert "mode" in _portfolios_columns(fresh_db)


def test_applies_on_existing_db_with_legacy_rows(legacy_db: Path):
    _apply_002(legacy_db)  # must not raise
    (total,) = _fetch_one(legacy_db, "SELECT COUNT(*) FROM portfolios")
    assert total == 1  # backfill is in-place, no row duplication
    assert _portfolios_columns(legacy_db)["mode"]["default"] == "'paper'"


def test_legacy_rows_backfilled(legacy_db: Path):
    _apply_002(legacy_db)
    row = _fetch_one(
        legacy_db,
        "SELECT name, mode, source, status FROM portfolios WHERE portfolio_id = ?",
        (LEGACY_ID,),
    )
    assert row == (LEGACY_NAME, "paper", "synthetic", "active")  # other columns untouched


# ---------------------------------------------------------------------------
# Acceptance: invalid values rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_mode", ["foo", "PAPER", "live ", ""])
def test_check_rejects_bad_mode(fresh_db: Path, bad_mode: str):
    _apply_002(fresh_db)
    engine = create_engine(f"sqlite:///{fresh_db}")
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios "
                        "(portfolio_id, name, initial_capital, current_cash, mode) "
                        "VALUES ('44444444-4444-4444-4444-444444444444', :name, 1000, 1000, :mode)"
                    ),
                    {"name": f"Bad {bad_mode!r}", "mode": bad_mode},
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad_source", ["foo", "1min", "MSTOCK", ""])
def test_check_rejects_bad_source(fresh_db: Path, bad_source: str):
    _apply_002(fresh_db)
    engine = create_engine(f"sqlite:///{fresh_db}")
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios "
                        "(portfolio_id, name, initial_capital, current_cash, source) "
                        "VALUES ('55555555-5555-5555-5555-555555555555', :name, 1000, 1000, :source)"
                    ),
                    {"name": f"Bad {bad_source!r}", "source": bad_source},
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mode,source", [("paper", "synthetic"), ("live", "mstock"), ("paper", "replay")]
)
def test_valid_combos_accepted(fresh_db: Path, mode: str, source: str):
    _apply_002(fresh_db)
    engine = create_engine(f"sqlite:///{fresh_db}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO portfolios "
                    "(portfolio_id, name, initial_capital, current_cash, mode, source) "
                    "VALUES ('66666666-6666-6666-6666-666666666666', :name, 1000, 1000, :mode, :source)"
                ),
                {"name": f"OK {mode}/{source}", "mode": mode, "source": source},
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Ledger + schema-parity bookkeeping
# ---------------------------------------------------------------------------


def test_ledger_records_both_migrations(legacy_db: Path):
    _apply_002(legacy_db)
    versions = _fetch_one(
        legacy_db, "SELECT COUNT(*) FROM schema_migrations WHERE version IN ('001','002')"
    )
    assert versions == (2,)


def test_orm_schema_matches_handwritten_after_002(legacy_db: Path):
    """Applying 001+002 by hand yields the same portfolios columns as the ORM."""
    _apply_002(legacy_db)
    hand = _portfolios_columns(legacy_db)

    orm_engine = create_engine("sqlite://")
    Base.metadata.create_all(orm_engine)
    orm_cols = {c["name"]: c for c in inspect(orm_engine).get_columns("portfolios")}
    orm_engine.dispose()

    assert set(hand) == set(
        orm_cols
    ), f"sql-only={sorted(set(hand) - set(orm_cols))} orm-only={sorted(set(orm_cols) - set(hand))}"
    for name in ("mode", "source"):
        assert hand[name]["nullable"] is False
        assert hand[name]["default"] in ("'paper'", "'synthetic'")


def test_orm_defaults_and_constraints_on_create_all():
    """The ORM alone (create_all) must also enforce mode/source defaults."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        from sqlalchemy.orm import Session

        with Session(engine) as s:
            p = Portfolio(name="ORM Run", initial_capital="100000", current_cash="100000")
            s.add(p)
            s.commit()
            assert p.mode == "paper"
            assert p.source == "synthetic"
            p2 = Portfolio(
                name="Live Run",
                initial_capital="100000",
                current_cash="100000",
                mode="live",
                source="mstock",
            )
            s.add(p2)
            s.commit()
            assert p2.mode == "live"
            assert p2.source == "mstock"
            p3 = Portfolio(name="Bad", initial_capital="1", current_cash="1", mode="bogus")
            s.add(p3)
            with pytest.raises(IntegrityError):
                s.commit()
    finally:
        engine.dispose()


def test_pg_002_file_matches_conventions():
    """PostgreSQL file: idempotent DDL, named constraints, ledger insert."""
    assert PG_002.exists() and PG_001.exists()
    sql = PG_002.read_text()
    assert "ADD COLUMN IF NOT EXISTS mode" in sql
    assert "ADD COLUMN IF NOT EXISTS source" in sql
    assert "ck_portfolios_mode" in sql
    assert "ck_portfolios_source" in sql
    assert "CHECK (mode IN ('paper','live'))" in sql
    assert "CHECK (source IN ('synthetic','replay','mstock'))" in sql
    assert "UPDATE portfolios" in sql  # backfill
    assert "ON CONFLICT (version) DO NOTHING" in sql  # ledger is idempotent
    assert "VALUES ('002'" in sql
