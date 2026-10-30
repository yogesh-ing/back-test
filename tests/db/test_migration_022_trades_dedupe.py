"""Migration 022 — ``trades.dedupe_key`` + its unique index (B1).

B1 (QA 2026-10-30): every restart re-inserted each runner's closed-trades cache
into ``trades`` — 6,476 rows for 2,878 real round trips, ₹2,23,635.74 of phantom
loss, up to 16 copies of one fill. Revision 022 adds the durable half of the fix.

Unlike 020/021 this is a plain additive ALTER, so it can be executed **verbatim**
against a scratch SQLite ``trades`` table with Alembic's real ``Operations``
object — which is what proves the two properties live needs:

* re-runnable, because a database may have taken the hand-applied SQL path
  already (the fork of BUG-38 taught this repo not to assume the ledger);
* unique for rows that carry a key, and silent for rows that do not — the
  pre-022 duplicates must not make ``upgrade`` fail, they are
  ``scripts/repair_trade_duplicates.py``'s job.

PostgreSQL-only syntax (``ADD COLUMN IF NOT EXISTS``, ``COMMENT ON``) is checked
textually, per the convention in ``test_migrations_020.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
ALEMBIC_VERSIONS = REPO_ROOT / "db" / "alembic" / "versions"
ALEMBIC_022 = ALEMBIC_VERSIONS / "20261031_1200_022_trades_dedupe_key.py"
PG_022 = MIGRATIONS / "022_trades_dedupe_key.sql"
SQLITE_022 = MIGRATIONS / "022_trades_dedupe_key.sqlite.sql"

INDEX = "uq_trades_dedupe_key"

#: The smallest table the revision can act on. Deliberately NOT the ORM model —
#: ``Base.metadata`` already carries ``dedupe_key``, so creating from it would
#: test a database that had already been migrated.
_SEED = (
    "CREATE TABLE trades ("
    " trade_id VARCHAR(36) PRIMARY KEY,"
    " symbol VARCHAR(64) NOT NULL,"
    " net_pnl NUMERIC(20,4) NOT NULL)"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location(ALEMBIC_022.stem, ALEMBIC_022)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "022" and module.down_revision == "021"
    return module


@pytest.fixture()
def trades_db():
    """A connection-level engine with a pre-022 ``trades`` table, as ``op`` sees it."""
    engine = create_engine("sqlite://")
    connection = engine.connect()
    connection.execute(text(_SEED))
    connection.execute(
        text("INSERT INTO trades (trade_id, symbol, net_pnl) VALUES ('legacy-1', 'A', -10)")
    )
    module = _revision_module()
    module.op = Operations(MigrationContext.configure(connection))
    try:
        yield module, connection
    finally:
        connection.close()
        engine.dispose()


def _has_column(connection, name: str) -> bool:
    return {c["name"] for c in inspect(connection).get_columns("trades")} >= {name}


def _has_index(connection, name: str) -> bool:
    return {i["name"] for i in inspect(connection).get_indexes("trades")} >= {name}


class TestUpgrade:
    def test_adds_the_column_and_the_unique_index(self, trades_db):
        module, conn = trades_db
        assert not _has_column(conn, "dedupe_key")
        module.upgrade()
        assert _has_column(conn, "dedupe_key")
        assert _has_index(conn, INDEX)
        # Additive: the pre-022 row is untouched, its key is simply absent.
        stored = conn.execute(text("SELECT symbol, dedupe_key FROM trades")).all()
        assert stored == [("A", None)]

    def test_re_running_is_a_no_op(self, trades_db):
        """A ledger that already took the SQL-file path must still upgrade."""
        module, conn = trades_db
        module.upgrade()
        module.upgrade()
        assert _has_column(conn, "dedupe_key") and _has_index(conn, INDEX)

    def test_downgrade_removes_both_and_is_reversible(self, trades_db):
        module, conn = trades_db
        module.upgrade()
        module.downgrade()
        assert not _has_column(conn, "dedupe_key") and not _has_index(conn, INDEX)
        module.downgrade()  # idempotent in the other direction too
        module.upgrade()
        assert _has_column(conn, "dedupe_key")


class TestUniquenessContract:
    def _insert(self, conn, trade_id: str, key: str | None) -> None:
        conn.execute(
            text("INSERT INTO trades (trade_id, symbol, net_pnl, dedupe_key) "
                 "VALUES (:tid, 'B', 5, :k)"),
            {"tid": trade_id, "k": key},
        )

    def test_the_same_round_trip_cannot_be_written_twice(self, trades_db):
        module, conn = trades_db
        module.upgrade()
        self._insert(conn, "t-1", "k" * 64)
        with pytest.raises(IntegrityError):
            self._insert(conn, "t-2", "k" * 64)

    def test_keyless_history_is_untouched(self, trades_db):
        """NULL never conflicts — that is what lets 022 land before the cleanup.

        The duplicates are still in the table on the day this migrates. Had the
        index been built on a backfilled column, ``upgrade`` would have failed
        on live and the guard would have arrived as an outage.
        """
        module, conn = trades_db
        module.upgrade()
        self._insert(conn, "dup-1", None)
        self._insert(conn, "dup-2", None)
        with Session(conn) as session:
            assert session.execute(
                text("SELECT COUNT(*) FROM trades WHERE dedupe_key IS NULL")
            ).scalar_one() == 3


class TestFilesMirrorEachOther:
    def test_alembic_file_matches_the_sql(self):
        src = ALEMBIC_022.read_text(encoding="utf-8")
        assert 'revision: str = "022"' in src
        assert 'down_revision: Union[str, None] = "021"' in src
        assert "dedupe_key" in src and INDEX in src
        assert "def downgrade()" in src

    def test_pg_file_is_additive_and_idempotent(self):
        sql = PG_022.read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(64)" in sql
        assert f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX}" in sql
        assert "'022'" in sql  # schema_migrations ledger row

    def test_sqlite_mirror_exists(self):
        sql = SQLITE_022.read_text(encoding="utf-8")
        assert "ADD COLUMN dedupe_key VARCHAR(64)" in sql
        assert f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX}" in sql
        assert "'022'" in sql
