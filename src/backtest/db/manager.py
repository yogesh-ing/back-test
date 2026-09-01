"""Database connection manager for the forward testing simulator.

Wraps a SQLAlchemy engine with the operational behaviour the trading loop
needs: a bounded connection pool, automatic recovery from a database restart,
bounded retries on transient faults, transaction scoping, and health checks.

Quick start
-----------
::

    from backtest.db import DatabaseManager

    db = DatabaseManager.from_env()          # reads config/database.yaml + env
    db.connect()

    rows = db.fetch_all(
        "SELECT symbol, quantity FROM positions WHERE portfolio_id = :pid",
        {"pid": portfolio_id},
    )

    with db.transaction() as conn:           # commits on exit, rolls back on error
        conn.execute(text("UPDATE portfolios SET current_cash = :c"), {"c": 100})

    with db.session() as s:                  # ORM unit of work
        s.add(Portfolio(name="run-1", initial_capital=100000, current_cash=100000))

    db.disconnect()

Or as a context manager, which connects and disposes for you::

    with DatabaseManager.from_env() as db:
        print(db.health_check())

Design notes
------------
Retries cover transient faults only
    A dropped socket or a SQLite write lock is worth retrying. A constraint
    violation or a syntax error is not — it will fail identically every time,
    and retrying only delays the error. See :data:`TRANSIENT_ERRORS`.

Retries never span an open transaction
    Replaying one statement inside a transaction whose earlier statements
    already applied would corrupt data. Retry logic is therefore disabled
    whenever an explicit transaction is active on the current thread.

Thread safety
    The engine and its pool are thread-safe by design. Explicit
    :meth:`begin_transaction` / :meth:`commit` / :meth:`rollback` state is
    held in thread-local storage, so two threads cannot disturb each other's
    transactions.
"""

from __future__ import annotations

import atexit
import logging
import random
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import Engine, Result
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from backtest.db.config import DatabaseConfig, load_config

__all__ = [
    "DatabaseManager",
    "DatabaseError",
    "ConnectionError",
    "DatabaseConnectionError",
    "TransactionError",
    "TRANSIENT_ERRORS",
]

logger = logging.getLogger("backtest.db")

#: Exception types that *may* be transient. Membership here is necessary but
#: not sufficient — see :func:`_is_transient`.
#:
#: ``OperationalError`` is unavoidably ambiguous: PostgreSQL raises it for a
#: dropped socket, but SQLite also raises it for ``no such table``. Retrying
#: the latter is pointless and hides the real error behind a misleading
#: "connection failed" message, so the message itself is inspected too.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    OperationalError,
    DisconnectionError,
    InterfaceError,
)

#: Substrings identifying a genuinely transient fault, matched against the
#: lower-cased exception text. Deliberately conservative: anything not listed
#: is treated as permanent and surfaces immediately.
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    # -- connection lifecycle (PostgreSQL / psycopg2) --
    "server closed the connection",
    "connection already closed",
    "connection has been closed",
    "connection not open",
    "connection reset",
    "connection refused",
    "connection lost",
    "connection timed out",
    "could not connect",
    # libpq prefixes every connect failure with this, whether the cause is
    # "Connection refused" (TCP) or "No such file or directory" (unix socket
    # gone because the server is down).
    "connection to server",
    "is the server running",
    "no connection to the server",
    "terminating connection",
    "broken pipe",
    "eof detected",
    "ssl connection has been closed",
    "ssl syscall error",
    "server has gone away",
    # -- server availability --
    "the database system is starting up",
    "the database system is shutting down",
    "too many clients",
    "sorry, too many clients already",
    "cannot allocate memory",
    # -- timeouts --
    "timeout expired",
    "timed out",
    "canceling statement due to statement timeout",
    # -- SQLite lock contention: a genuine retry-worthy condition --
    "database is locked",
    "database table is locked",
)


