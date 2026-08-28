"""Tests for the database connection manager (Step 2).

Runs on SQLite so no external services are needed. Failure modes that are
awkward to provoke for real (dropped connections, transient faults) are
simulated with monkeypatching and a fault-injecting fake.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from backtest.db.config import (
    ENV_PREFIX,
    ConfigError,
    DatabaseConfig,
    load_config,
)
from backtest.db.manager import (
    ConnectionError as DbConnectionError,
    DatabaseManager,
    TransactionError,
)


def _has_psycopg2() -> bool:
    """The two "real Postgres is unreachable" tests need the driver to get as
    far as an actual connection attempt — without it SQLAlchemy fails while
    *building* the engine, and the assertion would be testing the wrong thing.
    """
    try:
        import psycopg2  # noqa: F401
    except Exception:
        return False
    return True


requires_psycopg2 = pytest.mark.skipif(
    not _has_psycopg2(),
    reason="needs the PostgreSQL driver: pip install -r requirements.txt "
           "(psycopg2-binary) — otherwise engine creation fails before any connect",
)
from backtest.db.models import Base, Portfolio

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_ROOT / "config" / "database.yaml"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Stop the developer's real environment leaking into assertions."""
    for key in list(os_environ_keys()):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def os_environ_keys():
    import os

    return list(os.environ)


@pytest.fixture()
def db():
    """A connected in-memory manager with the schema already created."""
    manager = DatabaseManager.from_env(
        path=str(CONFIG_FILE), profile="testing", url="sqlite:///:memory:"
    )
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


@pytest.fixture()
def retrying_db():
    """Like ``db`` but with retries enabled (the testing profile disables them)."""
    manager = DatabaseManager.from_env(
        path=str(CONFIG_FILE),
        profile="testing",
        url="sqlite:///:memory:",
        retry_attempts=3,
        retry_base_delay=0.001,
        retry_max_delay=0.002,
    )
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


def _add_portfolio(db, name="P", pid=None):
    db.execute_query(
        "INSERT INTO portfolios (portfolio_id, name, initial_capital, current_cash) "
        "VALUES (:i, :n, :c, :c)",
        {"i": pid or name, "n": name, "c": 1000},
    )


# ===========================================================================
# Configuration
# ===========================================================================


