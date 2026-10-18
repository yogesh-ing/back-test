"""Append-only option intent journal (Options V1 PRD decision 8).

One row per trade decision and its outcome, written *next to* the order — not
in place of it. The journal answers the three questions the execution seam
cannot answer from memory after a crash:

* did we already submit this leg? (idempotency — ``order_id_for``)
* what did the broker last tell us about this intent? (reconciliation)
* how many intents did this book act on? (soak gate, ``count``)

Two properties make it usable as evidence:

**Deterministic ids.** :func:`intent_key` hashes the *decision* — strategy,
structure, underlying, expiry, legs and the bar clock the strategy acted on —
so the same decision replayed after a restart hashes to the same ``intent_id``.
A random uuid per attempt would make a duplicate submit undetectable, which is
the whole thing the journal exists to catch.

**Append-only.** There is no update or delete path on either implementation.
A row that is wrong is corrected by appending another event, so replay of the
journal reproduces the book — the PRD's "provably, not hopefully".

``MemoryJournal`` is the default for a process that has not been given a
database. It has the same API and semantics but dies with the process, so a
live runner logs loudly when it is the only journal attached.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.exc import IntegrityError

from backtest.db.models import Base, OptionIntentEvent, OptionIntentKind
from backtest.strategy.intent import TradeIntent

if TYPE_CHECKING:
    from backtest.db.manager import DatabaseManager

logger = logging.getLogger("backtest.options.intent_journal")

#: Event kinds — re-exported from the ORM enum so SQL and Python cannot drift.
KIND_DECIDED = OptionIntentKind.DECIDED.value
KIND_SUBMITTED = OptionIntentKind.SUBMITTED.value
KIND_FILLED = OptionIntentKind.FILLED.value
KIND_REJECTED = OptionIntentKind.REJECTED.value
KIND_UNCERTAIN = OptionIntentKind.UNCERTAIN.value
KIND_RECONCILED = OptionIntentKind.RECONCILED.value
KIND_HALTED = OptionIntentKind.HALTED.value
KIND_EXECUTED = OptionIntentKind.EXECUTED.value

ALL_KINDS = frozenset(kind.value for kind in OptionIntentKind)


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def _stamp(value: Any) -> str:
    """Normalise a clock value to a stable string (``None`` reads as empty)."""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def intent_key(
    intent: TradeIntent,
    *,
    action: str = "open",
    clock: Any = None,
) -> str:
    """Deterministic id for one decision — the journal's idempotency key.

    Derived from the decision itself, never from ``uuid4``: two attempts at
    the *same* decision must collide so a replay can be recognised and
    refused. ``leg`` order is part of the hash (a spread's legs are not
    interchangeable).

    ``clock`` is the decision's bar timestamp. It defaults to
    ``intent.view.bar_timestamp``; a caller acting on a snapshot without a bar
    clock may pass its own — that keeps distinct decisions distinct, which a
    missing clock would not.
    """
    view = intent.view
    when = _stamp(view.bar_timestamp if view.bar_timestamp is not None else clock)
    parts: list[str] = [
        action,
        intent.strategy_name or "",
        intent.structure_type,
        str(view.underlying),
        _stamp(intent.expiry),
        when,
    ]
    for leg in intent.legs:
        parts.extend(
            (
                str(leg.trading_symbol or leg.instrument_token),
                str(leg.side).upper(),
                str(leg.total_quantity),
            )
        )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def dedupe_key(intent_id: str, kind: str, leg_index: int | None = None) -> str:
    """The unique key for one event — ``intent_id`` + kind (+ leg)."""
    if leg_index is None:
        return f"{intent_id}:{kind}"
    return f"{intent_id}:{kind}:{leg_index}"


def decision_key(action: str, subject: str, *, code: str = "", clock: Any = None) -> str:
    """Deterministic id for a decision that never produced an executable intent.

    Pre-trade halts (an invalid contract, a DTE block, a funds reject) and
    structure closes have no :class:`TradeIntent` to hash, but they belong in the
    same append-only record — the PRD wants every rejection code reportable.
    ``action`` + the thing acted on + the reason + the bar clock identify it, so
    one halt on one bar records once however often the bar is replayed.
    """
    parts = ["decision", action, str(subject), code or "", _stamp(clock)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentEvent:
    """One journal row as read back (never written directly — use ``append``)."""

    intent_id: str
    kind: str
    leg_index: Optional[int] = None
    structure_id: Optional[str] = None
    strategy_name: Optional[str] = None
    structure_type: Optional[str] = None
    underlying: Optional[str] = None
    broker_order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)
    recorded_at: Optional[datetime] = None

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.intent_id, self.kind, self.leg_index)


def _row_to_event(row: OptionIntentEvent) -> IntentEvent:
    return IntentEvent(
        intent_id=row.intent_id,
        kind=row.kind,
        leg_index=row.leg_index,
        structure_id=row.structure_id,
        strategy_name=row.strategy_name,
        structure_type=row.structure_type,
        underlying=row.underlying,
        broker_order_id=row.broker_order_id,
        client_order_id=row.client_order_id,
        detail=dict(row.detail or {}),
        recorded_at=row.recorded_at,
    )


# ---------------------------------------------------------------------------
# Database journal
# ---------------------------------------------------------------------------


class IntentJournal:
    """Append-only journal backed by ``option_intent_events``.

    Every write is an INSERT. A write whose ``dedupe_key`` already exists is a
    no-op returning ``False`` — the unique index absorbs the duplicate rather
    than raising, so a caller can use ``append`` as the guard itself.
    """

    def __init__(self, manager: "DatabaseManager") -> None:
        self.manager = manager

    # -- schema ----------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create ``option_intent_events`` if absent (idempotent, table-scoped).

        Production databases get the table from migration 020; this keeps
        fresh SQLite dev/test databases working without a migration run.
        """
        Base.metadata.create_all(
            self.manager.engine, tables=[OptionIntentEvent.__table__]
        )

    # -- write -----------------------------------------------------------

    def append(
        self,
        *,
        intent_id: str,
        kind: str,
        leg_index: int | None = None,
        structure_id: str | None = None,
        strategy_name: str | None = None,
        structure_type: str | None = None,
        underlying: str | None = None,
        broker_order_id: str | None = None,
        client_order_id: str | None = None,
        detail: dict[str, Any] | None = None,
        dedupe: bool = True,
    ) -> bool:
        """Insert one event; ``False`` when its dedupe key already exists."""
        if kind not in ALL_KINDS:
            raise ValueError(f"unknown journal kind {kind!r}")
        key = dedupe_key(intent_id, kind, leg_index) if dedupe else None
        try:
            with self.manager.session() as session:
                if key is not None and session.query(OptionIntentEvent).filter_by(
                    dedupe_key=key
                ).first():
                    return False
                session.add(
                    OptionIntentEvent(
                        intent_id=intent_id,
                        kind=kind,
                        dedupe_key=key,
                        leg_index=leg_index,
                        structure_id=structure_id,
                        strategy_name=strategy_name,
                        structure_type=structure_type,
                        underlying=underlying,
                        broker_order_id=None if broker_order_id is None else str(broker_order_id),
                        client_order_id=client_order_id,
                        detail=dict(detail or {}),
                    )
                )
            return True
        except IntegrityError:
            # Two writers raced on the same key — the first one is the record.
            logger.debug("[journal] duplicate %s absorbed", key)
            return False

    def append_for_intent(
        self,
        intent: TradeIntent,
        kind: str,
        *,
        action: str = "open",
        clock: Any = None,
        leg_index: int | None = None,
        structure_id: str | None = None,
        broker_order_id: str | None = None,
        client_order_id: str | None = None,
        detail: dict[str, Any] | None = None,
        dedupe: bool = True,
    ) -> str:
        """Convenience write: hash the intent, stamp its labels, append.

        Returns the ``intent_id`` so the caller can thread it through later
        events without recomputing the key.
        """
        intent_id = intent_key(intent, action=action, clock=clock)
        self.append(
            intent_id=intent_id,
            kind=kind,
            leg_index=leg_index,
            structure_id=structure_id,
            strategy_name=intent.strategy_name or None,
            structure_type=intent.structure_type,
            underlying=str(intent.view.underlying),
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            detail=detail,
            dedupe=dedupe,
        )
        return intent_id

    # -- read ------------------------------------------------------------

    def events(self, intent_id: str) -> list[IntentEvent]:
        """Every event for one intent, oldest first."""
        with self.manager.session() as session:
            rows = (
                session.query(OptionIntentEvent)
                .filter_by(intent_id=intent_id)
                .order_by(OptionIntentEvent.event_id.asc())
                .all()
            )
            return [_row_to_event(row) for row in rows]

    def kinds(self, intent_id: str) -> set[str]:
        with self.manager.session() as session:
            rows = (
                session.query(OptionIntentEvent.kind)
                .filter_by(intent_id=intent_id)
                .distinct()
                .all()
            )
            return {row[0] for row in rows}

    def has(self, intent_id: str, kind: str, leg_index: int | None = None) -> bool:
        key = dedupe_key(intent_id, kind, leg_index)
        with self.manager.session() as session:
            return (
                session.query(OptionIntentEvent)
                .filter_by(dedupe_key=key)
                .first()
                is not None
            )

    def order_id_for(self, intent_id: str, leg_index: int | None = None) -> str | None:
        """Broker order id already recorded for this leg, if it was submitted.

        The idempotency read: a non-``None`` answer means the ticket is
        already at the broker and the caller must reconcile, never re-place.
        """
        with self.manager.session() as session:
            row = (
                session.query(OptionIntentEvent)
                .filter_by(
                    intent_id=intent_id,
                    kind=KIND_SUBMITTED,
                    leg_index=leg_index,
                )
                .order_by(OptionIntentEvent.event_id.asc())
                .first()
            )
            return row.broker_order_id if row is not None else None

    def count(self, kind: str | None = None) -> int:
        """Soak gate counter (PRD §5 phase 1: "100% intent→fill→journal")."""
        with self.manager.session() as session:
            query = session.query(OptionIntentEvent)
            if kind is not None:
                query = query.filter_by(kind=kind)
            return query.count()

    def recent(self, limit: int = 50) -> list[IntentEvent]:
        with self.manager.session() as session:
            rows = (
                session.query(OptionIntentEvent)
                .order_by(OptionIntentEvent.event_id.desc())
                .limit(limit)
                .all()
            )
            return [_row_to_event(row) for row in reversed(rows)]


