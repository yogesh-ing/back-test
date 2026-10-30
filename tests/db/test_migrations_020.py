"""Migration 020 — ``portfolios.source`` CHECK admits ``upstox``.

The direct successor to ``test_migrations_016.py``: 020 is the same unlock
one venue later. It runs on file-backed SQLite (``tmp_path``) so the
hand-written migration files are executed **verbatim** with
``executescript()`` — the sequence a developer applies locally:
``001`` → ``002`` → ``008`` → ``016`` → ``020``.

Naming note (2026-10-30, BUG-38): the *Alembic* revision for this unlock was
renumbered ``020`` → ``021``, because two files declared ``revision = "020"``
on the same parent and the chain had two heads. The hand-written SQL keeps its
``020`` key — it records into a different ledger (``schema_migrations``), whose
rows already exist in deployed databases, so renaming it would be a data
migration for nothing. Everything below that says ``020`` means the SQL file;
the two Alembic checks at the end name ``021``.

The intermediate ``016`` step matters for the same reason 008 did in that
test: the 020 rebuild carries every column and constraint accumulated so far
(``mode``/``source`` from 002, ``segment``/``execution_broker`` from 008,
the widened CHECK from 016). A DB that skipped 016 in the test could not
prove the 020 rebuild kept 016's work.

Covered:

* ``source='upstox'`` is rejected BEFORE 020 and accepted AFTER it;
* ``source='dhan'`` still works after 020 (the previous unlock is not lost);
* junk sources are still rejected;
* every pre-existing row survives the table rebuild verbatim;
* the ``updated_at`` trigger and the status index survive the rebuild;
* the rebuilt schema still matches the ORM models column-for-column;
* the ``schema_migrations`` ledger records ``020``;
* the Alembic revision mirrors the SQL (textual, per ``test_migrations_002``);
* the Alembic chain has ONE head and no revision id is declared twice
  (``test_alembic_has_single_linear_head`` — the BUG-38 guardrail);
* running revision ``020`` on a hand-applied ledger is a no-op, which is the
  shape live ``forward_test`` is in (stamped ``019``, table already present);
* the PG file follows the conventions 016's file follows.

PostgreSQL-specific behaviour (``DROP CONSTRAINT``, table-level CHECKs,
COMMENTs) cannot be exercised on SQLite; the PG file is verified textually,
matching the repo convention in ``tests/test_db_schema.py``.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from backtest.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
ALEMBIC_VERSIONS = REPO_ROOT / "db" / "alembic" / "versions"
SQL_001 = MIGRATIONS / "001_initial_schema.sqlite.sql"
SQL_002 = MIGRATIONS / "002_add_mode_source.sqlite.sql"
SQL_008 = MIGRATIONS / "008_multi_broker_segments.sqlite.sql"
SQL_016 = MIGRATIONS / "016_add_dhan_source.sqlite.sql"
SQL_020 = MIGRATIONS / "020_add_upstox_source.sqlite.sql"
PG_020 = MIGRATIONS / "020_add_upstox_source.sql"
ALEMBIC_021 = (
    ALEMBIC_VERSIONS / "20261013_1200_021_add_upstox_source_to_portfolios.py"
)

POPULATED_ID = "11111111-1111-1111-1111-111111111111"


def _apply(conn: sqlite3.Connection, *files: Path) -> None:
    for path in files:
        assert path.exists(), f"missing migration file: {path}"
        conn.executescript(path.read_text())


@pytest.fixture()
def pre20_db(tmp_path: Path) -> Path:
    """A pre-020 deployment: 001+002+008+016 applied, two legacy rows present."""
    db = tmp_path / "pre20.db"
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_001, SQL_002, SQL_008, SQL_016)
        conn.execute(
            "INSERT INTO portfolios "
            "(portfolio_id, name, initial_capital, current_cash, mode, source, "
            " segment, execution_broker) "
            "VALUES (?, 'Legacy mStock Run', 100000, 100000, 'paper', 'mstock', "
            "        'options_index', '')",
            (POPULATED_ID,),
        )
        conn.execute(
            "INSERT INTO portfolios "
            "(portfolio_id, name, initial_capital, current_cash, mode, source) "
            "VALUES ('22222222-2222-2222-2222-222222222222', 'Dhan Run', "
            "        200000, 200000, 'paper', 'dhan')"
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _apply_020(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        _apply(conn, SQL_020)
    finally:
        conn.close()


def _connect(db: Path):
    return create_engine(f"sqlite:///{db}")


def _insert_source(db: Path, portfolio_id: str, name: str, source: str) -> None:
    engine = _connect(db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO portfolios "
                    "(portfolio_id, name, initial_capital, current_cash, mode, source) "
                    "VALUES (:pid, :name, 1000, 1000, 'paper', :source)"
                ),
                {"pid": portfolio_id, "name": name, "source": source},
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The unlock: upstox rows land, pre-020 they cannot
# ---------------------------------------------------------------------------


def test_upstox_source_rejected_before_020(pre20_db: Path):
    engine = _connect(pre20_db)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios "
                        "(portfolio_id, name, initial_capital, current_cash, source) "
                        "VALUES ('33333333-3333-3333-3333-333333333333', 'Upstox Run', "
                        "1000, 1000, 'upstox')"
                    )
                )
    finally:
        engine.dispose()


def test_upstox_source_accepted_after_020(pre20_db: Path):
    _apply_020(pre20_db)
    _insert_source(
        pre20_db, "33333333-3333-3333-3333-333333333333", "Upstox Run", "upstox"
    )


def test_dhan_still_accepted_after_020(pre20_db: Path):
    """The 016 unlock must survive the 020 rebuild, not be replaced by it."""
    _apply_020(pre20_db)
    _insert_source(
        pre20_db, "44444444-4444-4444-4444-444444444444", "Another Dhan Run", "dhan"
    )


def test_synthetic_and_replay_still_accepted_after_020(pre20_db: Path):
    _apply_020(pre20_db)
    for i, source in enumerate(("synthetic", "replay")):
        _insert_source(
            pre20_db,
            f"55555555-5555-5555-5555-55555555555{i}",
            f"Run {source}",
            source,
        )


@pytest.mark.parametrize("bad_source", ["foo", "zerodha", "UPSTOX", ""])
def test_check_still_rejects_junk_after_020(pre20_db: Path, bad_source: str):
    _apply_020(pre20_db)
    engine = _connect(pre20_db)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios "
                        "(portfolio_id, name, initial_capital, current_cash, source) "
                        "VALUES ('66666666-6666-6666-6666-666666666666', :name, "
                        "1000, 1000, :source)"
                    ),
                    {"name": f"Bad {bad_source!r}", "source": bad_source},
                )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The rebuild keeps the data and the furniture
# ---------------------------------------------------------------------------


def test_rebuild_preserves_rows_and_columns(pre20_db: Path):
    _apply_020(pre20_db)
    conn = sqlite3.connect(pre20_db)
    try:
        rows = conn.execute(
            "SELECT name, mode, source, segment, execution_broker "
            "FROM portfolios ORDER BY name"
        ).fetchall()
        assert rows == [
            ("Dhan Run", "paper", "dhan", "", ""),
            ("Legacy mStock Run", "paper", "mstock", "options_index", ""),
        ]
    finally:
        conn.close()


def test_updated_at_trigger_survives_rebuild(pre20_db: Path):
    _apply_020(pre20_db)
    conn = sqlite3.connect(pre20_db)
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


def test_status_index_survives_rebuild(pre20_db: Path):
    _apply_020(pre20_db)
    engine = _connect(pre20_db)
    try:
        names = {ix["name"] for ix in inspect(engine).get_indexes("portfolios")}
        assert "ix_portfolios_status" in names
    finally:
        engine.dispose()


def test_ledger_records_020(pre20_db: Path):
    _apply_020(pre20_db)
    conn = sqlite3.connect(pre20_db)
    try:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = '020'"
        ).fetchone()
        assert n == 1
    finally:
        conn.close()


def test_rebuilt_schema_matches_orm(pre20_db: Path):
    """The hand-written 001+002+008+016+020 schema matches the ORM columns."""
    _apply_020(pre20_db)
    hand_engine = _connect(pre20_db)
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


def test_orm_model_admits_upstox():
    """``PortfolioSource.UPSTOX`` is the value the widening legitimises."""
    from backtest.db.models import PortfolioSource

    assert PortfolioSource.UPSTOX.value == "upstox"
    # The enum and the CHECK must name the same set, or a runner can build a
    # value the database will reject at flush time.
    assert {m.value for m in PortfolioSource} >= {
        "synthetic", "replay", "mstock", "dhan", "upstox",
    }


# ---------------------------------------------------------------------------
# PG + Alembic file conventions (textual — same repo rule as test_migrations_002)
# ---------------------------------------------------------------------------


def test_pg_020_file_matches_conventions():
    assert PG_020.exists()
    sql = PG_020.read_text()
    assert "DROP CONSTRAINT IF EXISTS ck_portfolios_source" in sql
    assert "CHECK (source IN ('synthetic','replay','mstock','dhan','upstox'))" in sql
    assert "ON CONFLICT (version) DO NOTHING" in sql  # ledger is idempotent
    assert "VALUES ('020'" in sql


def test_alembic_021_mirrors_the_sql():
    """The Alembic revision widens exactly the same constraint."""
    assert ALEMBIC_021.exists(), f"missing {ALEMBIC_021}"
    src = ALEMBIC_021.read_text(encoding="utf-8")
    assert 'revision: str = "021"' in src
    assert 'down_revision: Union[str, None] = "020"' in src
    assert "'synthetic','replay','mstock','dhan','upstox'" in src  # widened
    assert 'def downgrade()' in src
    # Downgrade restores the 016-era CHECK (drops upstox only).
    assert "'synthetic','replay','mstock','dhan')" in src


def test_alembic_has_single_linear_head():
    """CI guardrail (BUG-38): exactly one head, and no revision id declared twice.

    Two files declared ``revision = "020"`` on the same parent for three weeks.
    ``alembic upgrade`` could not resolve the chain, ``current`` sat at ``019``,
    and every later schema fix had nowhere to chain — while every test of the
    hand-written SQL files stayed green, because the fork existed only in the
    Alembic namespace. This is the check that makes two branches landing the same
    revision number fail loudly instead of silently.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    # Not pinned to an id on purpose: the head moves with every revision, and a
    # guard that has to be edited to stay green is the one that gets edited
    # wrongly. ``test_alembic_chain_009_to_022`` owns the pinned head.
    assert len(script.get_heads()) == 1, (
        f"the alembic chain must have exactly one head, got {script.get_heads()}"
    )

    declared: dict[str, str] = {}
    for path in sorted(ALEMBIC_VERSIONS.glob("*.py")):
        # 001 quotes with '', the rest with "" — both spellings are the same id.
        match = re.search(
            r"""^revision: str = ['"]([^'"]+)['"]""",
            path.read_text(encoding="utf-8"),
            re.M,
        )
        assert match, f"{path.name} declares no revision id"
        assert match.group(1) not in declared, (
            f"{path.name} and {declared[match.group(1)]} both declare "
            f'revision "{match.group(1)}"'
        )
        declared[match.group(1)] = path.name