def _is_transient(exc: BaseException) -> bool:
    """Decide whether ``exc`` is worth retrying.

    A failure is transient when SQLAlchemy has already invalidated the
    connection, or when the driver's message matches a known recoverable
    condition. Everything else — constraint violations, syntax errors,
    missing tables — is permanent and must surface on the first attempt.
    """
    if isinstance(exc, DisconnectionError):
        return True
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    if not isinstance(exc, TRANSIENT_ERRORS):
        return False
    text_ = str(exc).lower()
    return any(pattern in text_ for pattern in _TRANSIENT_PATTERNS)


class DatabaseError(RuntimeError):
    """Base class for connection-manager failures."""


class ConnectionError(DatabaseError):  # noqa: A001 - name required by the spec
    """Could not establish or maintain a database connection.

    .. warning::
       This name shadows the built-in :class:`ConnectionError` when imported
       unqualified. :data:`DatabaseConnectionError` is an alias for exactly
       the same class and is the safer import in application code.
    """


#: Unambiguous alias for :class:`ConnectionError`, which shadows the builtin.
#: ``except DatabaseConnectionError`` catches the same exceptions without the
#: risk of accidentally shadowing (or being shadowed by) the built-in type.
DatabaseConnectionError = ConnectionError


class TransactionError(DatabaseError):
    """Explicit transaction API used incorrectly."""