# ---------------------------------------------------------------------------
# Process-local journal (default when no database was wired in)
# ---------------------------------------------------------------------------


class MemoryJournal:
    """Same API and semantics as :class:`IntentJournal`, dies with the process.

    Not evidence: a restart loses it, so it cannot reconcile an uncertain
    submit across a crash. Live execution therefore logs a warning the first
    time it places an order on this journal — the operator sees that the run
    has no durable intent record.
    """

    def __init__(self) -> None:
        self._events: list[IntentEvent] = []
        self._keys: set[str] = set()
        self._warned = False

    def ensure_schema(self) -> None:  # pragma: no cover - nothing to create
        return None

    def append(
        self,
        *,
        intent_id: str,
        kind: str,
        leg_index: int | None = None,
        structure_id: str | None = None,
        strategy_name: str | None = None,
        structure_type: str | None = None,
        underlying: str | None = None,
        broker_order_id: str | None = None,
        client_order_id: str | None = None,
        detail: dict[str, Any] | None = None,
        dedupe: bool = True,
    ) -> bool:
        if kind not in ALL_KINDS:
            raise ValueError(f"unknown journal kind {kind!r}")
        key = dedupe_key(intent_id, kind, leg_index) if dedupe else None
        if key is not None and key in self._keys:
            return False
        if key is not None:
            self._keys.add(key)
        self._events.append(
            IntentEvent(
                intent_id=intent_id,
                kind=kind,
                leg_index=leg_index,
                structure_id=structure_id,
                strategy_name=strategy_name,
                structure_type=structure_type,
                underlying=underlying,
                broker_order_id=None if broker_order_id is None else str(broker_order_id),
                client_order_id=client_order_id,
                detail=dict(detail or {}),
            )
        )
        return True

    def append_for_intent(
        self,
        intent: TradeIntent,
        kind: str,
        *,
        action: str = "open",
        clock: Any = None,
        leg_index: int | None = None,
        structure_id: str | None = None,
        broker_order_id: str | None = None,
        client_order_id: str | None = None,
        detail: dict[str, Any] | None = None,
        dedupe: bool = True,
    ) -> str:
        intent_id = intent_key(intent, action=action, clock=clock)
        self.append(
            intent_id=intent_id,
            kind=kind,
            leg_index=leg_index,
            structure_id=structure_id,
            strategy_name=intent.strategy_name or None,
            structure_type=intent.structure_type,
            underlying=str(intent.view.underlying),
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            detail=detail,
            dedupe=dedupe,
        )
        return intent_id

    def events(self, intent_id: str) -> list[IntentEvent]:
        return [event for event in self._events if event.intent_id == intent_id]

    def kinds(self, intent_id: str) -> set[str]:
        return {event.kind for event in self._events if event.intent_id == intent_id}

    def has(self, intent_id: str, kind: str, leg_index: int | None = None) -> bool:
        return dedupe_key(intent_id, kind, leg_index) in self._keys

    def order_id_for(self, intent_id: str, leg_index: int | None = None) -> str | None:
        for event in self._events:
            if (
                event.intent_id == intent_id
                and event.kind == KIND_SUBMITTED
                and event.leg_index == leg_index
            ):
                return event.broker_order_id
        return None

    def count(self, kind: str | None = None) -> int:
        if kind is None:
            return len(self._events)
        return sum(1 for event in self._events if event.kind == kind)

    def recent(self, limit: int = 50) -> list[IntentEvent]:
        return self._events[-limit:]

    def warn_if_live(self) -> None:
        """Loud, once: live orders are going out with no durable intent record."""
        if self._warned:
            return
        self._warned = True
        logger.warning(
            "[journal] LIVE orders on a process-local journal — an intent "
            "submitted before a crash cannot be recognised or reconciled "
            "after it. Wire IntentJournal(manager) before enabling live."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def journal_from_env() -> Any:
    """Best-effort durable journal, mirroring options persistence wiring.

    ``OPTIONS_PERSISTENCE=off`` (or ``OPTIONS_INTENT_JOURNAL=off``) forces the
    process-local journal — the switch tests use to stay off the database. The
    default attaches the configured forward-testing database when reachable and
    degrades to :class:`MemoryJournal` otherwise, with a warning: a missing
    database must never take a runner down, but it must be visible.
    """
    for var in ("OPTIONS_INTENT_JOURNAL", "OPTIONS_PERSISTENCE"):
        mode = os.getenv(var, "").strip().lower()
        if mode in ("0", "off", "false", "no"):
            logger.info("[journal] %s=%s — using the process-local journal", var, mode)
            return MemoryJournal()
    try:
        from backtest.db import DatabaseManager

        manager = DatabaseManager.from_env()
        manager.connect()
        journal = IntentJournal(manager)
        journal.ensure_schema()
        logger.info("[journal] intent journal attached (%s)", manager.config.safe_url)
        return journal
    except Exception:  # noqa: BLE001 — the journal is best-effort by default
        logger.warning(
            "[journal] database unavailable — intents will be journaled in "
            "process memory only and will not survive a restart",
            exc_info=True,
        )
        return MemoryJournal()