# ---------------------------------------------------------------------------
# Upgrading a hand-applied ledger (the live `forward_test` shape)
# ---------------------------------------------------------------------------

JOURNAL_REVISION = "20261008_1200_020_option_intent_journal.py"


def _load_revision(filename: str):
    """Import one Alembic file as a module so its ``upgrade()`` can be driven."""
    path = ALEMBIC_VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    """Stands in for ``alembic.op``: records the DDL the revision would emit."""

    def __init__(self, bind) -> None:
        self._bind = bind
        self.calls: list[str] = []

    def get_bind(self):
        return self._bind

    def create_table(self, name, *a, **k):
        self.calls.append(f"create_table:{name}")

    def create_index(self, name, *a, **k):
        self.calls.append(f"create_index:{name}")

    def drop_index(self, name, *a, **k):
        self.calls.append(f"drop_index:{name}")

    def drop_table(self, name, *a, **k):
        self.calls.append(f"drop_table:{name}")

    def execute(self, statement, *a, **k):
        self.calls.append(f"execute:{str(statement)[:40]}")


def _bind_with_journal(present: bool):
    """A scratch SQLite bind with or without ``option_intent_events``."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        if present:
            conn.execute(text("CREATE TABLE option_intent_events (event_id INTEGER)"))
    return engine


def test_journal_revision_skips_a_hand_applied_ledger():
    """``alembic upgrade 019:021`` must be safe on live, which already has the table.

    ``forward_test`` is stamped ``019`` yet physically carries
    ``option_intent_events`` and its three indexes — the SQL-file path applied
    them (probed 2026-10-30). Without the skip, ``upgrade`` would die on
    ``CREATE TABLE`` and the widening this chain exists to deliver would never
    run. So this revision must be a no-op when the objects are already there,
    and must still create them when they are not.
    """
    module = _load_revision(JOURNAL_REVISION)

    stub = _RecordingOp(_bind_with_journal(present=True))
    module.op = stub
    module.upgrade()
    assert stub.calls == [], f"would abort a hand-applied upgrade: {stub.calls}"

    stub = _RecordingOp(_bind_with_journal(present=False))
    module.op = stub
    module.upgrade()
    assert stub.calls == [
        "create_table:option_intent_events",
        "create_index:ix_opt_intent_events_dedupe",
        "create_index:ix_opt_intent_events_intent",
        "create_index:ix_opt_intent_events_recorded",
    ]

    # The mirror case: a downgrade from an absent table must not raise either.
    stub.calls.clear()
    module.downgrade()
    assert stub.calls == []

    stub = _RecordingOp(_bind_with_journal(present=True))
    module.op = stub
    module.downgrade()
    assert stub.calls == [
        "drop_index:ix_opt_intent_events_recorded",
        "drop_index:ix_opt_intent_events_intent",
        "drop_index:ix_opt_intent_events_dedupe",
        "drop_table:option_intent_events",
    ]