class TestConfig:
    def test_loads_shipped_yaml(self):
        cfg = load_config(path=str(CONFIG_FILE), profile="testing")
        assert cfg.dialect == "sqlite"
        assert cfg.profile == "testing"

    def test_profile_overrides_default_block(self):
        default = load_config(path=str(CONFIG_FILE), profile="development")
        testing = load_config(path=str(CONFIG_FILE), profile="testing")
        # `default:` says 3 retries; the testing profile lowers it to 1.
        assert default.retry_attempts == 3
        assert testing.retry_attempts == 1

    def test_env_beats_yaml(self, monkeypatch):
        monkeypatch.setenv(f"{ENV_PREFIX}_POOL_MAX_SIZE", "77")
        cfg = load_config(path=str(CONFIG_FILE), profile="testing")
        assert cfg.pool_max_size == 77

    def test_argument_beats_env(self, monkeypatch):
        monkeypatch.setenv(f"{ENV_PREFIX}_POOL_MAX_SIZE", "77")
        cfg = load_config(path=str(CONFIG_FILE), profile="testing", pool_max_size=9)
        assert cfg.pool_max_size == 9

    def test_env_selects_profile(self, monkeypatch):
        monkeypatch.setenv(f"{ENV_PREFIX}_PROFILE", "testing")
        assert load_config(path=str(CONFIG_FILE)).profile == "testing"

    def test_unknown_profile_lists_the_valid_ones(self):
        with pytest.raises(ConfigError, match="unknown profile"):
            load_config(path=str(CONFIG_FILE), profile="staging")

    def test_missing_explicit_file_is_an_error(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config(path="/nonexistent/database.yaml")

    def test_missing_default_file_is_tolerated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "backtest.db.config.DEFAULT_CONFIG_PATH", tmp_path / "absent.yaml"
        )
        monkeypatch.setenv(f"{ENV_PREFIX}_URL", "sqlite:///:memory:")
        assert load_config().dialect == "sqlite"

    def test_malformed_yaml_names_the_file(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("profiles:\n  dev:\n   url: a: b: c\n")
        with pytest.raises(ConfigError, match="could not parse"):
            load_config(path=str(bad))

    def test_missing_url_explains_how_to_fix_it(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("profiles:\n  dev: {}\n")
        with pytest.raises(ConfigError, match="FORWARD_TEST_DB_URL"):
            load_config(path=str(empty), profile="dev")

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            (dict(pool_min_size=0), "pool_min_size"),
            (dict(pool_min_size=10, pool_max_size=5), "pool_max_size"),
            (dict(retry_attempts=0), "retry_attempts"),
            (dict(retry_base_delay=5, retry_max_delay=1), "retry_max_delay"),
            (dict(pool_timeout=-1), "pool_timeout"),
            (dict(url=""), "No database URL"),
            (dict(url="not-a-url"), "missing scheme"),
        ],
    )
    def test_validation_rejects_nonsense(self, kwargs, match):
        base = dict(url="sqlite:///:memory:")
        with pytest.raises(ConfigError, match=match):
            DatabaseConfig(**{**base, **kwargs}).validated()

    def test_password_is_masked_everywhere(self):
        cfg = DatabaseConfig(
            url="postgresql+psycopg2://alice:hunter2@db.internal:5432/forward_test"
        )
        assert "hunter2" not in cfg.safe_url
        assert "***" in cfg.safe_url
        assert "alice" in cfg.safe_url  # username is not a secret
        assert "hunter2" not in str(cfg.describe())

    def test_safe_url_without_password_is_unchanged(self):
        cfg = DatabaseConfig(url="sqlite:///forward_test.db")
        assert cfg.safe_url == "sqlite:///forward_test.db"

    def test_max_overflow_derivation(self):
        cfg = DatabaseConfig(url="sqlite://", pool_min_size=5, pool_max_size=20)
        assert cfg.max_overflow == 15

    @pytest.mark.parametrize(
        "url, dialect, is_pg, is_sqlite",
        [
            ("postgresql+psycopg2://u@h/d", "postgresql", True, False),
            ("postgresql://u@h/d", "postgresql", True, False),
            ("sqlite:///x.db", "sqlite", False, True),
        ],
    )
    def test_dialect_detection(self, url, dialect, is_pg, is_sqlite):
        cfg = DatabaseConfig(url=url)
        assert (cfg.dialect, cfg.is_postgres, cfg.is_sqlite) == (dialect, is_pg, is_sqlite)

    def test_config_is_immutable(self):
        cfg = DatabaseConfig(url="sqlite://")
        with pytest.raises(Exception):
            cfg.url = "postgresql://"  # type: ignore[misc]

    def test_with_overrides_returns_a_validated_copy(self):
        cfg = DatabaseConfig(url="sqlite://")
        assert cfg.with_overrides(pool_max_size=30).pool_max_size == 30
        with pytest.raises(ConfigError):
            cfg.with_overrides(pool_max_size=0)

    def test_unknown_override_key_is_rejected(self):
        with pytest.raises(ConfigError, match="unknown configuration keys"):
            load_config(path=str(CONFIG_FILE), profile="testing", pool_sizzle=1)

    def test_bool_parsing_from_env(self, monkeypatch):
        for raw, expected in [("true", True), ("1", True), ("no", False), ("off", False)]:
            monkeypatch.setenv(f"{ENV_PREFIX}_LOG_QUERIES", raw)
            assert load_config(path=str(CONFIG_FILE), profile="testing").log_queries is expected

    def test_bad_int_from_env_is_reported(self, monkeypatch):
        monkeypatch.setenv(f"{ENV_PREFIX}_POOL_MAX_SIZE", "many")
        with pytest.raises(ConfigError, match="pool_max_size must be an integer"):
            load_config(path=str(CONFIG_FILE), profile="testing")

    def test_production_profile_has_no_hardcoded_url(self):
        """Credentials must never live in a committed file."""
        import yaml

        doc = yaml.safe_load(CONFIG_FILE.read_text())
        assert "url" not in (doc["profiles"]["production"] or {})


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestLifecycle:
    def test_connect_is_idempotent(self, db):
        assert db.connect() is db.connect()

    def test_lazy_connect_on_first_use(self):
        m = DatabaseManager.from_env(path=str(CONFIG_FILE), profile="testing")
        assert not m.is_connected
        assert m.fetch_scalar("SELECT 1") == 1
        assert m.is_connected
        m.disconnect()

    def test_disconnect_is_idempotent(self, db):
        db.disconnect()
        db.disconnect()
        assert not db.is_connected

    def test_reuse_after_disconnect_is_refused(self, db):
        db.disconnect()
        with pytest.raises(DbConnectionError, match="disconnected"):
            db.connect()

    def test_context_manager_connects_and_disposes(self):
        with DatabaseManager.from_env(path=str(CONFIG_FILE), profile="testing") as m:
            assert m.is_connected
            assert m.fetch_scalar("SELECT 1") == 1
        assert not m.is_connected

    def test_context_manager_disposes_on_exception(self):
        m = DatabaseManager.from_env(path=str(CONFIG_FILE), profile="testing")
        with pytest.raises(ValueError):
            with m:
                raise ValueError("boom")
        assert not m.is_connected

    @requires_psycopg2
    def test_unreachable_database_raises_connection_error(self):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="postgresql+psycopg2://nobody@127.0.0.1:1/none",
            retry_attempts=1,
            connect_timeout=1,
        )
        with pytest.raises(DbConnectionError, match="unreachable"):
            m.connect()
        assert not m.is_connected  # must not be left half-initialised

    @requires_psycopg2
    def test_connection_error_never_leaks_the_password(self):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="postgresql+psycopg2://user:sup3rs3cret@127.0.0.1:1/none",
            retry_attempts=1,
            connect_timeout=1,
        )
        with pytest.raises(DbConnectionError) as excinfo:
            m.connect()
        assert "sup3rs3cret" not in str(excinfo.value)

    def test_repr_masks_password(self):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE), profile="testing",
            url="postgresql+psycopg2://u:pw@h:5432/d",
        )
        assert "pw" not in repr(m)


