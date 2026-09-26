"""Migration 005 — Portfolio Intelligence tables.

The hand-written SQLite files 001→005 are executed **verbatim** with
``executescript()`` on a file-backed DB (repo convention); the PostgreSQL
file is checked textually. The ORM models must round-trip against the
migrated schema, i.e. the migration and ``backtest.db.models`` agree.
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
]
PG_005 = MIGRATIONS / "005_portfolio_intelligence.sql"
TABLES = ("alerts", "portfolio_greeks_history", "market_regime_history")


@pytest.fixture()
def migrated(tmp_path: Path) -> Path:
    db = tmp_path / "m005.db"
    conn = sqlite3.connect(db)
    for path in SQLITE_FILES:
        assert path.exists(), path
        conn.executescript(path.read_text())
    conn.commit()
    conn.close()
    return db


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_tables_indexes_and_ledger(migrated):
    conn = sqlite3.connect(migrated)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names
    assert {"alert_id", "alert_type", "alert_key", "severity", "message", "data", "created_at",
            "resolved_at", "dismissed_at", "dismissed_by", "reviewed_at",
            "notified_strategies"} <= _columns(conn, "alerts")
    assert {"net_delta", "net_gamma", "net_vega", "net_theta", "greeks_by_strategy",
            "concentration_by_underlying", "concentration_by_strike"} <= _columns(
        conn, "portfolio_greeks_history")
    assert {"regime", "vix", "realized_vol", "source", "previous_regime",
            "regime_changed"} <= _columns(conn, "market_regime_history")
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    expected = {"idx_alerts_type", "idx_alerts_created", "idx_greeks_time", "idx_regime_time"}
    assert expected <= indexes
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert "005" in versions


def test_severity_check_and_idempotence(migrated):
    conn = sqlite3.connect(migrated)
    conn.execute(
        "INSERT INTO alerts (alert_id, alert_type, alert_key, severity, message) "
        "VALUES ('a1', 'portfolio_gamma_critical', 'portfolio_gamma_critical', 'critical', 'm')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO alerts (alert_id, alert_type, alert_key, severity, message) "
            "VALUES ('a2', 'x', 'x', 'catastrophic', 'm')"
        )
    # Re-applying 005 is harmless (IF NOT EXISTS / INSERT OR IGNORE).
    conn.executescript(SQLITE_FILES[-1].read_text())
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_orm_models_round_trip_on_migrated_schema(migrated):
    from backtest.db import DatabaseManager
    from backtest.db.models import MarketRegimeSnapshot, PortfolioAlert, PortfolioGreeksSnapshot

    db = DatabaseManager.from_env(url=f"sqlite:///{migrated}")
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with db.session() as s:
        s.add(PortfolioAlert(alert_id="x1", alert_type="portfolio_delta_warning",
                             alert_key="portfolio_delta_warning", severity="warning",
                             message="m", data={"net_delta": 900}, created_at=now))
        s.add(PortfolioGreeksSnapshot(snapshot_id="g1", timestamp=now, net_delta=1, net_gamma=-2,
                                      net_vega=-3, net_theta=4, greeks_by_strategy=[]))
        s.add(MarketRegimeSnapshot(regime_id="r1", timestamp=now, regime="low_vol", vix=13.2,
                                   source="manual", regime_changed=False))
    with db.session() as s:
        row = s.get(PortfolioAlert, "x1")
        assert row.data == {"net_delta": 900}
        assert s.get(MarketRegimeSnapshot, "r1").regime == "low_vol"


def test_postgres_file_matches():
    sql = PG_005.read_text()
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "JSONB" in sql
    assert "'critical','warning','info'" in sql.replace(", ", ",")
    assert "'005'" in sql
