"""Retire ``portfolios`` rows that no runner owns (BUG-37, ticket 2.4 item 3).

``SELECT sum(initial_capital) FROM portfolios WHERE status='active'`` is the
capital ledger the dashboard and the double-entry checks read. Rows stay
``'active'`` forever after their runner is deleted, re-spawned under a new
instance id, or never existed outside a test fixture — measured 2026-10-30:
63 active rows and ₹3.40 crore of capital against 14 live runners.

This module is deliberately pure (a session in, a report out) so the boot
reaper and the operator's one-shot tool share one definition of "claimed", and
so the tool can wrap its ``bak_`` snapshot and the flip in one transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, List, Set

from sqlalchemy import func, select

from backtest.db.models import Portfolio, Position

__all__ = [
    "RETIRED_STATUS",
    "ReapReport",
    "is_claimed",
    "reap_orphan_portfolio_rows",
    "runner_claim_keys",
]

#: Only a book that still claims to be live is reaped; anything already
#: stopped/paused is left exactly as the operator left it.
_REAP_FROM_STATUS = ("active",)

#: ``ck_portfolios_status`` admits active/paused/stopped only, so retirement
#: uses ``stopped`` — a distinct ``archived`` value needs a migration.
RETIRED_STATUS = "stopped"

#: ``LiveTradePersister`` names each runner's row ``"<config name> [<id8>]"``.
_INSTANCE_SUFFIX = re.compile(r"\[([0-9A-Fa-f]{8})\]\s*$")


@dataclass
class ReapReport:
    """What a reap found. ``reaped`` is empty for a clean ledger."""

    scanned: int = 0
    reaped: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    reaped_capital: Decimal = Decimal("0")
    #: Reaped rows that still carry ``positions.status='open'``. Reported, not
    #: blocked: a row nobody runs is phantom capital even while it shows marks.
    reaped_open_exposure_rows: int = 0

    @property
    def changed(self) -> int:
        return len(self.reaped)


def runner_claim_keys(runners: Iterable[Any]) -> Set[str]:
    """Every key a live runner owns in the ledger.

    A runner claims its row by the persistence convention (name + instance
    prefix), by the row id it was actually given (``_db_portfolio_id``, set on
    the first trade write), and by the id of the in-memory book signal logging
    attaches to.
    """
    claims: Set[str] = set()
    for runner in runners:
        instance_id = getattr(runner, "instance_id", None)
        if not instance_id:
            continue
        prefix = str(instance_id)[:8].lower()
        claims.add(prefix)
        for owned in (
            instance_id,
            getattr(runner, "_db_portfolio_id", None),
            getattr(getattr(runner, "portfolio", None), "portfolio_id", None),
        ):
            if owned:
                claims.add(str(owned))
        name = getattr(getattr(runner, "config", None), "name", None)
        if name:
            claims.add(f"{name} [{prefix}]")
    return claims


def is_claimed(row: Portfolio, claims: Set[str]) -> bool:
    """True when a live runner owns this ledger row."""
    if row.portfolio_id in claims or row.name in claims:
        return True
    match = _INSTANCE_SUFFIX.search(row.name or "")
    return bool(match) and match.group(1).lower() in claims


def reap_orphan_portfolio_rows(session: Any, claims: Set[str]) -> ReapReport:
    """Flip every unclaimed ``'active'`` portfolio row to ``'stopped'``.

    A status flip, never a delete: trades, positions and orders keep their
    foreign keys and their history. The caller owns the transaction.
    """
    rows = (
        session.scalars(select(Portfolio).where(Portfolio.status.in_(_REAP_FROM_STATUS))).all()
    )
    report = ReapReport(scanned=len(rows))
    orphans: List[Portfolio] = []
    for row in rows:
        if is_claimed(row, claims):
            report.kept.append(row.name)
        else:
            orphans.append(row)

    if orphans:
        ids = [row.portfolio_id for row in orphans]
        report.reaped_open_exposure_rows = int(
            session.scalar(
                select(func.count())
                .select_from(Position)
                .where(Position.portfolio_id.in_(ids), Position.status == "open")
            )
            or 0
        )
        for row in orphans:
            row.status = RETIRED_STATUS
            report.reaped.append(row.name)
            report.reaped_capital += Decimal(str(row.initial_capital))
    return report