# ===========================================================================
# Pooling
# ===========================================================================


class TestPooling:
    def test_postgres_uses_a_bounded_queue_pool(self):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="production",
            url="postgresql+psycopg2://u@h/d",
            pool_min_size=5,
            pool_max_size=20,
        )
        kwargs = m._engine_kwargs()
        assert kwargs["poolclass"] is QueuePool
        assert kwargs["pool_size"] == 5
        assert kwargs["max_overflow"] == 15  # 5 + 15 = 20 hard ceiling
        assert kwargs["pool_pre_ping"] is True

    def test_postgres_sets_statement_timeout_and_app_name(self):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="production",
            url="postgresql+psycopg2://u@h/d",
            statement_timeout_ms=1234,
        )
        args = m._engine_kwargs()["connect_args"]
        assert "statement_timeout=1234" in args["options"]
        assert args["application_name"] == "forward_test"

    def test_memory_sqlite_uses_static_pool(self, db):
        """An in-memory DB lives in one connection; all callers must share it."""
        assert isinstance(db.engine.pool, StaticPool)

    def test_file_sqlite_uses_null_pool(self, tmp_path):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url=f"sqlite:///{tmp_path/'x.db'}",
        )
        m.connect()
        assert isinstance(m.engine.pool, NullPool)
        m.disconnect()

    def test_pool_status_reports_metrics(self, db):
        status = db.pool_status()
        assert status["class"] == "StaticPool"
        assert db.pool_status() != {} 

    def test_pool_status_empty_before_connect(self):
        m = DatabaseManager.from_env(path=str(CONFIG_FILE), profile="testing")
        assert m.pool_status() == {}

    def test_concurrent_checkouts_stay_within_the_pool(self, tmp_path):
        """Many threads hammering the pool must not deadlock or error."""
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url=f"sqlite:///{tmp_path/'concurrent.db'}",
        )
        m.connect()
        Base.metadata.create_all(m.engine)
        errors: list[Exception] = []

        def worker():
            try:
                for _ in range(10):
                    m.fetch_scalar("SELECT 1")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        m.disconnect()
        assert not errors


