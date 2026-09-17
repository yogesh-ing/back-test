"""Portfolio command-center state persistence — Gap #3 / "V2" (P2.4).

The restart-survival gap: every runner config, every book, and every bucket
circuit-breaker latch used to live in process memory, so a restart lost the
book and *resurrected halted breakers*. This module is the smallest honest
fix, following the house pattern (the walk-forward runner's ``save_state`` /
``load_state`` JSON files; ``PLAYBOOKS_PATH``):

* :class:`PortfolioStateStore` — one versioned JSON file, **atomic writes**
  (tmp + ``os.replace`` — a crash mid-write never corrupts the last good
  state), corruption/schema-mismatch-safe loads (warn + ignore, boot clean).
* :func:`capture_runner` / :func:`restore_runner` — the full runner book via
  ``Portfolio.to_dict``/``from_dict`` (positions, closed positions, equity
  history AND the order ledger — the "Step 20" snapshot format, built for
  exactly this and finally wired in), plus runtime scalars and, for option
  runners, the complete option book (``OptionPosition``/``StructurePosition``
  snapshots + bridge bar-clock scalars).
* The manager (``portfolio_manager.PortfolioManager``) opts in via
  ``state_path`` (env ``PORTFOLIO_STATE_PATH``); with no path configured,
  behaviour is byte-for-byte the old in-memory V1.

Restore semantics (fail-closed): a runner persisted as RUNNING comes back
**PAUSED** — after a crash/restart nothing trades until a human resumes it.
The book, capital, anchors and breaker latches are exactly as left; only
*who acts next* requires an explicit resume.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import fields as dataclass_fields
from datetime import date, datetime
from enum import Enum
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("backtest.forward.state_store")

__all__ = [
    "PortfolioStateStore",
    "STATE_SCHEMA_VERSION",
    "capture_runner",
    "restore_runner",
    "config_to_dict",
]

#: Bump when the snapshot shape changes; loads of older files are refused
#: (warn + start clean) rather than mis-rehydrated.
STATE_SCHEMA_VERSION = 1

#: Tails kept for the append-only runner histories (bounded file size).
_HISTORY_TAIL = 500


class PortfolioStateStore:
    """Atomic, versioned JSON persistence for the portfolio manager state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- write ---------------------------------------------------------------

    def save(self, payload: Dict[str, Any]) -> None:
        """Write ``payload`` atomically (tmp file + ``os.replace``)."""
        payload = {"schema": STATE_SCHEMA_VERSION, "saved_at": _now_iso(), **payload}
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                json.dump(payload, tmp, indent=2)
            os.replace(tmp_name, self.path)
        except Exception:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:  # pragma: no cover
                    pass
            raise

    # -- read ----------------------------------------------------------------

    def load(self) -> Optional[Dict[str, Any]]:
        """The stored payload, or ``None`` (missing / corrupt / wrong schema).

        A bad file NEVER blocks a boot: it is renamed aside (``.corrupt``)
        so the failure is visible and the next save starts clean.
        """
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            logger.error(
                "state file %s unreadable (%s) — renaming aside and starting clean",
                self.path, exc,
            )
            try:
                self.path.rename(self.path.with_suffix(self.path.suffix + ".corrupt"))
            except OSError:  # pragma: no cover
                pass
            return None
        if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA_VERSION:
            logger.error(
                "state file %s schema %r != %s — ignoring (rename aside manually "
                "if you need it)",
                self.path, payload.get("schema") if isinstance(payload, dict) else "?",
                STATE_SCHEMA_VERSION,
            )
            return None
        return payload

    def clear(self) -> None:
        """Delete the state file (fresh start)."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            logger.warning("could not remove state file %s", self.path)


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------


def config_to_dict(config: Any) -> Dict[str, Any]:
    """``RunnerConfig`` → JSON dict (dataclass fields, forward-compatible)."""
    return {f.name: getattr(config, f.name) for f in dataclass_fields(config)}


def _config_from_dict(payload: Dict[str, Any]) -> Any:
    from backtest.forward.paper_runner import RunnerConfig

    known = {f.name for f in dataclass_fields(RunnerConfig)}
    unknown = sorted(set(payload) - known)
    if unknown:
        logger.warning("ignoring unknown runner-config keys from state: %s", unknown)
    return RunnerConfig(**{k: v for k, v in payload.items() if k in known})


# ---------------------------------------------------------------------------
# Option book serializers (paper_trading dataclasses — the state-file format)
# ---------------------------------------------------------------------------


def _opt_position_to_snapshot(p: Any) -> Dict[str, Any]:
    return {
        "position_id": p.position_id,
        "structure_id": p.structure_id,
        "strategy_name": p.strategy_name,
        "instrument_token": p.instrument_token,
        "trading_symbol": p.trading_symbol,
        "underlying": p.underlying,
        "option_type": p.option_type,
        "strike": str(p.strike),
        "expiry": p.expiry.isoformat() if p.expiry else None,
        "lot_size": p.lot_size,
        "side": p.side,
        "quantity": p.quantity,
        "entry_price": str(p.entry_price),
        "current_price": str(p.current_price),
        "status": p.status.value,
        "realized_pnl": str(p.realized_pnl),
        "unrealized_pnl": str(p.unrealized_pnl),
        "commission": str(p.commission),
    }


def _opt_position_from_snapshot(payload: Dict[str, Any]) -> Any:
    from backtest.options.paper_trading import OptionPosition, PositionStatus

    return OptionPosition(
        position_id=payload["position_id"],
        structure_id=payload.get("structure_id", ""),
        strategy_name=payload.get("strategy_name", ""),
        instrument_token=payload.get("instrument_token", ""),
        trading_symbol=payload.get("trading_symbol", ""),
        underlying=payload.get("underlying", ""),
        option_type=payload.get("option_type", ""),
        strike=Decimal(payload.get("strike", "0")),
        expiry=(
            date.fromisoformat(payload["expiry"]) if payload.get("expiry") else None
        ),
        lot_size=int(payload.get("lot_size", 25)),
        side=payload.get("side", ""),
        quantity=int(payload.get("quantity", 0)),
        entry_price=Decimal(payload.get("entry_price", "0")),
        current_price=Decimal(payload.get("current_price", "0")),
        status=PositionStatus(payload.get("status", "open")),
        realized_pnl=Decimal(payload.get("realized_pnl", "0")),
        unrealized_pnl=Decimal(payload.get("unrealized_pnl", "0")),
        commission=Decimal(payload.get("commission", "0")),
    )


def _structure_to_snapshot(s: Any) -> Dict[str, Any]:
    return {
        "structure_id": s.structure_id,
        "structure_type": s.structure_type,
        "strategy_name": s.strategy_name,
        "underlying": s.underlying,
        "expiry": s.expiry.isoformat() if s.expiry else None,
        "opened_at": s.opened_at.isoformat() if s.opened_at else None,
        "closed_at": s.closed_at.isoformat() if s.closed_at else None,
        "exit_reason": s.exit_reason,
        "legs": [_opt_position_to_snapshot(leg) for leg in s.legs],
    }


def _structure_from_snapshot(payload: Dict[str, Any]) -> Any:
    from backtest.options.paper_trading import StructurePosition

    return StructurePosition(
        structure_id=payload["structure_id"],
        structure_type=payload.get("structure_type", ""),
        strategy_name=payload.get("strategy_name", ""),
        underlying=payload.get("underlying", ""),
        expiry=(
            date.fromisoformat(payload["expiry"]) if payload.get("expiry") else date.today()
        ),
        legs=[_opt_position_from_snapshot(leg) for leg in payload.get("legs", [])],
        opened_at=(
            datetime.fromisoformat(payload["opened_at"])
            if payload.get("opened_at")
            else datetime.utcnow()
        ),
        closed_at=(
            datetime.fromisoformat(payload["closed_at"])
            if payload.get("closed_at")
            else None
        ),
        exit_reason=payload.get("exit_reason"),
    )


# ---------------------------------------------------------------------------
# Bridge + runner capture / restore (attribute-level — no bridge code changes)
# ---------------------------------------------------------------------------

_BRIDGE_SCALARS = (
    "open_structure_id",
    "executed_count",
    "closed_count",
    "underlying",
    "_bar_index",
    "_entry_bar_index",
    "_exit_bar_index",
    "_bars_without_view",
    "_structure_direction",
    "_structure_expiry",
    "_entry_premium",
    "_settlement_count",
    "_reentries_today",
    "_reentry_day",
    "last_unrealized_pnl",
    "last_spot",
    "last_mtm_ts",
    "last_exit",
)


def capture_bridge(bridge: Any) -> Dict[str, Any]:
    """``OptionsBridge`` → snapshot (scalars + the broker's whole book)."""
    broker = bridge.option_broker
    structures = getattr(broker, "_structures", {})
    return {
        "scalars": {
            name: _to_json(getattr(bridge, name)) for name in _BRIDGE_SCALARS
        },
        "broker": {
            "capital": str(broker.capital),
            "available_cash": str(broker.available_cash),
            "statutory_fees_paid": str(getattr(broker, "_statutory_fees_paid", 0)),
            "structures": [
                _structure_to_snapshot(s) for s in list(structures.values())[-_HISTORY_TAIL:]
            ],
        },
    }


def restore_bridge(bridge: Any, snapshot: Dict[str, Any]) -> None:
    """Rehydrate bridge scalars + broker book (best-effort, logged)."""
    from decimal import Decimal as D

    broker = bridge.option_broker
    for name, value in snapshot.get("scalars", {}).items():
        if name in ("_entry_premium", "last_unrealized_pnl"):
            value = D(str(value or 0))
        setattr(bridge, name, value)
    book = snapshot.get("broker", {})
    if not book:
        return
    broker.capital = D(book.get("capital", str(broker.capital)))
    broker.available_cash = D(book.get("available_cash", str(broker.available_cash)))
    broker._statutory_fees_paid = D(book.get("statutory_fees_paid", "0"))
    structures: Dict[str, Any] = {}
    positions: Dict[str, Any] = {}
    for raw in book.get("structures", []):
        structure = _structure_from_snapshot(raw)
        structures[structure.structure_id] = structure
        for leg in structure.legs:
            positions[leg.position_id] = leg
    broker._structures = structures
    broker._positions = positions


def _to_json(value: Any) -> Any:
    """Decimals/dates/datetimes/Enums → JSON-safe scalars (snapshot allowlist)."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Runner capture / restore
# ---------------------------------------------------------------------------


def capture_runner(runner: Any) -> Dict[str, Any]:
    """One runner's config + full book + runtime scalars → JSON-safe dict."""
    cfg = config_to_dict(runner.config)
    # add_runner mints the instance id on the RUNNER (config keeps None) —
    # pin it into the captured config so restore re-creates the SAME identity.
    cfg["instance_id"] = runner.instance_id
    snapshot: Dict[str, Any] = {
        "config": cfg,
        "portfolio": runner.portfolio.to_dict(),
        "status": runner.status,
        "error": runner.error,
        "bars_processed": runner.bars_processed,
        "created_ts": runner.created_ts,
        "peak_equity": runner.peak_equity,
        "max_drawdown_pct": runner.max_drawdown_pct,
        "day_start_equity": runner._day_start_equity,
        "current_day": runner._current_day,
        "last_price": dict(runner.last_price),
        "last_bar_ts": dict(getattr(runner, "_last_bar_ts", {}) or {}),
        "last_option_pnl": runner.last_option_pnl,
        "closed_trades_cache": list(runner.closed_trades_cache)[-_HISTORY_TAIL:],
        "equity_curve": list(runner.equity_curve)[-_HISTORY_TAIL:],
    }
    if runner.options_bridge is not None:
        snapshot["bridge"] = capture_bridge(runner.options_bridge)
    return snapshot


def restore_runner(runner: Any, snapshot: Dict[str, Any]) -> None:
    """Rehydrate one (freshly constructed, NOT started) runner from a snapshot.

    Fail-closed status mapping: RUNNING → PAUSED (nothing trades after a
    restart until a human resumes); PAUSED/STOPPED → as saved.
    """
    from backtest.forward.paper_runner import STATUS_PAUSED, STATUS_RUNNING, STATUS_STOPPED
    from backtest.simulator.execution import free_executor
    from backtest.simulator.portfolio import Portfolio

    # Book — full round-trip (positions, closed, equity history, orders).
    restored = Portfolio.from_dict(snapshot["portfolio"])
    runner.portfolio = restored
    runner.executor = free_executor(restored)

    runner.bars_processed = int(snapshot.get("bars_processed", 0))
    runner.created_ts = snapshot.get("created_ts", runner.created_ts)
    runner.peak_equity = float(snapshot.get("peak_equity", runner.peak_equity))
    runner.max_drawdown_pct = float(snapshot.get("max_drawdown_pct", 0.0))
    runner._day_start_equity = float(snapshot.get("day_start_equity", restored.current_cash))
    runner._current_day = snapshot.get("current_day")
    runner.last_price = dict(snapshot.get("last_price", {}))
    runner._last_bar_ts = dict(snapshot.get("last_bar_ts", {}))
    runner.last_option_pnl = float(snapshot.get("last_option_pnl", 0.0))
    runner.closed_trades_cache = list(snapshot.get("closed_trades_cache", []))
    runner.equity_curve = list(snapshot.get("equity_curve", []))
    runner.error = snapshot.get("error")

    if runner.options_bridge is not None and snapshot.get("bridge"):
        restore_bridge(runner.options_bridge, snapshot["bridge"])

    saved_status = snapshot.get("status", STATUS_STOPPED)
    if saved_status == STATUS_RUNNING:
        runner.status = STATUS_PAUSED
    else:
        runner.status = (
            saved_status
            if saved_status in (STATUS_PAUSED, STATUS_STOPPED)
            else STATUS_STOPPED
        )


def _now_iso() -> str:
    return datetime.now().isoformat()