class DatabaseManager:
    """Owns a SQLAlchemy engine and its connection pool.

    Parameters
    ----------
    config:
        Settings to use. Build one with
        :func:`backtest.db.config.load_config`, or call :meth:`from_env`.

    Notes
    -----
    Construction is cheap and does not touch the network. The engine is built
    on the first :meth:`connect` (or on first use, which connects lazily).
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._lock = threading.RLock()
        self._local = threading.local()
        self._closed = False
        # Cheap observability for the Step 20 monitoring hooks.
        self._stats = {"queries": 0, "retries": 0, "failures": 0, "slow_queries": 0}

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        path: str | None = None,
        profile: str | None = None,
        **overrides: Any,
    ) -> "DatabaseManager":
        """Build a manager from ``config/database.yaml`` plus environment.

        Any keyword override is passed through to
        :func:`~backtest.db.config.load_config`.
        """
        return cls(load_config(path=path, profile=profile, **overrides))

    @property
    def config(self) -> DatabaseConfig:
        return self._config

    @property
    def engine(self) -> Engine:
        """The live engine, connecting on first access."""
        if self._engine is None:
            self.connect()
        assert self._engine is not None  # for type checkers
        return self._engine

    @property
    def is_connected(self) -> bool:
        return self._engine is not None and not self._closed

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> Engine:
        """Create the engine and verify the database is reachable.

        Idempotent: calling it twice returns the same engine.

        Raises
        ------
        ConnectionError
            If the database cannot be reached after ``retry_attempts``.
        """
        with self._lock:
            if self._closed:
                raise ConnectionError(
                    "this DatabaseManager has been disconnected; create a new one"
                )
            if self._engine is not None:
                return self._engine

            cfg = self._config
            logger.info("connecting to database", extra={"db": cfg.describe()})

            try:
                engine = create_engine(cfg.url, **self._engine_kwargs())
            except Exception as exc:
                raise ConnectionError(f"could not build engine for {cfg.safe_url}: {exc}") from exc

            self._install_listeners(engine)
            self._engine = engine
            self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

            # Fail fast and loudly rather than at the first query.
            try:
                self._retrying("connect", self._ping, engine)
            except Exception as exc:
                self._engine = None
                self._session_factory = None
                engine.dispose()
                raise ConnectionError(f"database unreachable at {cfg.safe_url}: {exc}") from exc

            atexit.register(self._atexit_dispose)
            logger.info("database connected (%s)", cfg.dialect)
            return engine

    def disconnect(self) -> None:
        """Close every pooled connection and mark the manager unusable.

        Safe to call more than once. Any thread-local transaction still open
        is rolled back first, so a crash during shutdown cannot silently
        commit partial work.
        """
        with self._lock:
            if self._engine is None:
                self._closed = True
                return

            if self._has_active_transaction():
                logger.warning("disconnect() with an open transaction — rolling back")
                try:
                    self.rollback()
                except Exception:  # pragma: no cover - best effort
                    logger.exception("rollback during disconnect failed")

            logger.info("closing database connections")
            try:
                self._engine.dispose()
            finally:
                self._engine = None
                self._session_factory = None
                self._closed = True
                try:
                    atexit.unregister(self._atexit_dispose)
                except Exception:  # pragma: no cover
                    pass

    def _atexit_dispose(self) -> None:  # pragma: no cover - interpreter shutdown
        """Last-resort cleanup so the process never leaks sockets."""
        try:
            if self._engine is not None:
                self._engine.dispose()
        except Exception:
            pass

    def __enter__(self) -> "DatabaseManager":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()

    # -- engine construction details ---------------------------------------

    def _engine_kwargs(self) -> dict[str, Any]:
        """Translate :class:`DatabaseConfig` into ``create_engine`` arguments."""
        cfg = self._config
        kwargs: dict[str, Any] = {
            "echo": cfg.echo,
            "future": True,
            "pool_pre_ping": cfg.pool_pre_ping,
        }
        connect_args: dict[str, Any] = {}

        if cfg.is_sqlite:
            # SQLite serialises writes; a QueuePool adds contention without
            # adding throughput.
            if cfg.is_memory_sqlite:
                # An in-memory database lives inside a single connection, so
                # every caller must share exactly one. StaticPool does that;
                # anything else would hand out empty databases.
                kwargs["poolclass"] = StaticPool
                connect_args["check_same_thread"] = False
            else:
                kwargs["poolclass"] = NullPool
            # Wait rather than fail immediately when another writer holds the
            # lock. Value in seconds.
            connect_args["timeout"] = max(1, cfg.connect_timeout)
        else:
            kwargs.update(
                poolclass=QueuePool,
                pool_size=cfg.pool_min_size,
                max_overflow=cfg.max_overflow,
                pool_timeout=cfg.pool_timeout,
                pool_recycle=cfg.pool_recycle,
            )

        if cfg.is_postgres:
            connect_args["connect_timeout"] = cfg.connect_timeout
            connect_args["application_name"] = cfg.application_name
            if cfg.statement_timeout_ms > 0:
                # Server-side guard: one runaway query cannot hold a pool slot
                # forever.
                connect_args["options"] = f"-c statement_timeout={cfg.statement_timeout_ms}"

        if connect_args:
            kwargs["connect_args"] = connect_args
        return kwargs

    def _install_listeners(self, engine: Engine) -> None:
        """Attach PRAGMA setup and optional query logging."""
        cfg = self._config

        if cfg.is_sqlite:

            @event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
                """SQLite defaults are unsafe for this workload; fix them."""
                cursor = dbapi_conn.cursor()
                try:
                    # Foreign keys are OFF by default in SQLite. Without this,
                    # every FK in the schema is decorative.
                    cursor.execute("PRAGMA foreign_keys=ON")
                    # WAL lets readers proceed during a write — important when
                    # a dashboard polls while the trading loop writes.
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute(f"PRAGMA busy_timeout={max(1, cfg.connect_timeout) * 1000}")
                finally:
                    cursor.close()

        # The timing listeners are always installed: the query counter feeds
        # the Step 20 monitoring hooks, and the cost is one perf_counter()
        # call per statement. Only the *logging* is conditional.
        @event.listens_for(engine, "before_cursor_execute")
        def _start_timer(
            conn: Any, cursor: Any, statement: str, params: Any, context: Any, executemany: bool
        ) -> None:
            conn.info.setdefault("_query_start", []).append(time.perf_counter())

        @event.listens_for(engine, "after_cursor_execute")
        def _log_duration(
            conn: Any, cursor: Any, statement: str, params: Any, context: Any, executemany: bool
        ) -> None:
            stack = conn.info.get("_query_start") or []
            if not stack:
                return
            elapsed_ms = (time.perf_counter() - stack.pop()) * 1000.0
            self._stats["queries"] += 1

            if cfg.slow_query_ms > 0 and elapsed_ms >= cfg.slow_query_ms:
                self._stats["slow_queries"] += 1
                logger.warning("slow query: %.1fms — %s", elapsed_ms, _compact(statement))
            elif cfg.log_queries:
                logger.debug("query %.1fms — %s", elapsed_ms, _compact(statement))

    @staticmethod
    def _ping(engine: Engine) -> None:
        """Minimal liveness probe."""
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    # -- retry -------------------------------------------------------------

    def _retrying(self, label: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run ``func``, retrying transient failures with jittered backoff.

        Retries are suppressed while an explicit transaction is open: replaying
        a statement whose predecessors already applied would corrupt state.
        """
        cfg = self._config
        attempts = 1 if self._has_active_transaction() else cfg.retry_attempts
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return func(*args, **kwargs)
            except SQLAlchemyError as exc:
                if not _is_transient(exc):
                    # Deterministic error (constraint violation, bad SQL,
                    # missing table). Retrying cannot help and would only
                    # disguise the real cause.
                    self._stats["failures"] += 1
                    raise
                last = exc
                if attempt >= attempts:
                    break
                delay = min(cfg.retry_max_delay, cfg.retry_base_delay * (2 ** (attempt - 1)))
                # Jitter prevents a fleet of workers reconnecting in lockstep
                # after a database restart.
                delay *= 0.5 + random.random()
                self._stats["retries"] += 1
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.2fs",
                    label,
                    attempt,
                    attempts,
                    _compact(str(exc)),
                    delay,
                )
                time.sleep(delay)

        self._stats["failures"] += 1
        assert last is not None
        raise ConnectionError(f"{label} failed after {attempts} attempt(s): {last}") from last

    # -- context managers --------------------------------------------------

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Yield a pooled connection inside a transaction.

        Commits on clean exit, rolls back on exception. If an explicit
        transaction is already open on this thread, that one is reused and
        this block does **not** commit — the outermost scope decides.
        """
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
            return

        conn = self.engine.connect()
        transaction = conn.begin()
        try:
            yield conn
        except Exception:
            transaction.rollback()
            conn.close()
            raise
        else:
            transaction.commit()
            conn.close()

    #: Readable alias — ``with db.transaction() as conn:``
    transaction = connection

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield an ORM :class:`~sqlalchemy.orm.Session` as a unit of work.

        Commits on clean exit, rolls back on exception, always closes.
        """
        if self._session_factory is None:
            self.connect()
        assert self._session_factory is not None

        s = self._session_factory()
        try:
            yield s
        except Exception:
            s.rollback()
            raise
        else:
            s.commit()
        finally:
            s.close()

    # -- explicit transaction API ------------------------------------------
    #
    # Provided because the Step 2 specification asks for it. Prefer the
    # context managers above: they cannot leak a connection if the caller
    # forgets to commit.

    def begin_transaction(self) -> Connection:
        """Open a transaction bound to the current thread.

        Must be paired with :meth:`commit` or :meth:`rollback`.

        Raises
        ------
        TransactionError
            If a transaction is already open on this thread. Nesting is
            rejected rather than silently flattened, because a "commit" of an
            inner block that actually commits the outer one is a data-loss
            bug waiting to happen.
        """
        if self._has_active_transaction():
            raise TransactionError(
                "a transaction is already active on this thread; "
                "commit or roll it back first (nesting is not supported)"
            )
        conn = self.engine.connect()
        self._local.connection = conn
        self._local.transaction = conn.begin()
        return conn

    def commit(self) -> None:
        """Commit the thread's explicit transaction."""
        self._finish_transaction(commit=True)

    def rollback(self) -> None:
        """Roll back the thread's explicit transaction."""
        self._finish_transaction(commit=False)

    def _finish_transaction(self, commit: bool) -> None:
        transaction = getattr(self._local, "transaction", None)
        conn = getattr(self._local, "connection", None)
        if transaction is None or conn is None:
            raise TransactionError(
                "no active transaction on this thread; call begin_transaction() first"
            )
        try:
            if commit:
                transaction.commit()
            else:
                transaction.rollback()
        finally:
            # Clear state before closing so a failure here cannot strand the
            # thread with an unusable transaction.
            self._local.transaction = None
            self._local.connection = None
            conn.close()

    def _has_active_transaction(self) -> bool:
        return getattr(self._local, "transaction", None) is not None

    # -- query helpers -----------------------------------------------------
    #
    # All of these accept raw SQL with named bind parameters (:name) and a
    # dict of values. Never interpolate values into the SQL string — bind
    # parameters are what keep this injection-safe.

    def execute_query(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        """Execute a statement and return the affected row count.

        For ``SELECT`` use :meth:`fetch_all` or :meth:`fetch_one` instead;
        the rows are not retained here.

        Examples
        --------
        >>> db.execute_query(                                  # doctest: +SKIP
        ...     "UPDATE portfolios SET status = :s WHERE portfolio_id = :p",
        ...     {"s": "paused", "p": pid},
        ... )
        1
        """

        def run() -> int:
            with self.connection() as conn:
                result = conn.execute(text(sql), dict(params or {}))
                return result.rowcount if result.rowcount is not None else -1

        return self._retrying("execute_query", run)

    def execute_many(self, sql: str, params_list: Sequence[Mapping[str, Any]]) -> int:
        """Execute one statement against many parameter sets.

        Uses the driver's executemany path, which is dramatically faster than
        looping — the difference matters when flushing a day of bars into
        ``market_data_cache``.

        Returns the affected row count, or ``0`` for an empty ``params_list``.
        """
        rows = [dict(p) for p in params_list]
        if not rows:
            return 0

        def run() -> int:
            with self.connection() as conn:
                result = conn.execute(text(sql), rows)
                return result.rowcount if result.rowcount is not None else -1

        return self._retrying("execute_many", run)

    def fetch_one(self, sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """Return the first row as a dict, or ``None`` if there are no rows."""

        def run() -> dict[str, Any] | None:
            with self.connection() as conn:
                row = conn.execute(text(sql), dict(params or {})).mappings().first()
                return dict(row) if row is not None else None

        return self._retrying("fetch_one", run)

    def fetch_all(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return every row as a list of dicts (empty list if none)."""

        def run() -> list[dict[str, Any]]:
            with self.connection() as conn:
                rows = conn.execute(text(sql), dict(params or {})).mappings().all()
                return [dict(r) for r in rows]

        return self._retrying("fetch_all", run)

    def fetch_scalar(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        """Return the first column of the first row, or ``None``.

        Convenient for ``SELECT count(*)`` and similar.
        """

        def run() -> Any:
            with self.connection() as conn:
                return conn.execute(text(sql), dict(params or {})).scalar()

        return self._retrying("fetch_scalar", run)

    # -- health / diagnostics ----------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """Probe the database and summarise pool state.

        Never raises — returns ``{"healthy": False, "error": ...}`` instead,
        so a monitoring loop can call it without its own try/except.
        """
        started = time.perf_counter()
        report: dict[str, Any] = {
            "healthy": False,
            "dialect": self._config.dialect,
            "url": self._config.safe_url,
            "profile": self._config.profile,
        }
        try:
            if self._engine is None:
                self.connect()
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            report["healthy"] = True
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            report["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            report["pool"] = self.pool_status()
            report["stats"] = dict(self._stats)
        return report

    def pool_status(self) -> dict[str, Any]:
        """Current pool occupancy, or ``{}`` when the pool exposes no metrics.

        ``NullPool`` and ``StaticPool`` (used for SQLite) have no counters.
        """
        if self._engine is None:
            return {}
        pool = self._engine.pool
        status: dict[str, Any] = {"class": type(pool).__name__}
        for name in ("size", "checkedin", "checkedout", "overflow"):
            getter = getattr(pool, name, None)
            if callable(getter):
                try:
                    status[name] = getter()
                except Exception:  # pragma: no cover - defensive
                    pass
        if isinstance(pool, QueuePool):
            status["max"] = self._config.pool_max_size
        return status

    @property
    def stats(self) -> dict[str, int]:
        """Counters for queries, retries, failures and slow queries."""
        return dict(self._stats)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        state = "connected" if self.is_connected else "disconnected"
        return f"<DatabaseManager {self._config.safe_url} [{state}]>"


def _compact(sql: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate, for readable one-line log entries."""
    flat = " ".join(str(sql).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