# ===========================================================================
# Queries
# ===========================================================================


class TestQueries:
    def test_execute_query_returns_rowcount(self, db):
        assert (
            db.execute_query(
                "INSERT INTO portfolios (portfolio_id,name,initial_capital,current_cash) "
                "VALUES ('a','A',1,1)"
            )
            == 1
        )

    def test_fetch_one_returns_dict(self, db):
        _add_portfolio(db, "Alpha")
        assert db.fetch_one("SELECT name FROM portfolios") == {"name": "Alpha"}

    def test_fetch_one_returns_none_when_empty(self, db):
        assert db.fetch_one("SELECT name FROM portfolios WHERE name='nope'") is None

    def test_fetch_all_returns_list_of_dicts(self, db):
        _add_portfolio(db, "A")
        _add_portfolio(db, "B")
        names = {r["name"] for r in db.fetch_all("SELECT name FROM portfolios")}
        assert names == {"A", "B"}

    def test_fetch_all_empty_is_empty_list(self, db):
        assert db.fetch_all("SELECT name FROM portfolios") == []

    def test_fetch_scalar(self, db):
        _add_portfolio(db, "A")
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 1

    def test_named_parameters_are_bound_not_interpolated(self, db):
        _add_portfolio(db, "A")
        assert db.fetch_one(
            "SELECT name FROM portfolios WHERE name = :n", {"n": "A"}
        ) == {"name": "A"}

    def test_sql_injection_is_neutralised(self, db):
        """A malicious value must be treated as data, never as SQL."""
        _add_portfolio(db, "A")
        evil = "A'; DROP TABLE portfolios; --"
        assert db.fetch_one("SELECT name FROM portfolios WHERE name = :n", {"n": evil}) is None
        # The table is still there.
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 1

    def test_execute_many_inserts_every_row(self, db):
        rows = [
            {"i": f"p{i}", "n": f"P{i}", "c": 100 + i} for i in range(50)
        ]
        db.execute_many(
            "INSERT INTO portfolios (portfolio_id,name,initial_capital,current_cash) "
            "VALUES (:i,:n,:c,:c)",
            rows,
        )
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 50

    def test_execute_many_with_no_rows_is_a_noop(self, db):
        assert db.execute_many("INSERT INTO portfolios VALUES (1)", []) == 0

    def test_constraint_violation_propagates_unretried(self, db):
        """Deterministic errors must surface immediately, not after 3 tries."""
        _add_portfolio(db, "dup")
        before = db.stats["retries"]
        with pytest.raises(IntegrityError):
            _add_portfolio(db, "dup")
        assert db.stats["retries"] == before

    def test_bad_sql_propagates(self, db):
        with pytest.raises((OperationalError, ProgrammingError)):
            db.fetch_all("SELECT * FROM table_that_does_not_exist")


# ===========================================================================
# Transactions
# ===========================================================================


