"""Migration 016 — ``portfolios.source`` CHECK admits ``dhan``.

Runs entirely on file-backed SQLite (``tmp_path``) so the hand-written
migration files are executed **verbatim** with ``executescript()`` — the
same sequence a developer applies locally: ``001`` → ``002`` → ``008``,
then ``016`` (the 008 step matters: the 016 rebuild carries the columns
008 added — ``segment`` / ``execution_broker`` — so a DB that skipped it
in the test would hide a dropped-column bug).

Covered:

* the migration applies cleanly and preserves every pre-existing row
  (all old source values stay valid under the widened CHECK);
* ``source='dhan'`` is accepted AFTER 016 and rejected BEFORE it;
* junk sources are still rejected;
* the ``updated_at`` trigger and the status index survive the rebuild;
* the ``schema_migrations`` ledger records ``016``;
* the rebuilt schema still matches the ORM models column-for-column.

PostgreSQL-specific behaviour (``DROP CONSTRAINT``, table-level CHECKs,
COMMENTs) cannot be exercised on SQLite; the PG file is verified
textually, matching the repo convention in ``tests/test_db_schema.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from backtest.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
SQL_001 = MIGRATIONS / "001_initial_schema.sqlite.sql"
SQL_002 = MIGRATIONS / "002_add_mode_source.sqlite.sql"
SQL_008 = MIGRATIONS / "008_multi_broker_segments.sqlite.sql"
SQL_016 = MIGRATIONS / "016_add_dhan_source.sqlite.sql"
PG_016 = MIGRATIONS / "016_add_dhan_source.sql"

POPULATED_ID = "11111111-1111-1111-1111-111111111111"


def _apply(conn: sqlite3.Connection, *files: Path) -> None:
    for path in files:
        assert path.exists(), f"missing migration file: {path}"
        conn.executescript(path.read_text())


@pytest.fixture()
def pre16_db(tmp_path: Path) -> Path:
    """A pre-016 deployment: 001+002+008 applied, one mstock row present."""
    db = tmp_path / "pre16.db"
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_001, SQL_002, SQL_008)
        conn.execute(
            "INSERT INTO portfolios "
            "(portfolio_id, name, initial_capital, current_cash, mode, source, "
            " segment, execution_broker) "
            "VALUES (?, 'Legacy mStock Run', 100000, 100000, 'paper', 'mstock', "
            "        'options_index', '')",
            (POPULATED_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _apply_016(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_016)
    finally:
        conn.close()


def _connect(db: Path):
    return create_engine(f"sqlite:///{db}")


# ---------------------------------------------------------------------------
# The unlock: dhan rows land, pre-016 they cannot
# ---------------------------------------------------------------------------


def test_dhan_source_rejected_before_016(pre16_db: Path):
    engine = _connect(pre16_db)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios "
                        "(portfolio_id, name, initial_capital, current_cash, source) "
                        "VALUES ('22222222-2222-2222-2222-222222222222', 'Dhan Run', "
                        "1000, 1000, 'dhan')"
                    )
                )
    finally:
        engine.dispose()


def test_dhan_source_accepted_after_016(pre16_db: Path):
    _apply_016(pre16_db)
    engine = _connect(pre16_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO portfolios "
                    "(portfolio_id, name, initial_capital, current_cash, mode, source) "
                    "VALUES ('22222222-2222-2222-2222-222222222222', 'Dhan Run', "
                    "1000, 1000, 'paper', 'dhan')"
                )
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad_source", ["foo", "zerodha", "DHAN", ""])
def test_check_still_rejects_junk_after_016(pre16_db: Path, bad_source: str):
    _apply_016(pre16_db)
    engine = _connect(pre16_db)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios "
                        "(portfolio_id, name, initial_capital, current_cash, source) "
                        "VALUES ('33333333-3333-3333-3333-333333333333', :name, "
                        "1000, 1000, :source)"
                    ),
                    {"name": f"Bad {bad_source!r}", "source": bad_source},
                )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The rebuild keeps the data and the furniture
# ---------------------------------------------------------------------------


def test_rebuild_preserves_rows_and_columns(pre16_db: Path):
    _apply_016(pre16_db)
    conn = sqlite3.connect(pre16_db)
    try:
        row = conn.execute(
            "SELECT name, mode, source, segment, execution_broker "
            "FROM portfolios WHERE portfolio_id = ?",
            (POPULATED_ID,),
        ).fetchone()
        assert row == ("Legacy mStock Run", "paper", "mstock", "options_index", "")
        (total,) = conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()
        assert total == 1  # in-place swap, no row duplication
    finally:
        conn.close()


def test_updated_at_trigger_survives_rebuild(pre16_db: Path):
    _apply_016(pre16_db)
    conn = sqlite3.connect(pre16_db)
    try:
        # Sentinel row with a deliberately ancient updated_at (an INSERT does
        # not fire the AFTER UPDATE trigger) — the trigger's rewrite is then
        # observable no matter when the test runs (datetime('now') has
        # 1-second resolution, so comparing two same-second reads flakes).
        probe_id = "99999999-9999-9999-9999-999999999999"
        conn.execute(
            "INSERT INTO portfolios (portfolio_id, name, initial_capital, current_cash, "
            "updated_at) VALUES (?, 'Trigger Probe', 1000, 1000, '2000-01-01 00:00:00')",
            (probe_id,),
        )
        conn.commit()
        conn.execute(
            "UPDATE portfolios SET current_cash = current_cash + 1 WHERE portfolio_id = ?",
            (probe_id,),
        )
        conn.commit()
        (after,) = conn.execute(
            "SELECT updated_at FROM portfolios WHERE portfolio_id = ?", (probe_id,)
        ).fetchone()
        assert after != "2000-01-01 00:00:00", "the trg_portfolios_updated_at trigger was lost"
    finally:
        conn.close()


def test_status_index_survives_rebuild(pre16_db: Path):
    _apply_016(pre16_db)
    engine = _connect(pre16_db)
    try:
        names = {ix["name"] for ix in inspect(engine).get_indexes("portfolios")}
        assert "ix_portfolios_status" in names
    finally:
        engine.dispose()


def test_ledger_records_016(pre16_db: Path):
    _apply_016(pre16_db)
    conn = sqlite3.connect(pre16_db)
    try:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = '016'"
        ).fetchone()
        assert n == 1
    finally:
        conn.close()


def test_rebuilt_schema_matches_orm(pre16_db: Path):
    """The hand-written 001+002+008+016 schema matches the ORM columns."""
    _apply_016(pre16_db)
    hand_engine = _connect(pre16_db)
    try:
        hand_cols = {c["name"] for c in inspect(hand_engine).get_columns("portfolios")}
    finally:
        hand_engine.dispose()

    orm_engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(orm_engine)
        orm_cols = {c["name"] for c in inspect(orm_engine).get_columns("portfolios")}
    finally:
        orm_engine.dispose()

    assert hand_cols == orm_cols


# ---------------------------------------------------------------------------
# PG file conventions (textual — same repo rule as test_migrations_002)
# ---------------------------------------------------------------------------


def test_pg_016_file_matches_conventions():
    assert PG_016.exists()
    sql = PG_016.read_text()
    assert "DROP CONSTRAINT IF EXISTS ck_portfolios_source" in sql
    assert "CHECK (source IN ('synthetic','replay','mstock','dhan'))" in sql
    assert "ON CONFLICT (version) DO NOTHING" in sql  # ledger is idempotent
    assert "VALUES ('016'" in sql
