"""Migration 003 — canonical timeframe naming for ``market_data_cache`` (P4.3).

Runs entirely on file-backed SQLite (``tmp_path``) so the hand-written
migration files are executed **verbatim** with ``executescript()`` — the
same sequence a developer applies locally: 001, 002, then
``003_canonical_timeframes.sqlite.sql``.

Covered (ticket acceptance criteria):

* the migration applies cleanly on a DB whose cache holds the OLD
  vocabulary (60min / day / week / 3min / 1min);
* exact-equivalent rows are remapped (60min→1hour, day→1day, week→1week);
* rows in timeframes with no canonical equivalent (3min/30min/month) are
  deleted — the table is a re-ingestable cache, not a ledger;
* the rebuilt CHECK accepts the full canonical set (incl. ``4hour``) and
  rejects the old names;
* the ``schema_migrations`` ledger records ``003``;
* the PostgreSQL file carries the same remap + canonical CHECK (verified
  textually, matching the repo convention).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
SQL_001 = MIGRATIONS / "001_initial_schema.sqlite.sql"
SQL_002 = MIGRATIONS / "002_add_mode_source.sqlite.sql"
SQL_003 = MIGRATIONS / "003_canonical_timeframes.sqlite.sql"
PG_003 = MIGRATIONS / "003_canonical_timeframes.sql"

CANONICAL = ("1min", "5min", "15min", "1hour", "4hour", "1day", "1week")


def _apply(conn: sqlite3.Connection, *files: Path) -> None:
    for path in files:
        assert path.exists(), f"missing migration file: {path}"
        conn.executescript(path.read_text())


def _insert_bars(conn: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
    """rows: (timeframe, ts) — one bar per timeframe under symbol 'DEMO'."""
    for tf, ts in rows:
        conn.execute(
            "INSERT INTO market_data_cache "
            "(symbol, exchange, timeframe, ts, open, high, low, close, volume) "
            "VALUES ('DEMO', 'NSE', ?, ?, 100, 101, 99, 100.5, 1000)",
            (tf, ts),
        )


@pytest.fixture()
def old_vocab_db(tmp_path: Path) -> Path:
    """001+002 applied, cache populated with the PRE-003 vocabulary."""
    db = tmp_path / "old_vocab.db"
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_001, SQL_002)
        _insert_bars(conn, [
            ("1min", "2024-01-02 09:15:00"),
            ("5min", "2024-01-02 09:15:00"),
            ("3min", "2024-01-02 09:15:00"),     # no canonical equivalent
            ("60min", "2024-01-02 09:15:00"),    # -> 1hour
            ("day", "2024-01-02 09:15:00"),      # -> 1day
            ("week", "2024-01-01 09:15:00"),     # -> 1week
            ("month", "2024-01-01 09:15:00"),    # no canonical equivalent
        ])
        conn.commit()
    finally:
        conn.close()
    return db


def test_migration_remaps_rows_and_rebuilds_check(old_vocab_db: Path) -> None:
    conn = sqlite3.connect(old_vocab_db)
    try:
        _apply(conn, SQL_003)
        tf = [r[0] for r in conn.execute(
            "SELECT timeframe FROM market_data_cache ORDER BY ts, timeframe"
        )]
        # 3min + month dropped; 60min/day/week remapped; 1min/5min untouched
        assert sorted(tf) == ["1day", "1hour", "1min", "1week", "5min"]
        # the ledger records 003
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert "003" in versions
    finally:
        conn.close()


def test_rebuilt_check_enforces_canonical_set(old_vocab_db: Path) -> None:
    conn = sqlite3.connect(old_vocab_db)
    try:
        _apply(conn, SQL_003)
        # every canonical value must be insertable (one per ts)
        for i, name in enumerate(CANONICAL):
            conn.execute(
                "INSERT INTO market_data_cache "
                "(symbol, exchange, timeframe, ts, open, high, low, close, volume) "
                "VALUES ('DEMO', 'NSE', ?, ?, 100, 101, 99, 100.5, 10)",
                (name, f"2024-02-0{i + 1} 09:15:00"),
            )
    finally:
        conn.close()

    engine = create_engine(f"sqlite:///{old_vocab_db}")
    with engine.begin() as sa_conn:
        for i, bad in enumerate(["60min", "30min", "day", "week", "1D", "3min"]):
            with pytest.raises(IntegrityError):
                sa_conn.execute(
                    text(
                        "INSERT INTO market_data_cache "
                        "(symbol, exchange, timeframe, ts, open, high, low, close, volume) "
                        "VALUES ('DEMO', 'NSE', :tf, :ts, 100, 101, 99, 100.5, 10)"
                    ),
                    {"tf": bad, "ts": f"2024-03-0{i + 1} 09:15:00"},
                )
    engine.dispose()


def test_pg_file_carrys_canonical_set_and_remap() -> None:
    assert PG_003.exists(), "missing PostgreSQL migration 003"
    sql = PG_003.read_text()
    for name in CANONICAL:
        assert f"'{name}'" in sql, f"canonical {name} missing from PG CHECK"
    assert "SET timeframe = '1hour' WHERE timeframe = '60min'" in sql
    assert "SET timeframe = '1day'  WHERE timeframe = 'day'" in sql
    assert "SET timeframe = '1week' WHERE timeframe = 'week'" in sql
    assert "DELETE FROM market_data_cache WHERE timeframe IN ('3min', '30min', 'month')" in sql
    assert "DROP CONSTRAINT IF EXISTS ck_mdc_timeframe" in sql
