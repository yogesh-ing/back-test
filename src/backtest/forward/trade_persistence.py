"""Live trade persistence — closed trades → PostgreSQL (GAP-4, 2026-09-21).

The live portfolio's books are in-memory (`PortfolioManager`); the JSON state
store (`PORTFOLIO_STATE_PATH`) snapshots the *tail* for restart survival. This
module is the PERMANENT layer: every closed trade (equity round-trip or option
structure) lands in the `trades` table via the existing
:class:`backtest.db.manager.DatabaseManager` — the same schema the backtest
engine writes, so cross-session history accumulates in one place.

Design rules (matching the platform's honesty/fail-soft conventions):
* Persistence must NEVER break trading — every failure is logged and swallowed.
* Idempotent per ROUND TRIP, not per process: the in-memory ``_flushed`` set
  is only a fast path. The durable guard is :func:`trade_dedupe_key` — the
  ``trades`` row's identity columns are looked up before every insert, so a
  restart that restores a runner's closed-trades cache re-writes nothing
  (B1, QA 2026-10-30: 6,476 raw rows for 2,878 real trades, ₹2,23,635.74 of
  phantom loss, 16 copies of one fill).
* Synthetic-testable: accepts any duck-typed DatabaseManager; unit tests use
  in-memory SQLite.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from backtest.data.sources_policy import default_broker_source
from backtest.timekeeping import from_stored_utc, iso_utc, now_utc

logger = logging.getLogger(__name__)

#: The columns that make a round trip *this* round trip — the same tuple the
#: 2026-10-30 duplicate audit grouped on, so the guard and a backfill of the
#: existing rows agree about what "already persisted" means. P&L is
#: deliberately absent: a corrected fee must not look like a new trade.
DEDUPE_COLUMNS = (
    "portfolio_id",
    "symbol",
    "direction",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "quantity",
)

#: Unit separator — cannot appear in a symbol or an ISO timestamp.
_KEY_JOINER = "\x1f"


def _canonical(value: Any) -> str:
    """Render one identity column the same way it is stored.

    Timestamps go through :func:`iso_utc` so a trade cached as naive UTC and a
    row stored as ``+05:30`` hash identically, and prices/quantities to the
    column's 8 decimals so ``154.0`` and ``154.00000000`` are one value.
    """
    if isinstance(value, datetime):
        return iso_utc(value) or ""
    if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
        return f"{Decimal(str(value)):.8f}"
    return str(value or "").strip()


def trade_dedupe_key(values: Mapping[str, Any]) -> str:
    """Deterministic 64-hex id for one closed round trip.

    Built from :data:`DEDUPE_COLUMNS` only, so the same trade produces the same
    key whether it is read from the runner's cache or from a stored row — which
    is what lets ``scripts/repair_trade_duplicates.py`` backfill history.
    """
    missing = [c for c in DEDUPE_COLUMNS if c not in values]
    if missing:
        raise KeyError(f"trade identity columns missing: {missing}")
    joined = _KEY_JOINER.join(_canonical(values[c]) for c in DEDUPE_COLUMNS)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class LiveTradePersister:
    """Flushes each runner's newly-closed trades into the DB ``trades`` table."""

    def __init__(self, db_manager: Any) -> None:
        self.db = db_manager
        #: runner.instance_id → set of (exit_ts, symbol, pnl) already written
        self._flushed: dict[str, set[tuple[str, str, str]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    #: Sources whose trades must NEVER reach the permanent DB. Synthetic/
    # replay data produces fake fills — persisting them would pollute the
    # real cross-session trade history (owner decision 2026-09-22, mid-session:
    # "we should not be storing trades which are running or may run on
    # synthetic data to DB"). Same trust rule as the live bucket's
    # fail-closed data gate: only REAL market data earns permanent storage.
    NON_PERSISTENT_SOURCES = frozenset({"synthetic", "replay"})

    def flush_runner(self, runner: Any) -> int:
        """Write runner's not-yet-persisted closed trades. Returns count.

        Runners on synthetic/replay data are skipped entirely — their fills
        are simulations on simulated prices and would contaminate the real
        trade history. Their books still live in the in-memory state + JSON
        snapshot like before; only mstock (real market data) runners persist.
        """
        if self.db is None:
            return 0
        # Data-trust gate FIRST — cheap check, no work for fake-data runners.
        source = str(getattr(getattr(runner, "config", None), "source", "") or "").lower()
        if source in self.NON_PERSISTENT_SOURCES:
            return 0
        try:
            trades = list(runner.closed_trades)
        except Exception:  # noqa: BLE001 — never break the tick
            return 0
        if not trades:
            return 0

        seen = self._flushed.setdefault(runner.instance_id, set())
        pending = []
        for t in trades:
            key = (
                str(t.get("exit_ts") or ""),
                str(t.get("symbol") or ""),
                str(t.get("pnl") or ""),
            )
            if key in seen:
                continue
            pending.append((t, key))
        if not pending:
            return 0

        written = 0
        skipped = 0
        for t, key in pending:
            try:
                portfolio_id = self._ensure_portfolio_row(runner)
                values = self._trade_values(portfolio_id, runner, t)
                # The durable guard runs before every insert: ``_flushed`` died
                # with the last process, the DB did not.
                if self._already_persisted(values):
                    seen.add(key)
                    skipped += 1
                    continue
                values["dedupe_key"] = trade_dedupe_key(values)
                self._insert_trade(values)
                seen.add(key)
                written += 1
            except Exception:  # noqa: BLE001 — one bad trade must not stop the rest
                logger.exception(
                    "trade persist failed for runner %s (%s)", runner.instance_id, t.get("symbol")
                )
        if written or skipped:
            logger.info(
                "[db] runner %s: persisted %d closed trade(s), %d already in history",
                runner.instance_id,
                written,
                skipped,
            )
        return written

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_portfolio_row(self, runner: Any) -> str:
        """One `portfolios` row per runner instance (created once, cached)."""
        cached = getattr(runner, "_db_portfolio_id", None)
        if cached:
            return cached

        from backtest.db.models import Portfolio as PortfolioRow

        cfg = runner.config
        name = f"{cfg.name} [{runner.instance_id[:8]}]"
        with self.db.session() as session:
            existing = (
                session.query(PortfolioRow).filter(PortfolioRow.name == name).one_or_none()
            )
            if existing is not None:
                runner._db_portfolio_id = existing.portfolio_id
                return existing.portfolio_id
            row = PortfolioRow(
                name=name,
                initial_capital=Decimal(str(cfg.allocated_capital)),
                current_cash=Decimal(
                    str(getattr(runner.portfolio, "current_cash", 0)
                        or getattr(runner.portfolio, "cash", 0)
                        or 0)
                ),
                mode="paper",
                # Never a literal feed name: the runner's own source is already
                # policy-resolved at construction, and if a legacy config
                # somehow carries none, this writes the deployment's broker
                # feed rather than stamping "synthetic" on a real book.
                source=str(getattr(cfg, "source", "") or default_broker_source()),
            )
            session.add(row)
            session.flush()
            runner._db_portfolio_id = row.portfolio_id
            return row.portfolio_id

    def _trade_values(self, portfolio_id: str, runner: Any, t: dict[str, Any]) -> dict[str, Any]:
        """One closed trade as the exact column values that will be written.

        Returned as a dict rather than an ORM row so the identity columns can be
        hashed into :func:`trade_dedupe_key` and queried for existence before
        anything is inserted.
        """
        entry_ts = _parse_ts(t.get("entry_ts"))
        exit_ts = _parse_ts(t.get("exit_ts")) or now_utc()
        if entry_ts is None:
            entry_ts = exit_ts
        # The runner's book is the truth for a round trip: it knows the price
        # P&L, the brokerage and the venue/statutory charges separately. Until
        # BUG-26 the equity flow reported only one number, which the writer had
        # to copy into both P&L columns — so a costed loss read as a gross one.
        gross_raw = t.get("gross_pnl")
        gross = Decimal(str(gross_raw if gross_raw is not None else t.get("pnl") or 0))
        commission = Decimal(str(t.get("commission") or 0))
        statutory = Decimal(str(t.get("statutory_fees") or 0))
        costs = commission + statutory
        net_raw = t.get("net_pnl")
        net = Decimal(str(net_raw)) if net_raw is not None else gross - costs
        qty = Decimal(str(t.get("units") or t.get("qty") or 1))
        entry_px = Decimal(str(t.get("entry_price") or 0))
        exit_px = Decimal(str(t.get("exit_price") or 0))
        holding_min = max(int((exit_ts - entry_ts).total_seconds() // 60), 0)
        return {
            "portfolio_id": portfolio_id,
            "symbol": str(t.get("symbol") or "?")[:64],
            "strategy_name": str(runner.config.strategy_name)[:64],
            "direction": "long" if str(t.get("side", "LONG")).upper() == "LONG" else "short",
            "quantity": abs(qty),
            "entry_price": abs(entry_px),
            "exit_price": abs(exit_px),
            "entry_time": entry_ts,
            "exit_time": exit_ts,
            "gross_pnl": gross,
            "net_pnl": net,
            "commission_total": costs,
            "holding_period_minutes": holding_min,
            "exit_reason": _map_exit_reason(t.get("exit_reason")),
        }

    def _already_persisted(self, values: Mapping[str, Any]) -> bool:
        """Has this exact round trip already been written?

        Matched on the identity columns, not on ``dedupe_key``, because the rows
        the 2026-10-30 duplicates were measured against predate the key column —
        a key-only lookup would have re-inserted all of them on the first
        restart after this shipped.
        """
        from backtest.db.models import Trade as TradeRow

        with self.db.session() as session:
            found = (
                session.query(TradeRow.trade_id)
                .filter(
                    TradeRow.portfolio_id == values["portfolio_id"],
                    TradeRow.symbol == values["symbol"],
                    TradeRow.direction == values["direction"],
                    TradeRow.entry_time == values["entry_time"],
                    TradeRow.exit_time == values["exit_time"],
                    TradeRow.entry_price == values["entry_price"],
                    TradeRow.exit_price == values["exit_price"],
                    TradeRow.quantity == values["quantity"],
                )
                .first()
            )
        return found is not None

    def _insert_trade(self, values: Mapping[str, Any]) -> None:
        from backtest.db.models import Trade as TradeRow

        with self.db.session() as session:
            session.add(TradeRow(**values))


def _parse_ts(value: Any) -> datetime | None:
    """Trade-record timestamp → aware UTC.

    The previous version parsed any ISO string and then *deleted* its offset
    with ``replace(tzinfo=None)``. The ``trades`` columns are ``TIMESTAMPTZ``
    and this deployment's session timezone is ``Asia/Calcutta``, so Postgres
    reinterpreted every naive value as IST: a UTC entry silently lost 5h30
    while an IST bar exit stayed put. That is what wrote the 330-minute
    "holding periods" of 2026-10-27.

    Aware values pass through as the instants they are. A naive one is read as
    UTC via :func:`from_stored_utc` — the door for stamps this application wrote
    with ``utcnow()`` — rather than refused, because losing a closed trade from
    the permanent history is worse for a trader than having its clock labelled;
    ``tests/test_timekeeping_policy.py`` is what keeps new writers aware.
    """
    return from_stored_utc(value)


def _map_exit_reason(raw: Any) -> str | None:
    """Runner exit reasons → the ``ExitReason`` enum's allowed values.

    The DB enforces a check constraint over ExitReason (signal / stop_loss /
    take_profit / trailing_stop / time_stop / risk_limit / manual / eod_flat).
    Runner reasons are free-form ("target", "stop", "expiry"…) — map the
    known ones, fall back to "signal" for anything unrecognised.
    """
    r = str(raw or "").strip().lower()
    if not r:
        return None
    # BUG-39: the wall-clock square-off books ``intraday_squareoff``, which matched
    # none of the tokens below and landed in the history as "signal" — a risk
    # control showing up as a strategy exit.
    if "squareoff" in r or "square_off" in r or "square off" in r:
        return "eod_flat"
    if "target" in r or "profit" in r:
        return "take_profit"
    if "stop" in r or "sl" in r:
        return "stop_loss"
    if "trail" in r:
        return "trailing_stop"
    if "time" in r or "eod" in r:
        return "time_stop"
    if "expiry" in r or "settle" in r:
        return "eod_flat"
    if "risk" in r or "breaker" in r:
        return "risk_limit"
    if "manual" in r:
        return "manual"
    return "signal"
