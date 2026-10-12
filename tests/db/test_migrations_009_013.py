"""Migrations 009–015 — the parameter optimization engine schema.

* SQLite: the hand-written mirror (``009_013_optimization_engine.sqlite.sql``)
  is executed **verbatim** after 001–004 and must agree with the ORM (tables,
  columns, index names), be idempotent, seed the three default presets and
  enforce the FK / CHECK behaviour the engine relies on.
* Alembic: 009→015 chain onto 008 with a single head.
* PostgreSQL files: verified textually (views, trigger, seed, rollback order).
* Optional live PostgreSQL round trip: set ``OPTIMIZATION_TEST_PG_URL`` to a
  server URL with CREATEDB rights; the test creates and drops its own
  throwaway database (it never touches the database named in the URL).
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from backtest.db.models import OPTIMIZATION_TABLES, Base

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
SQLITE_CHAIN = [
    MIGRATIONS / "001_initial_schema.sqlite.sql",
    MIGRATIONS / "002_add_mode_source.sqlite.sql",
    MIGRATIONS / "003_canonical_timeframes.sqlite.sql",
    MIGRATIONS / "004_add_trade_structures.sqlite.sql",
]
SQLITE_OPT = MIGRATIONS / "009_013_optimization_engine.sqlite.sql"
#: 018 later touched parameter_presets (backtest_run_id lineage column) —
#: the fixture applies it after the optimization mirror so the ORM and the
#: hand-built schema describe the same table.
SQLITE_LEDGER = MIGRATIONS / "018_backtest_run_ledger.sqlite.sql"
PG_FILES = {
    "009": MIGRATIONS / "009_optimization_core.sql",
    "010": MIGRATIONS / "010_optimization_audit_presets.sql",
    "011": MIGRATIONS / "011_optimization_indexes.sql",
    "012": MIGRATIONS / "012_optimization_views.sql",
    "013": MIGRATIONS / "013_optimization_seed_presets.sql",
    "014": MIGRATIONS / "014_optimization_deflated_sharpe.sql",
    "015": MIGRATIONS / "015_optimization_data_attestation.sql",
}
PG_ROLLBACK = MIGRATIONS / "009_013_optimization_rollback.sql"
TABLES = {"optimization_runs", "optimization_results", "parameter_presets", "optimization_audit"}
VIEWS = (
    "v_latest_optimization",
    "v_top_results",
    "v_optimization_summary",
    "v_parameter_history",
    "v_active_presets",
)
SEED_IDS = [f"00000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3)]


@pytest.fixture()
def sqlite_db(tmp_path: Path) -> Path:
    path = tmp_path / "opt.db"
    conn = sqlite3.connect(path)
    for f in [*SQLITE_CHAIN, SQLITE_OPT, SQLITE_LEDGER]:
        assert f.exists(), f
        conn.executescript(f.read_text())
    conn.close()
    return path


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# SQLite mirror
# ---------------------------------------------------------------------------


def test_orm_registry_lists_the_four_tables():
    assert {t.name for t in OPTIMIZATION_TABLES} == TABLES


def test_sqlite_file_matches_orm_columns_and_indexes(sqlite_db: Path):
    hand = inspect(create_engine(f"sqlite:///{sqlite_db}"))
    orm_engine = create_engine("sqlite://")
    Base.metadata.create_all(orm_engine, tables=[Base.metadata.tables[t] for t in TABLES])
    orm = inspect(orm_engine)
    for table in sorted(TABLES):
        hand_cols = {c["name"] for c in hand.get_columns(table)}
        orm_cols = {c["name"] for c in orm.get_columns(table)}
        assert hand_cols == orm_cols, (table, hand_cols ^ orm_cols)
        hand_idx = {i["name"] for i in hand.get_indexes(table)}
        orm_idx = {i["name"] for i in orm.get_indexes(table)}
        assert orm_idx <= hand_idx, (table, orm_idx - hand_idx)


def test_sqlite_file_is_idempotent(sqlite_db: Path):
    conn = _conn(sqlite_db)
    conn.executescript(SQLITE_OPT.read_text())  # second application must not raise
    n = conn.execute(
        "SELECT COUNT(*) FROM parameter_presets WHERE strategy_id='default'"
    ).fetchone()[0]
    conn.close()
    assert n == 3


def test_seeded_presets_and_ledger(sqlite_db: Path):
    conn = _conn(sqlite_db)
    rows = conn.execute(
        "SELECT preset_id, name, source, is_active FROM parameter_presets " "ORDER BY preset_id"
    ).fetchall()
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    conn.close()
    assert [r[0] for r in rows] == SEED_IDS
    assert [r[1] for r in rows] == ["Conservative", "Moderate", "Aggressive"]
    assert {r[2] for r in rows} == {"default"} and all(r[3] == 1 for r in rows)
    assert {"009", "010", "011", "013"} <= versions
    assert "012" not in versions  # analytics views are PostgreSQL-only


def _insert_run(conn: sqlite3.Connection, status: str = "completed") -> str:
    rid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO optimization_runs (run_id, strategy_id, objective_function, method, "
        "param_space, backtest_config, status) VALUES (?, 'sma_crossover', 'sharpe', 'grid', "
        "'[]', '{}', ?)",
        (rid, status),
    )
    return rid


def test_fk_behaviour_cascade_and_set_null(sqlite_db: Path):
    conn = _conn(sqlite_db)
    rid = _insert_run(conn)
    conn.execute(
        "INSERT INTO optimization_results (result_id, run_id, params, "
        "objective_score, constraints_met) VALUES (?, ?, '{}', 1.0, 1)",
        (str(uuid.uuid4()), rid),
    )
    conn.execute(
        "INSERT INTO optimization_audit (audit_id, run_id, strategy_id, action) "
        "VALUES (?, ?, 'sma_crossover', 'apply')",
        (str(uuid.uuid4()), rid),
    )
    conn.execute(
        "INSERT INTO parameter_presets (preset_id, strategy_id, name, params, source, "
        "optimization_run_id) VALUES (?, 'sma_crossover', 'p', '{}', 'optimization', ?)",
        (str(uuid.uuid4()), rid),
    )
    conn.execute("DELETE FROM optimization_runs WHERE run_id = ?", (rid,))
    assert conn.execute("SELECT COUNT(*) FROM optimization_results").fetchone()[0] == 0
    assert conn.execute("SELECT run_id FROM optimization_audit").fetchone()[0] is None
    assert (
        conn.execute(
            "SELECT optimization_run_id FROM parameter_presets " "WHERE name='p'"
        ).fetchone()[0]
        is None
    )
    conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO optimization_runs (run_id, strategy_id, objective_function, method, "
        "param_space, backtest_config, status) VALUES ('x', 's', 'sharpe', 'grid', '[]', '{}', "
        "'bogus')",
        "INSERT INTO optimization_runs (run_id, strategy_id, objective_function, method, "
        "param_space, backtest_config) VALUES ('x', 's', 'luck', 'grid', '[]', '{}')",
        "INSERT INTO optimization_runs (run_id, strategy_id, objective_function, method, "
        "param_space, backtest_config) VALUES ('x', 's', 'sharpe', 'magic', '[]', '{}')",
        "INSERT INTO parameter_presets (preset_id, strategy_id, name, params, source) VALUES "
        "('x', 's', 'n', '{}', 'nowhere')",
    ],
)
def test_check_constraints_reject_unknown_enums(sqlite_db: Path, sql: str):
    conn = _conn(sqlite_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
    conn.close()


def test_store_runs_on_the_hand_migrated_database(sqlite_db: Path):
    """The application layer works against the SQL-file schema, not just create_all."""
    from backtest.db import DatabaseManager
    from backtest.optimization.store import OptimizationStore

    manager = DatabaseManager.from_env(profile="testing", url=f"sqlite:///{sqlite_db}")
    manager.connect()
    store = OptimizationStore(manager)
    store.ensure_schema()  # no-op on an up-to-date schema, must not duplicate the seed
    assert len([p for p in store.list_presets() if p["strategy_id"] == "default"]) == 3
    rid = store.create_run(
        strategy_id="sma_crossover",
        objective="sharpe",
        method="grid",
        param_space=[],
        constraints=[],
        backtest_config={},
        walk_forward_enabled=False,
        walk_forward_config={},
        total_combinations=1,
    )
    assert store.get_run(rid)["status"] == "pending"
    manager.disconnect()


# ---------------------------------------------------------------------------
# Alembic chain
# ---------------------------------------------------------------------------


def test_alembic_chain_009_to_018():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["018"]
    revs = ("009", "010", "011", "012", "013", "014", "015", "016", "017", "018")
    chain = {r: script.get_revision(r).down_revision for r in revs}
    assert chain == {
        "009": "008",
        "010": "009",
        "011": "010",
        "012": "011",
        "013": "012",
        "014": "013",
        "015": "014",
        "016": "015",
        "017": "016",
        "018": "017",
    }


# ---------------------------------------------------------------------------
# PostgreSQL files (textual)
# ---------------------------------------------------------------------------


def test_pg_files_exist_and_record_their_version():
    for version, path in PG_FILES.items():
        sql = path.read_text()
        assert f"'{version}'" in sql, f"{path.name} does not record schema_migrations {version}"
    assert PG_ROLLBACK.exists()


def test_pg_core_uses_jsonb_uuid_timestamptz_and_trigger():
    sql = PG_FILES["009"].read_text()
    assert "JSONB" in sql and "UUID" in sql and "TIMESTAMPTZ" in sql
    assert "gen_random_uuid()" in sql
    assert "set_updated_at()" in sql and "CREATE TRIGGER" in sql.upper()
    assert "ON DELETE CASCADE" in sql


def test_pg_views_and_seed():
    views = PG_FILES["012"].read_text()
    for v in VIEWS:
        assert f"CREATE OR REPLACE VIEW {v}" in views, v
    seed = PG_FILES["013"].read_text()
    for pid in SEED_IDS:
        assert pid in seed
    assert "ON CONFLICT" in seed.upper()


def test_pg_rollback_drops_views_before_tables():
    sql = PG_ROLLBACK.read_text()
    first_table_drop = sql.index("DROP TABLE")
    for v in VIEWS:
        assert sql.index(f"DROP VIEW IF EXISTS {v}") < first_table_drop
    order = [
        sql.index(f"DROP TABLE IF EXISTS {t}")
        for t in (
            "optimization_audit",
            "parameter_presets",
            "optimization_results",
            "optimization_runs",
        )
    ]
    assert order == sorted(order), "children must be dropped before optimization_runs"


# ---------------------------------------------------------------------------
# Live PostgreSQL (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("OPTIMIZATION_TEST_PG_URL"),
    reason="set OPTIMIZATION_TEST_PG_URL to run the PostgreSQL round trip",
)
def test_postgres_alembic_round_trip():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine import make_url

    base = make_url(os.environ["OPTIMIZATION_TEST_PG_URL"])
    scratch = f"opt_mig_{uuid.uuid4().hex[:10]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{scratch}"'))
    url = base.set(database=scratch)
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        # -x db_url wins over FORWARD_TEST_DB_URL in db/alembic/env.py
        cfg.cmd_opts = type(
            "Opts", (), {"x": [f"db_url={url.render_as_string(hide_password=False)}"]}
        )()
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as c:
            names = set(inspect(c).get_table_names())
            assert TABLES <= names
            views = set(inspect(c).get_view_names())
            assert set(VIEWS) <= views
            assert (
                c.execute(
                    text("SELECT COUNT(*) FROM parameter_presets " "WHERE strategy_id='default'")
                ).scalar()
                == 3
            )
            rid = c.execute(
                text(
                    "INSERT INTO optimization_runs (strategy_id, objective_function, method, "
                    "param_space, backtest_config) VALUES ('s', 'sharpe', 'grid', '[]', '{}') "
                    "RETURNING run_id"
                )
            ).scalar()
            assert rid is not None  # gen_random_uuid() server default
            c.commit()
        eng.dispose()
        command.downgrade(cfg, "004")
        eng = create_engine(url)
        with eng.connect() as c:
            assert not (TABLES & set(inspect(c).get_table_names()))
            assert not (set(VIEWS) & set(inspect(c).get_view_names()))
        eng.dispose()
        command.upgrade(cfg, "head")  # and back up again
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()


def test_014_adds_the_deflated_sharpe_column(sqlite_db):
    """PRD Part 2 §3. Additive, nullable, and wide enough for a big search.

    A (4, 2) column would silently round a 123.456 bar to 12.35 — the value
    would still look plausible, which is the failure mode worth guarding.
    """
    info = {
        r["name"]: r
        for r in inspect(create_engine(f"sqlite:///{sqlite_db}")).get_columns("optimization_runs")
    }
    assert "deflated_sharpe" in info
    assert info["deflated_sharpe"]["nullable"] is True

    conn = _conn(sqlite_db)
    rid = _insert_run(conn)
    conn.execute("UPDATE optimization_runs SET deflated_sharpe = 123.456 WHERE run_id = ?", (rid,))
    stored = conn.execute(
        "SELECT deflated_sharpe FROM optimization_runs WHERE run_id = ?", (rid,)
    ).fetchone()[0]
    conn.close()
    assert float(stored) == pytest.approx(123.456, abs=0.001)


def test_014_recreates_the_views_with_the_new_column():
    """CREATE OR REPLACE VIEW cannot change a column list, so 014 replaces the
    two affected views whole. A forgotten view would still work — just
    silently without the new column."""
    sql = PG_FILES["014"].read_text()
    assert "CREATE OR REPLACE VIEW v_latest_optimization" in sql
    assert "CREATE OR REPLACE VIEW v_optimization_summary" in sql
    for view in ("v_latest_optimization", "v_optimization_summary"):
        block = sql.split(f"CREATE OR REPLACE VIEW {view} AS", 1)[1]
        block = block.split(";", 1)[0]
        assert "deflated_sharpe" in block, f"{view} was not updated to carry the column"


def test_015_adds_the_attestation_columns(sqlite_db):
    """PRD Part 2 §2. Additive, nullable, and unbackfilled.

    Every run that predates this migration has no attestation, and that is the
    honest state of it. A backfilled value would be a claim nobody checked, so
    the test pins that the columns are nullable rather than that they are
    populated.
    """
    info = {
        r["name"]: r
        for r in inspect(create_engine(f"sqlite:///{sqlite_db}")).get_columns("optimization_runs")
    }
    for column in (
        "data_source",
        "data_fetch_date",
        "bars_count",
        "symbol",
        "timeframe",
        "date_from",
        "date_to",
        "data_attestation",
    ):
        assert column in info, f"015 must add {column}"
        assert info[column]["nullable"] is True, f"{column} must stay nullable"


def test_015_stores_a_full_attestation_json_record(sqlite_db):
    """The flat columns are for filtering; the JSON record is the thing a page
    reads back, and it has to survive a round trip intact."""
    import json

    conn = _conn(sqlite_db)
    rid = _insert_run(conn)
    record = {
        "data_source": "db",
        "data_source_real": True,
        "bars_count": 1247,
        "date_from": "2020-01-01",
        "date_to": "2024-12-31",
        "stale": True,
        "stale_days": 44,
    }
    conn.execute(
        "UPDATE optimization_runs SET data_attestation = ? WHERE run_id = ?",
        (json.dumps(record), rid),
    )
    stored = conn.execute(
        "SELECT data_attestation FROM optimization_runs WHERE run_id = ?", (rid,)
    ).fetchone()[0]
    conn.close()
    assert json.loads(stored) == record


def test_015_recreates_the_views_with_the_new_columns():
    """CREATE OR REPLACE VIEW cannot change a column list, so 015 replaces the
    two affected views whole. A forgotten view would still work — just silently
    without the new columns."""
    sql = PG_FILES["015"].read_text()
    for view in ("v_latest_optimization", "v_optimization_summary"):
        assert f"CREATE OR REPLACE VIEW {view} AS" in sql
        block = sql.split(f"CREATE OR REPLACE VIEW {view} AS", 1)[1].split(";", 1)[0]
        assert "data_source" in block, f"{view} was not updated to carry the columns"


def test_015_backfills_nothing():
    """A backfilled attestation would be a value nobody measured."""
    sql = PG_FILES["015"].read_text()
    assert "UPDATE optimization_runs" not in sql
    assert "server_default" not in sql
