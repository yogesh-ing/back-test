"""Alembic runtime environment for the forward testing simulator.

The database URL is resolved from the environment rather than ``alembic.ini``
so credentials stay out of version control. Resolution order:

1. ``-x db_url=...`` passed on the alembic command line
2. ``FORWARD_TEST_DB_URL`` environment variable
3. ``sqlalchemy.url`` in ``alembic.ini`` (normally blank)

``backtest.db.models.Base.metadata`` is the autogenerate target, so
``alembic revision --autogenerate`` diffs the live database against the ORM.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make ``src`` importable when alembic is invoked from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from backtest.db.models import Base  # noqa: E402  (import after sys.path fix)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Find the database URL, preferring the most explicit source."""
    from_cli = context.get_x_argument(as_dictionary=True).get("db_url")
    if from_cli:
        return from_cli

    from_env = os.getenv("FORWARD_TEST_DB_URL")
    if from_env:
        return from_env

    from_ini = config.get_main_option("sqlalchemy.url")
    if from_ini:
        return from_ini

    raise RuntimeError(
        "No database URL configured. Set FORWARD_TEST_DB_URL, e.g.\n"
        "  export FORWARD_TEST_DB_URL="
        "postgresql+psycopg2://user:pass@localhost:5432/forward_test\n"
        "or pass it inline:  alembic -x db_url=... upgrade head"
    )


def _include_object(obj, name, type_, reflected, compare_to):
    """Keep autogenerate from touching tables we do not own.

    ``schema_migrations`` is bookkeeping for the manual SQL path and must not
    be dropped when someone runs ``--autogenerate``.
    """
    if type_ == "table" and name == "schema_migrations":
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade --sql``).

    Useful for handing a reviewed script to a DBA.
    """
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        # Needed for ALTER on SQLite, harmless elsewhere.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations inside a transaction."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
            # SQLite cannot ALTER columns; batch mode rebuilds the table.
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()