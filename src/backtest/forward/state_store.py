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
The book, capital and anchors are exactly as left; only *who acts next*
requires an explicit resume. Breaker latches are the one exception: they are
written to the file but deliberately not re-armed, because a halt guards a
session's P&L and a restored latch trapped the dashboard in a permanent
"🔴 HALTED" (see ``PortfolioManager._apply_state_payload``).
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

from backtest.timekeeping import from_stored_utc, now_utc

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

    #: Boot backups kept before the oldest is pruned (bounded cruft).
    KEEP_BOOT_BACKUPS = 10

    def boot_backup(self) -> Optional[Path]:
        """Copy the file aside before this process gets a chance to rewrite it.

        Every save rebuilds the runner list from live memory, so a boot that
        restores less than the file held would otherwise overwrite history —
        the 2026-10-27 loss of five option runners. Parked snapshots cover the
        refusal case; this covers the rest (a restore that raised and booted
        clean, a runner that cannot be re-created at all). One call per boot.
        """
        if not self.path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.path.with_name(f"{self.path.name}.boot_{stamp}")
        try:
            target.write_bytes(self.path.read_bytes())
        except OSError as exc:  # pragma: no cover — a backup must not block boot
            logger.warning("boot backup of %s failed: %s", self.path, exc)
            return None
        backups = sorted(
            self.path.parent.glob(f"{self.path.name}.boot_*"),
            key=lambda p: p.name,
        )
        for stale in backups[: max(0, len(backups) - self.KEEP_BOOT_BACKUPS)]:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover
                pass
        return target


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
        "opened_at": p.opened_at.isoformat() if getattr(p, "opened_at", None) else None,
        "closed_at": p.closed_at.isoformat() if getattr(p, "closed_at", None) else None,
        "last_updated": p.last_updated.isoformat() if getattr(p, "last_updated", None) else None,
        "mtm_failures": int(getattr(p, "mtm_failures", 0) or 0),
        "last_quote_error": getattr(p, "last_quote_error", None),
    }


def _restore_ts(raw: Any) -> Optional[datetime]:
    """State-file timestamp → aware UTC (see
    :func:`backtest.timekeeping.from_stored_utc` for the rule)."""
    return from_stored_utc(raw)


def _opt_position_from_snapshot(payload: Dict[str, Any]) -> Any:
    from backtest.options.paper_trading import OptionPosition, PositionStatus

    opened_at = _restore_ts(payload.get("opened_at")) or now_utc()
    closed_at = _restore_ts(payload.get("closed_at"))
    last_updated = _restore_ts(payload.get("last_updated")) or opened_at
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
        opened_at=opened_at,
        closed_at=closed_at,
        last_updated=last_updated,
        mtm_failures=int(payload.get("mtm_failures", 0) or 0),
        last_quote_error=payload.get("last_quote_error"),
    )


def _structure_to_snapshot(s: Any) -> Dict[str, Any]:
    from backtest.options.paper_trading import _ist_session_date

    entry_session = getattr(s, "entry_session_date", None) or (
        _ist_session_date(s.opened_at) if s.opened_at else None
    )
    return {
        "structure_id": s.structure_id,
        "structure_type": s.structure_type,
        "strategy_name": s.strategy_name,
        "underlying": s.underlying,
        "expiry": s.expiry.isoformat() if s.expiry else None,
        "opened_at": s.opened_at.isoformat() if s.opened_at else None,
        "closed_at": s.closed_at.isoformat() if s.closed_at else None,
        "exit_reason": s.exit_reason,
        "session_date": getattr(s, "session_date", None),
        # BUG-18: both ends of the trip survive a restart, or an overnight
        # carry loses the session it was actually opened in. The anchor pair
        # goes with them: a mid-session restart that re-derives it from the
        # current mark would credit today's whole move to yesterday.
        "entry_session_date": entry_session,
        "last_mark_session": getattr(s, "last_mark_session", None),
        "last_mark_net_pnl": str(getattr(s, "last_mark_net_pnl", 0) or 0),
        "prior_close_net_pnl": str(getattr(s, "prior_close_net_pnl", 0) or 0),
        "statutory_fees": str(getattr(s, "statutory_fees", 0) or 0),
        "legs": [_opt_position_to_snapshot(leg) for leg in s.legs],
    }