class TestTransactions:
    def test_context_manager_commits(self, db):
        with db.transaction() as conn:
            conn.execute(
                text(
                    "INSERT INTO portfolios (portfolio_id,name,initial_capital,current_cash) "
                    "VALUES ('t','T',1,1)"
                )
            )
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 1

    def test_context_manager_rolls_back_on_error(self, db):
        with pytest.raises(ValueError):
            with db.transaction() as conn:
                conn.execute(
                    text(
                        "INSERT INTO portfolios (portfolio_id,name,initial_capital,current_cash) "
                        "VALUES ('t','T',1,1)"
                    )
                )
                raise ValueError("abort")
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 0

    def test_explicit_commit(self, db):
        conn = db.begin_transaction()
        conn.execute(
            text(
                "INSERT INTO portfolios (portfolio_id,name,initial_capital,current_cash) "
                "VALUES ('t','T',1,1)"
            )
        )
        db.commit()
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 1

    def test_explicit_rollback(self, db):
        conn = db.begin_transaction()
        conn.execute(
            text(
                "INSERT INTO portfolios (portfolio_id,name,initial_capital,current_cash) "
                "VALUES ('t','T',1,1)"
            )
        )
        db.rollback()
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 0

    def test_commit_without_begin_is_an_error(self, db):
        with pytest.raises(TransactionError, match="no active transaction"):
            db.commit()

    def test_rollback_without_begin_is_an_error(self, db):
        with pytest.raises(TransactionError, match="no active transaction"):
            db.rollback()

    def test_nested_begin_is_refused(self, db):
        db.begin_transaction()
        try:
            with pytest.raises(TransactionError, match="already active"):
                db.begin_transaction()
        finally:
            db.rollback()

    def test_helpers_join_an_open_transaction(self, db):
        """execute_query inside an explicit transaction must be rolled back too."""
        db.begin_transaction()
        _add_portfolio(db, "joined")
        db.rollback()
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 0

    def test_transaction_state_is_per_thread(self, tmp_path):
        """One thread's transaction must be invisible to another."""
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url=f"sqlite:///{tmp_path/'threads.db'}",
        )
        m.connect()
        Base.metadata.create_all(m.engine)
        m.begin_transaction()
        seen: list[bool] = []

        def other_thread():
            # No transaction here, even though the main thread has one open.
            seen.append(m._has_active_transaction())

        t = threading.Thread(target=other_thread)
        t.start()
        t.join(timeout=10)
        m.rollback()
        m.disconnect()
        assert seen == [False]

    def test_disconnect_rolls_back_an_open_transaction(self, tmp_path):
        path = tmp_path / "shutdown.db"
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE), profile="testing", url=f"sqlite:///{path}"
        )
        m.connect()
        Base.metadata.create_all(m.engine)
        conn = m.begin_transaction()
        conn.execute(
            text(
                "INSERT INTO portfolios (portfolio_id,name,initial_capital,current_cash) "
                "VALUES ('t','T',1,1)"
            )
        )
        m.disconnect()  # must NOT commit

        again = DatabaseManager.from_env(
            path=str(CONFIG_FILE), profile="testing", url=f"sqlite:///{path}"
        )
        again.connect()
        assert again.fetch_scalar("SELECT count(*) FROM portfolios") == 0
        again.disconnect()


# ===========================================================================
# ORM sessions
# ===========================================================================


class TestSessions:
    def test_session_commits(self, db):
        with db.session() as s:
            s.add(Portfolio(name="orm", initial_capital=1000, current_cash=1000))
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 1

    def test_session_rolls_back_on_error(self, db):
        with pytest.raises(ValueError):
            with db.session() as s:
                s.add(Portfolio(name="orm", initial_capital=1000, current_cash=1000))
                raise ValueError("nope")
        assert db.fetch_scalar("SELECT count(*) FROM portfolios") == 0

    def test_session_surfaces_constraint_violations(self, db):
        with db.session() as s:
            s.add(Portfolio(name="dup", initial_capital=1, current_cash=1))
        with pytest.raises(IntegrityError):
            with db.session() as s:
                s.add(Portfolio(name="dup", initial_capital=1, current_cash=1))

    def test_objects_usable_after_commit(self, db):
        """expire_on_commit=False keeps attributes readable post-commit."""
        with db.session() as s:
            p = Portfolio(name="orm", initial_capital=1000, current_cash=1000)
            s.add(p)
        assert p.name == "orm"  # would raise DetachedInstanceError if expired


# ===========================================================================
# Retries and recovery
# ===========================================================================


class _Flaky:
    """Fails with a transient error the first ``n`` times, then succeeds."""

    def __init__(self, failures: int):
        self.remaining = failures
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise OperationalError("SELECT 1", {}, Exception("connection lost"))
        return "ok"


