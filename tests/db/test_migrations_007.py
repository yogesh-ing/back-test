"""Migration 007 — segments + global live kill-switch (settings panel Phase 2).

Same convention as test_migrations_005: the hand-written SQLite files are
executed **verbatim** with ``executescript()`` on a file-backed DB; the ORM
models must round-trip against the migrated schema. The PostgreSQL file is
checked textually.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
SQLITE_FILES = [
    MIGRATIONS / "001_initial_schema.sqlite.sql",
    MIGRATIONS / "002_add_mode_source.sqlite.sql",
    MIGRATIONS / "003_canonical_timeframes.sqlite.sql",
    MIGRATIONS / "004_add_trade_structures.sqlite.sql",
    MIGRATIONS / "005_portfolio_intelligence.sqlite.sql",
    MIGRATIONS / "006_broker_profiles.sqlite.sql",
    MIGRATIONS / "007_segments_kill_switch.sqlite.sql",
]
PG_007 = MIGRATIONS / "007_segments_kill_switch.sql"


@pytest.fixture()
def migrated(tmp_path: Path) -> Path:
    db = tmp_path / "m007.db"
    conn = sqlite3.connect(db)
    for path in SQLITE_FILES:
        assert path.exists(), path
        conn.executescript(path.read_text())
    conn.commit()
    conn.close()
    return db


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_tables_columns_and_version(migrated):
    conn = sqlite3.connect(migrated)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"segments", "segment_audit"} <= names
    assert {
        "segment_id", "segment_name", "mode", "allocated_capital",
        "broker_profile_id", "risk_limits", "created_at", "updated_at",
    } <= _columns(conn, "segments")
    assert {
        "audit_id", "segment_id", "field_changed", "old_value", "new_value",
        "changed_by", "changed_at",
    } <= _columns(conn, "segment_audit")
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_segment_audit_segment" in indexes
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert "007" in versions


def test_kill_switch_sentinel_seeded_default_off_and_idempotent(migrated):
    """The switch exists from migration time and defaults to OFF (fail closed)."""
    conn = sqlite3.connect(migrated)
    row = conn.execute(
        "SELECT commission_model FROM broker_profiles WHERE profile_id = '__live_kill_switch__'"
    ).fetchone()
    assert row is not None
    assert '"enabled": false' in row[0]
    # Re-applying 007 is harmless.
    conn.executescript(SQLITE_FILES[-1].read_text())
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM broker_profiles WHERE profile_id = '__live_kill_switch__'"
        ).fetchone()[0]
        == 1
    )


def test_orm_models_round_trip_on_migrated_schema(migrated):
    from backtest.db import DatabaseManager
    from backtest.db.models import SegmentAudit, SegmentRow

    db = DatabaseManager.from_env(url=f"sqlite:///{migrated}")
    with db.session() as s:
        s.add(
            SegmentRow(
                segment_id="eq",
                segment_name="Equity Intraday",
                mode="paper",
                allocated_capital=250000,
                broker_profile_id="mstock",
                risk_limits={"daily_loss_limit": 5000, "max_positions": 4},
            )
        )
        s.add(SegmentAudit(segment_id="eq", field_changed="mode", old_value="paper", new_value="live"))
    with db.session() as s:
        row = s.get(SegmentRow, "eq")
        assert row.to_dict()["risk_limits"]["daily_loss_limit"] == 5000.0
        assert row.to_dict()["allocated_capital"] == 250000.0
        assert s.get(SegmentAudit, 1).field_changed == "mode"


def test_postgres_file_matches():
    sql = PG_007.read_text()
    for table in ("segments", "segment_audit"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "JSONB" in sql
    assert "__live_kill_switch__" in sql
    assert "'enabled': false" in sql or '"enabled": false' in sql
    assert "'007'" in sql