def _structure_from_snapshot(payload: Dict[str, Any]) -> Any:
    from backtest.options.paper_trading import StructurePosition, _ist_session_date

    closed_at = _restore_ts(payload.get("closed_at"))
    session_date = payload.get("session_date")
    if not session_date and closed_at is not None:
        session_date = _ist_session_date(closed_at)
    opened_at = _restore_ts(payload.get("opened_at")) or now_utc()
    entry_session_date = payload.get("entry_session_date") or _ist_session_date(opened_at)

    return StructurePosition(
        structure_id=payload["structure_id"],
        structure_type=payload.get("structure_type", ""),
        strategy_name=payload.get("strategy_name", ""),
        underlying=payload.get("underlying", ""),
        expiry=(
            date.fromisoformat(payload["expiry"]) if payload.get("expiry") else date.today()
        ),
        legs=[_opt_position_from_snapshot(leg) for leg in payload.get("legs", [])],
        opened_at=opened_at,
        closed_at=closed_at,
        exit_reason=payload.get("exit_reason"),
        session_date=session_date,
        entry_session_date=entry_session_date,
        # Written by ``_structure_to_snapshot`` since BUG-18; a snapshot from
        # before that leaves them ``None`` and ``restore_structure`` re-derives
        # the anchor instead of pretending today started at zero.
        last_mark_session=payload.get("last_mark_session"),
        last_mark_net_pnl=Decimal(str(payload.get("last_mark_net_pnl", "0") or "0")),
        prior_close_net_pnl=Decimal(str(payload.get("prior_close_net_pnl", "0") or "0")),
        statutory_fees=Decimal(str(payload.get("statutory_fees", "0") or "0")),
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
    bridge._restored_contracts_rebound = False


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
        # Live Order Management: an operator's stop/target is protection the
        # human set deliberately — losing it to a restart would re-open the
        # exact risk the stop was there to cap.
        "position_rules": {
            key: dict(levels)
            for key, levels in (getattr(runner, "position_rules", {}) or {}).items()
        },
    }
    bridge = getattr(runner, "options_bridge", None)
    if bridge is not None:
        snapshot["bridge_manual_rules"] = bridge.manual_rules()
    if runner.options_bridge is not None:
        snapshot["bridge"] = capture_bridge(runner.options_bridge)
    # F-12: live gateway working orders — a restart re-arms POLLING for the
    # venue orders instead of double-placing them.
    snap_gateway = getattr(runner.broker, "snapshot_state", None)
    if callable(snap_gateway):
        snapshot["live_gateway"] = snap_gateway()
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
    # Live Order Management: restore the operator's stops/targets. Levels whose
    # position no longer exists are dropped — a stop with nothing to protect is
    # a trap for the next trade.
    restored_rules: Dict[str, Dict[str, Any]] = {}
    for key, levels in (snapshot.get("position_rules") or {}).items():
        if not isinstance(levels, dict):
            continue
        clean = {
            field: (float(value) if value is not None else None)
            for field, value in levels.items()
            if field in ("stop_loss", "target")
        }
        if any(value is not None for value in clean.values()):
            restored_rules[str(key)] = clean
    runner.position_rules = {
        key: levels for key, levels in restored_rules.items() if key in restored.positions
    }

    if runner.options_bridge is not None and snapshot.get("bridge"):
        restore_bridge(runner.options_bridge, snapshot["bridge"])
    bridge_levels = snapshot.get("bridge_manual_rules") or {}
    if runner.options_bridge is not None and runner.options_bridge.open_structure_id:
        stop_loss = bridge_levels.get("stop_loss")
        target = bridge_levels.get("target")
        if stop_loss is not None or target is not None:
            try:
                runner.options_bridge.set_manual_rule(stop_loss=stop_loss, target=target)
            except ValueError:
                # Levels can be stale relative to the restored mark (the book
                # moved while the process was down). Dropping them is the
                # fail-safe: a level that cannot be armed must not be reported
                # as armed.
                runner.options_bridge.clear_manual_rules()

    gateway_state = snapshot.get("live_gateway")
    restore_gateway = getattr(runner.broker, "restore_state", None)
    if gateway_state and callable(restore_gateway):
        restore_gateway(gateway_state)

    # Phase 1 (Plan §8): a spawn snapshot freezes configuration, not code.
    # If the strategy bundle (strategy file + template chain) changed while
    # we were down, the runner must not trade new code under old paperwork:
    # it comes back PAUSED with STRATEGY_CODE_CHANGED until an operator
    # resumes it (resume() restamps the bundle — a conscious migration).
    runner.strategy_code_changed = False
    _bundle = getattr(runner.config, "strategy_bundle", None)
    if isinstance(_bundle, dict) and _bundle.get("bundle_files"):
        try:
            from backtest.strategy.bundle import bundle_matches
            from backtest.strategy.execution import STRATEGY_CODE_CHANGED
            from backtest.strategy.registry import get_strategy

            _cls = get_strategy(runner.config.strategy_name)
            if not bundle_matches(_cls, _bundle["bundle_files"]):
                runner.strategy_code_changed = True
                runner.error = (
                    f"{STRATEGY_CODE_CHANGED}: strategy/template code changed "
                    "since this runner was spawned; resume to migrate consciously"
                )
        except Exception:  # noqa: BLE001 — an unverifiable bundle must not crash restore
            runner.strategy_code_changed = False

    saved_status = snapshot.get("status", STATUS_STOPPED)
    if runner.strategy_code_changed:
        runner.status = STATUS_PAUSED
    elif saved_status == STATUS_RUNNING:
        runner.status = STATUS_PAUSED
    else:
        runner.status = (
            saved_status
            if saved_status in (STATUS_PAUSED, STATUS_STOPPED)
            else STATUS_STOPPED
        )
    if runner.options_bridge is not None:
        runner.options_bridge.runner_running = runner.status == STATUS_RUNNING
        runner.options_bridge.runner_id = runner.instance_id
        runner.options_bridge.runner_name = runner.config.name


def _now_iso() -> str:
    """``saved_at`` — aware UTC, so a state file states its own clock."""
    return now_utc().isoformat()
