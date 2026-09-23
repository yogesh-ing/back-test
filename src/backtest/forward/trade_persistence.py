"""Live trade persistence — closed trades → PostgreSQL (GAP-4, 2026-09-21).

The live portfolio's books are in-memory (`PortfolioManager`); the JSON state
store (`PORTFOLIO_STATE_PATH`) snapshots the *tail* for restart survival. This
module is the PERMANENT layer: every closed trade (equity round-trip or option
structure) lands in the `trades` table via the existing
:class:`backtest.db.manager.DatabaseManager` — the same schema the backtest
engine writes, so cross-session history accumulates in one place.

Design rules (matching the platform's honesty/fail-soft conventions):
* Persistence must NEVER break trading — every failure is logged and swallowed.
* Idempotent per runner: each runner tracks which exit_ts+symbols it has
  already flushed (in-memory set; duplicates are skipped by a natural key).
* Synthetic-testable: accepts any duck-typed DatabaseManager; unit tests use
  in-memory SQLite.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

#: Runner-level dedupe key length — a natural key of exit_ts+symbol+pnl is
#: enough to catch the double-flush case (same trade seen on two sweeps).
_NATURAL_KEY_LEN = 3


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
        for t, key in pending:
            try:
                portfolio_id = self._ensure_portfolio_row(runner)
                self._insert_trade(portfolio_id, runner, t)
                seen.add(key)
                written += 1
            except Exception:  # noqa: BLE001 — one bad trade must not stop the rest
                logger.exception(
                    "trade persist failed for runner %s (%s)", runner.instance_id, t.get("symbol")
                )
        if written:
            logger.info(
                "[db] persisted %d closed trade(s) for runner %s", written, runner.instance_id
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
                source=str(getattr(cfg, "source", "synthetic") or "synthetic"),
            )
            session.add(row)
            session.flush()
            runner._db_portfolio_id = row.portfolio_id
            return row.portfolio_id

    def _insert_trade(self, portfolio_id: str, runner: Any, t: dict[str, Any]) -> None:
        from backtest.db.models import Trade as TradeRow

        entry_ts = _parse_ts(t.get("entry_ts"))
        exit_ts = _parse_ts(t.get("exit_ts")) or datetime.utcnow()
        if entry_ts is None:
            entry_ts = exit_ts
        pnl = Decimal(str(t.get("pnl") or 0))
        qty = Decimal(str(t.get("units") or t.get("qty") or 1))
        entry_px = Decimal(str(t.get("entry_price") or 0))
        exit_px = Decimal(str(t.get("exit_price") or 0))
        holding_min = max(int((exit_ts - entry_ts).total_seconds() // 60), 0)
        exit_reason = _map_exit_reason(t.get("exit_reason"))

        with self.db.session() as session:
            session.add(
                TradeRow(
                    portfolio_id=portfolio_id,
                    symbol=str(t.get("symbol") or "?")[:64],
                    strategy_name=str(runner.config.strategy_name)[:64],
                    direction="long" if str(t.get("side", "LONG")).upper() == "LONG" else "short",
                    quantity=abs(qty),
                    entry_price=abs(entry_px),
                    exit_price=abs(exit_px),
                    entry_time=entry_ts,
                    exit_time=exit_ts,
                    gross_pnl=pnl,
                    net_pnl=pnl,
                    commission_total=Decimal(str(t.get("commission") or 0)),
                    holding_period_minutes=holding_min,
                    exit_reason=exit_reason,
                )
            )


def _parse_ts(value: Any) -> datetime | None:
    """ISO string / datetime → naive datetime (DB column is tz-aware-capable)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


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