class TestRetries:
    def test_transient_failure_is_retried_then_succeeds(self, retrying_db):
        flaky = _Flaky(failures=2)
        assert retrying_db._retrying("test", flaky) == "ok"
        assert flaky.calls == 3

    def test_gives_up_after_configured_attempts(self, tmp_path):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="sqlite:///:memory:",
            retry_attempts=3,
            retry_base_delay=0.001,
            retry_max_delay=0.002,
        )
        m.connect()
        flaky = _Flaky(failures=99)
        with pytest.raises(DbConnectionError, match="after 3 attempt"):
            m._retrying("test", flaky)
        assert flaky.calls == 3
        m.disconnect()

    def test_deterministic_errors_are_not_retried(self, db):
        calls = {"n": 0}

        def always_integrity():
            calls["n"] += 1
            raise IntegrityError("stmt", {}, Exception("duplicate key"))

        with pytest.raises(IntegrityError):
            db._retrying("test", always_integrity)
        assert calls["n"] == 1, "constraint violations must fail immediately"

    def test_no_retry_inside_an_open_transaction(self, db):
        """Replaying a statement mid-transaction could corrupt data."""
        db.begin_transaction()
        try:
            flaky = _Flaky(failures=99)
            with pytest.raises(DbConnectionError, match="after 1 attempt"):
                db._retrying("test", flaky)
            assert flaky.calls == 1
        finally:
            db.rollback()

    def test_backoff_grows_between_attempts(self, tmp_path, monkeypatch):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="sqlite:///:memory:",
            retry_attempts=4,
            retry_base_delay=1.0,
            retry_max_delay=100.0,
        )
        m.connect()
        slept: list[float] = []
        monkeypatch.setattr("backtest.db.manager.time.sleep", slept.append)
        # Remove jitter so the progression is deterministic.
        monkeypatch.setattr("backtest.db.manager.random.random", lambda: 0.5)
        with pytest.raises(DbConnectionError):
            m._retrying("test", _Flaky(failures=99))
        assert slept == [1.0, 2.0, 4.0]  # doubling
        m.disconnect()

    def test_backoff_is_capped(self, monkeypatch):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="sqlite:///:memory:",
            retry_attempts=6,
            retry_base_delay=1.0,
            retry_max_delay=3.0,
        )
        m.connect()
        slept: list[float] = []
        monkeypatch.setattr("backtest.db.manager.time.sleep", slept.append)
        monkeypatch.setattr("backtest.db.manager.random.random", lambda: 0.5)
        with pytest.raises(DbConnectionError):
            m._retrying("test", _Flaky(failures=99))
        assert max(slept) <= 3.0
        m.disconnect()

    def test_retry_counter_is_tracked(self, retrying_db):
        before = retrying_db.stats["retries"]
        retrying_db._retrying("test", _Flaky(failures=2))
        assert retrying_db.stats["retries"] == before + 2

    def test_recovers_after_the_pool_is_invalidated(self, tmp_path):
        """Simulates a database restart: stale connections must be replaced."""
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url=f"sqlite:///{tmp_path/'restart.db'}",
        )
        m.connect()
        Base.metadata.create_all(m.engine)
        _add_portfolio(m, "before")

        m.engine.dispose()  # every pooled connection is now gone

        assert m.fetch_scalar("SELECT count(*) FROM portfolios") == 1
        _add_portfolio(m, "after")
        assert m.fetch_scalar("SELECT count(*) FROM portfolios") == 2
        m.disconnect()


# ===========================================================================
# Health checks and logging
# ===========================================================================


