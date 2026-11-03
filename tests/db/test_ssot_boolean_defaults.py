"""Sprint D's Postgres DDL must compile — ``halted`` was ``DEFAULT 0`` on a boolean.

``ensure_forward_ssot_schema()`` is the only path that creates the four Sprint D
tables on a live database (there is no alembic revision for them), and it runs
``Base.metadata.create_all()`` on every state persist.  Against this
deployment's Postgres 18 that call raised on the first one::

    psycopg2.errors.DatatypeMismatch: column "halted" is of type boolean but
    default expression is of type integer

because ``RiskSessionStateRecord.halted`` was ``Boolean,
server_default=text("0")``.  The exception is caught as a fail-soft dual-write
(``portfolio_manager.save_state``), so the symptom was not an outage but a
permanent silence: the SSoT tables never existed, every save wrote a traceback,
and the mirror never mirrored.

SQLite accepts ``DEFAULT 0`` for a boolean, so a schema test on the usual scratch
database cannot see this.  The guard has to compile the DDL for the dialect that
actually runs in production.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Boolean
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from backtest.db.models import (
    Base,
    FORWARD_SSOT_TABLES,
    RiskSessionStateRecord,
)

#: ``DEFAULT 0`` / ``DEFAULT 1`` are legal on SQLite and rejected by Postgres on
#: a boolean column.  Numeric types are the intended counterpart, so the check is
#: scoped to lines that declare a BOOLEAN rather than to the literal itself.
_INT_LITERAL = re.compile(r"\bDEFAULT\s+[01]\b", re.IGNORECASE)


def _postgresql_ddl(table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_no_boolean_column_anywhere_declares_an_integer_default():
    offenders = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if not isinstance(column.type, Boolean):
                continue
            default = column.server_default
            if default is None:
                continue
            text_value = str(default.arg)
            if re.match(r"^\s*[01]\s*$", text_value):
                offenders.append(f"{table.name}.{column.name} DEFAULT {text_value}")
    assert not offenders, f"boolean columns Postgres will refuse: {offenders}"


@pytest.mark.parametrize("table", FORWARD_SSOT_TABLES, ids=lambda t: t.name)
def test_forward_ssot_tables_compile_for_postgres(table):
    ddl = _postgresql_ddl(table)
    assert "CREATE TABLE" in ddl
    for line in ddl.splitlines():
        if "boolean" not in line.lower():
            continue
        assert not _INT_LITERAL.search(line), f"{table.name}: {line.strip()}"


def test_risk_session_state_halted_defaults_to_false():
    ddl = _postgresql_ddl(RiskSessionStateRecord.__table__)
    halted_line = next(line for line in ddl.splitlines() if "halted" in line.lower())
    assert "BOOLEAN" in halted_line.upper()
    assert "default false" in halted_line.lower()
    assert "NOT NULL" in halted_line.upper()