class TestHealthAndLogging:
    def test_health_check_when_healthy(self, db):
        report = db.health_check()
        assert report["healthy"] is True
        assert report["dialect"] == "sqlite"
        assert report["latency_ms"] >= 0
        assert "error" not in report

    def test_health_check_reports_failure_without_raising(self):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="postgresql+psycopg2://nobody@127.0.0.1:1/none",
            retry_attempts=1,
            connect_timeout=1,
        )
        report = m.health_check()  # must not raise
        assert report["healthy"] is False
        assert "error" in report

    def test_health_check_masks_password(self):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="postgresql+psycopg2://u:topsecret@127.0.0.1:1/none",
            retry_attempts=1,
            connect_timeout=1,
        )
        assert "topsecret" not in str(m.health_check())

    def test_slow_query_warning(self, caplog, tmp_path):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="sqlite:///:memory:",
            slow_query_ms=1,
        )
        m.connect()
        with caplog.at_level(logging.WARNING, logger="backtest.db"):
            m.fetch_scalar("SELECT 1")
            time.sleep(0.005)
            m.fetch_scalar("SELECT 1")
        m.disconnect()
        # At least the counter must have moved; timing-based log assertions
        # are flaky on a loaded CI box.
        assert m.stats["queries"] > 0

    def test_query_logging_can_be_enabled(self, caplog):
        m = DatabaseManager.from_env(
            path=str(CONFIG_FILE),
            profile="testing",
            url="sqlite:///:memory:",
            log_queries=True,
            slow_query_ms=0,
        )
        m.connect()
        with caplog.at_level(logging.DEBUG, logger="backtest.db"):
            m.fetch_scalar("SELECT 1")
        m.disconnect()
        assert any("SELECT 1" in r.getMessage() for r in caplog.records)

    def test_stats_are_counted(self, db):
        before = db.stats["queries"]
        db.fetch_scalar("SELECT 1")
        assert db.stats["queries"] > before

    def test_stats_snapshot_is_a_copy(self, db):
        snapshot = db.stats
        snapshot["queries"] = -999
        assert db.stats["queries"] != -999


# ===========================================================================
# Transient-vs-permanent classification
#
# This is the subtlest part of the retry logic. OperationalError means
# "connection died" on PostgreSQL but also "no such table" on SQLite, so the
# message has to be inspected. Getting this wrong either retries pointlessly
# (hiding the real error) or fails to recover from a database restart.
# ===========================================================================


class TestTransientClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "server closed the connection unexpectedly",
            "connection already closed",
            "could not connect to server: Connection refused",
            "terminating connection due to administrator command",
            "the database system is starting up",
            "sorry, too many clients already",
            "SSL connection has been closed unexpectedly",
            "database is locked",
            "canceling statement due to statement timeout",
            "connection timed out",
            # libpq's real wording when the server is down. Verified against a
            # live PostgreSQL killed mid-query.
            'connection to server on socket "/tmp/.s.PGSQL.5432" failed: '
            "No such file or directory",
            "connection to server at localhost, port 5432 failed: Connection refused",
            "Is the server running locally and accepting connections on that socket?",
        ],
    )
    def test_recognised_as_transient(self, message):
        from backtest.db.manager import _is_transient

        assert _is_transient(OperationalError("stmt", {}, Exception(message)))

    @pytest.mark.parametrize(
        "exc",
        [
            OperationalError("stmt", {}, Exception("no such table: portfolios")),
            OperationalError("stmt", {}, Exception('relation "orders" does not exist')),
            OperationalError("stmt", {}, Exception("syntax error at or near SELCT")),
            IntegrityError("stmt", {}, Exception("duplicate key value")),
            ProgrammingError("stmt", {}, Exception("column x does not exist")),
        ],
    )
    def test_recognised_as_permanent(self, exc):
        from backtest.db.manager import _is_transient

        assert not _is_transient(exc)

    def test_invalidated_connection_is_always_transient(self):
        from backtest.db.manager import _is_transient

        exc = OperationalError("stmt", {}, Exception("something odd"))
        exc.connection_invalidated = True
        assert _is_transient(exc)

    def test_missing_table_surfaces_immediately(self, retrying_db):
        """A missing table must not be retried three times and rewrapped."""
        before = retrying_db.stats["retries"]
        with pytest.raises((OperationalError, ProgrammingError)):
            retrying_db.fetch_all("SELECT * FROM no_such_table")
        assert retrying_db.stats["retries"] == before


def test_database_connection_error_alias_is_the_same_class():
    """ConnectionError shadows the builtin; the alias must be interchangeable."""
    from backtest.db import ConnectionError as Shadowing, DatabaseConnectionError

    assert DatabaseConnectionError is Shadowing
    assert issubclass(DatabaseConnectionError, Exception)