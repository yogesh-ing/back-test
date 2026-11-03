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
from typing import Any, Dict, Iterable, List, Optional

from backtest.timekeeping import current_session_date, from_stored_utc, now_utc

logger = logging.getLogger("backtest.forward.state_store")

__all__ = [
    "PortfolioStateStore",
    "PostgresStateStore",
    "STATE_SCHEMA_VERSION",
    "capture_runner",
    "restore_runner",
    "config_to_dict",
    "resolve_state_store_primary",
    "verify_state_parity",
]

_NON_PERSISTENT_SOURCES = frozenset({"synthetic", "replay"})


def resolve_state_store_primary() -> str:
    """Active boot authority: ``'json'`` (D1-a default) or ``'postgres'`` (D1-b)."""
    raw = str(os.environ.get("STATE_STORE_PRIMARY", "json") or "json").strip().lower()
    if raw in ("postgres", "pg", "postgresql", "db"):
        return "postgres"
    return "json"


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
                "w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
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
            payload = json.loads(self.path.read_text(encoding="utf-8"))
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
        "prev_close_mark": (
            str(p.prev_close_mark) if getattr(p, "prev_close_mark", None) is not None else None
        ),
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
    raw_prev_close = payload.get("prev_close_mark")
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
        prev_close_mark=(
            Decimal(str(raw_prev_close)) if raw_prev_close is not None else None
        ),
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
        # BUG-04: the NET contract's day anchors. Without them a restart
        # mid-session would re-baseline daily P&L to the whole lifetime.
        "pnl_session": runner._pnl_session,
        "last_session_realized": runner._last_session_realized,
        "last_session_open": runner._last_session_open,
        "prev_close_realized_net": runner._prev_close_realized_net,
        "prev_close_open_pnl": runner._prev_close_open_pnl,
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
    from backtest.simulator.portfolio import Portfolio

    # Book — full round-trip (positions, closed, equity history, orders).
    restored = Portfolio.from_dict(snapshot["portfolio"])
    runner.portfolio = restored
    # Rebind the runner's OWN costed engine to the new book. This site used to
    # install ``free_executor``, so every runner that survived a restart traded
    # zero-fee regardless of the BUG-26 cost fix — and a restored book is the
    # normal case, not the exception.
    runner.bind_executor(restored)

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

    # BUG-04 day anchors. A snapshot written before the contract existed has
    # no session history, and the book it restores is not flat: starting the
    # anchors at 0 would report the whole lifetime as today's P&L until the
    # first EOD. So baseline them at the restored book — the same
    # "re-baseline from current equity" decision the manager's anchors made
    # on 2026-10-15 — and let the session roll them forward from there.
    # This MUST run after the bridge is back: an option runner's realized and
    # open P&L live in the bridge, not in ``portfolio``, so baselining earlier
    # reads 0/0, leaves the anchors at zero, and the boot risk sweep then sees
    # the whole lifetime as today's loss (the 2026-11-02 false −3.8L breach).
    if "pnl_session" in snapshot:
        runner._pnl_session = snapshot.get("pnl_session")
        runner._last_session_realized = float(snapshot.get("last_session_realized", 0.0))
        runner._last_session_open = float(snapshot.get("last_session_open", 0.0))
        runner._prev_close_realized_net = float(snapshot.get("prev_close_realized_net", 0.0))
        runner._prev_close_open_pnl = float(snapshot.get("prev_close_open_pnl", 0.0))
    else:
        runner.baseline_pnl_anchors()

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


# ---------------------------------------------------------------------------
# Institutional PostgreSQL Single-Source-of-Truth Store (Sprint D / Ticket D1)
# ---------------------------------------------------------------------------


def _dec_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _dec_zero(value: Any) -> Decimal:
    out = _dec_or_none(value)
    return out if out is not None else Decimal("0")


def _extract_carry_policy(cfg: Dict[str, Any]) -> Optional[str]:
    inst = cfg.get("instrument")
    if not isinstance(inst, dict) or str(inst.get("type", "")).lower() != "option":
        return None
    expr = inst.get("expression")
    if isinstance(expr, dict):
        exit_cfg = expr.get("exit")
        if isinstance(exit_cfg, dict):
            cp = str(exit_cfg.get("carry_policy") or "").strip().lower()
            if cp in ("intraday", "positional"):
                return cp
        cp_top = str(expr.get("carry_policy") or "").strip().lower()
        if cp_top in ("intraday", "positional"):
            return cp_top
    return "intraday"


def _net_structure_mark(legs: List[Dict[str, Any]], key: str) -> Optional[Decimal]:
    """Signed net structure premium per unit across legs for ``key``."""
    if not legs:
        return None
    units = 0
    for leg in legs:
        u = int(leg.get("quantity", 0) or 0) * int(leg.get("lot_size", 0) or 0)
        if u > units:
            units = u
    if units <= 0:
        return None
    total = Decimal("0")
    seen = False
    for leg in legs:
        px = _dec_or_none(leg.get(key))
        if px is None:
            continue
        seen = True
        leg_units = Decimal(
            str(int(leg.get("quantity", 0) or 0) * int(leg.get("lot_size", 0) or 0))
        )
        sign = Decimal("1") if str(leg.get("side", "BUY")).upper() == "BUY" else Decimal("-1")
        total += px * leg_units * sign
    if not seen:
        return None
    return (total / Decimal(str(units))).quantize(Decimal("0.0001"))


class PostgresStateStore:
    """Event-driven PostgreSQL Single-Source-of-Truth store (Sprint D / Ticket D1).

    Honours the 7-Event Database Touch Contract:
      * 0 SQL queries on live WebSocket ticks, non-trading candle checks, or
        ``/api/portfolio`` SSE/HTTP UI refreshes.
      * Uses the shared ``DatabaseManager`` connection pool exclusively (POOL-3).
      * Skips ``synthetic`` / ``replay`` runners unless ``allow_synthetic=True``
        so unit tests never pollute production history while hermetic SQLite
        tests can opt in explicitly.
    """

    def __init__(self, db_manager: Any, *, allow_synthetic: bool = False) -> None:
        self.db = db_manager
        self.allow_synthetic = bool(allow_synthetic)
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self.db is None or self._schema_ready:
            return
        from backtest.db.models import ensure_forward_ssot_schema

        ensure_forward_ssot_schema(self.db)
        self._schema_ready = True

    def _eligible_runner_snap(self, snap: Dict[str, Any]) -> bool:
        if self.allow_synthetic:
            return True
        cfg = snap.get("config") or {}
        source = str(cfg.get("source") or "").strip().lower()
        return source not in _NON_PERSISTENT_SOURCES

    # -- Event 3 / Event 4: Snapshot & Structure/Leg Upsert ------------------

    def save(self, payload: Dict[str, Any]) -> bool:
        """Persist runner configs, open/closed option books, and risk state."""
        if self.db is None:
            return False
        self.ensure_schema()
        from backtest.db.models import (
            OptionOpenLegRecord,
            OptionOpenStructureRecord,
            RiskSessionStateRecord,
            RunnerInstanceRecord,
        )

        now = now_utc()
        all_snaps: List[Dict[str, Any]] = []
        for snap in payload.get("runners", []) or []:
            if isinstance(snap, dict) and self._eligible_runner_snap(snap):
                all_snaps.append(snap)
        for snap in (payload.get("parked_runners") or {}).values():
            if isinstance(snap, dict) and self._eligible_runner_snap(snap):
                all_snaps.append(snap)

        seen_ids: set[str] = set()
        with self.db.session() as session:
            for snap in all_snaps:
                cfg = dict(snap.get("config") or {})
                iid = str(cfg.get("instance_id") or "").strip()
                if not iid:
                    continue
                seen_ids.add(iid)
                mode = str(cfg.get("mode") or "paper").strip().lower()
                if mode not in ("paper", "live"):
                    mode = "paper"
                status = str(snap.get("status") or "PAUSED").strip().upper()
                if status not in ("RUNNING", "PAUSED", "STOPPED", "RETIRED"):
                    status = "PAUSED"
                carry_policy = _extract_carry_policy(cfg)

                row = session.get(RunnerInstanceRecord, iid)
                if row is None:
                    row = RunnerInstanceRecord(
                        instance_id=iid,
                        created_at=_restore_ts(snap.get("created_ts")) or now,
                    )
                    session.add(row)

                row.name = str(cfg.get("name") or iid)[:128]
                row.strategy_name = str(cfg.get("strategy_name") or "unknown")[:64]
                row.mode = mode
                row.source = str(cfg.get("source") or "mstock")[:32]
                row.segment = (
                    str(cfg.get("segment"))[:32] if cfg.get("segment") else None
                )
                row.execution_broker = (
                    str(cfg.get("execution_broker"))[:32]
                    if cfg.get("execution_broker")
                    else None
                )
                row.status = status
                row.allocated_capital = _dec_zero(cfg.get("allocated_capital"))
                row.carry_policy = carry_policy
                row.config_json = cfg
                # Store full snapshot for lossless restore while authoritative
                # option marks & risk latches live in relational tables below.
                row.runtime_state_json = {
                    k: v for k, v in snap.items() if k != "config"
                }
                last_prices = snap.get("last_price") or {}
                if isinstance(last_prices, dict) and last_prices:
                    first_px = next(iter(last_prices.values()), None)
                    row.last_mark_price = _dec_or_none(first_px)
                    row.last_mark_at = now
                row.updated_at = now

                self._upsert_option_book_from_snap(
                    session,
                    OptionOpenStructureRecord,
                    OptionOpenLegRecord,
                    iid=iid,
                    mode=mode,
                    carry_policy=carry_policy or "intraday",
                    snap=snap,
                    now=now,
                )

            # Retire any DB runner rows that were explicitly deleted in memory:
            existing_active = (
                session.query(RunnerInstanceRecord)
                .filter(RunnerInstanceRecord.status != "RETIRED")
                .all()
            )
            for db_row in existing_active:
                if db_row.instance_id not in seen_ids:
                    if not self.allow_synthetic and db_row.source in _NON_PERSISTENT_SOURCES:
                        continue
                    db_row.status = "RETIRED"
                    db_row.updated_at = now

            # Persist today's cash-flow-neutral risk slices:
            mgr_meta = payload.get("manager") or {}
            sess_iso = str(
                mgr_meta.get("session_date_ist")
                or current_session_date().isoformat()
            )
            sess_date = date.fromisoformat(sess_iso)
            self._upsert_risk_slice(
                session,
                RiskSessionStateRecord,
                sess_date=sess_date,
                slice_key="global",
                peak_daily_pnl=_dec_zero(
                    mgr_meta.get(
                        "session_peak_daily_pnl", mgr_meta.get("peak_daily_pnl")
                    )
                ),
                current_daily_pnl=_dec_zero(mgr_meta.get("current_daily_pnl")),
                deployed_capital=_dec_zero(mgr_meta.get("total_capital")),
                halted=bool(mgr_meta.get("halted", False)),
                halt_reason=mgr_meta.get("halt_reason"),
                now=now,
            )
            buckets = payload.get("buckets") or {}
            for slice_key, bdata in buckets.items():
                if not isinstance(bdata, dict):
                    continue
                self._upsert_risk_slice(
                    session,
                    RiskSessionStateRecord,
                    sess_date=sess_date,
                    slice_key=str(slice_key)[:64],
                    peak_daily_pnl=_dec_zero(
                        bdata.get(
                            "session_peak_daily_pnl", bdata.get("peak_daily_pnl")
                        )
                    ),
                    current_daily_pnl=_dec_zero(bdata.get("current_daily_pnl")),
                    deployed_capital=_dec_zero(bdata.get("deployed_capital")),
                    halted=bool(bdata.get("halted", False)),
                    halt_reason=bdata.get("halt_reason"),
                    now=now,
                )
        return True

    def _upsert_option_book_from_snap(
        self,
        session: Any,
        StructRow: Any,
        LegRow: Any,
        *,
        iid: str,
        mode: str,
        carry_policy: str,
        snap: Dict[str, Any],
        now: datetime,
    ) -> None:
        bridge = snap.get("bridge")
        if not isinstance(bridge, dict):
            return
        book = bridge.get("broker") or {}
        manual = snap.get("bridge_manual_rules") or {}
        open_sid = (bridge.get("scalars") or {}).get("open_structure_id")
        for s_raw in book.get("structures") or []:
            if not isinstance(s_raw, dict) or not s_raw.get("structure_id"):
                continue
            sid = str(s_raw["structure_id"])[:64]
            legs_raw = [x for x in (s_raw.get("legs") or []) if isinstance(x, dict)]
            closed_at = _restore_ts(s_raw.get("closed_at"))
            opened_at = _restore_ts(s_raw.get("opened_at")) or now
            status = "closed" if closed_at is not None else "open"
            expiry_d = (
                date.fromisoformat(s_raw["expiry"])
                if s_raw.get("expiry")
                else opened_at.date()
            )
            entry_sess = (
                date.fromisoformat(s_raw["entry_session_date"])
                if s_raw.get("entry_session_date")
                else opened_at.date()
            )
            close_sess = (
                date.fromisoformat(s_raw["session_date"])
                if s_raw.get("session_date")
                else None
            )
            entry_cost = Decimal("0")
            comm_total = Decimal("0")
            for leg in legs_raw:
                qty_units = Decimal(
                    str(int(leg.get("quantity", 0) or 0) * int(leg.get("lot_size", 0) or 0))
                )
                sign = (
                    Decimal("1")
                    if str(leg.get("side", "BUY")).upper() == "BUY"
                    else Decimal("-1")
                )
                entry_cost += _dec_zero(leg.get("entry_price")) * qty_units * sign
                comm_total += _dec_zero(leg.get("commission"))

            s_row = session.get(StructRow, sid)
            if s_row is None:
                s_row = StructRow(structure_id=sid, opened_at=opened_at)
                session.add(s_row)
            s_row.instance_id = iid
            s_row.mode = mode
            s_row.strategy_name = str(s_raw.get("strategy_name") or "unknown")[:64]
            s_row.structure_type = str(s_raw.get("structure_type") or "option")[:64]
            s_row.underlying = str(s_raw.get("underlying") or "NIFTY")[:32]
            s_row.expiry = expiry_d
            s_row.carry_policy = carry_policy
            s_row.status = status
            s_row.entry_session_date = entry_sess
            s_row.session_date = close_sess
            s_row.entry_net_cost = entry_cost.quantize(Decimal("0.0001"))
            s_row.commission_total = comm_total.quantize(Decimal("0.0001"))
            s_row.statutory_fees = _dec_zero(s_raw.get("statutory_fees"))
            s_row.last_mark_net_pnl = _dec_zero(s_raw.get("last_mark_net_pnl"))
            s_row.prior_close_net_pnl = _dec_zero(s_raw.get("prior_close_net_pnl"))
            net_prev = _net_structure_mark(legs_raw, "prev_close_mark")
            if net_prev is not None:
                s_row.prev_close_mark = net_prev
            if sid == open_sid and isinstance(manual, dict):
                s_row.stop_loss = _dec_or_none(manual.get("stop_loss"))
                s_row.target = _dec_or_none(manual.get("target"))
            s_row.opened_at = opened_at
            s_row.closed_at = closed_at
            s_row.last_mark_at = now
            s_row.exit_reason = (
                str(s_raw.get("exit_reason"))[:64] if s_raw.get("exit_reason") else None
            )
            session.flush()

            for leg in legs_raw:
                pid = str(leg.get("position_id") or "").strip()[:64]
                if not pid:
                    continue
                l_row = session.get(LegRow, pid)
                leg_opened = _restore_ts(leg.get("opened_at")) or opened_at
                leg_closed = _restore_ts(leg.get("closed_at"))
                leg_updated = _restore_ts(leg.get("last_updated")) or now
                if l_row is None:
                    l_row = LegRow(
                        position_id=pid,
                        structure_id=sid,
                        opened_at=leg_opened,
                    )
                    session.add(l_row)
                l_row.structure_id = sid
                l_row.instance_id = iid
                l_row.instrument_type = "OPT"
                l_row.instrument_token = (
                    str(leg.get("instrument_token"))[:64]
                    if leg.get("instrument_token")
                    else None
                )
                l_row.trading_symbol = str(
                    leg.get("trading_symbol") or f"{s_row.underlying}_{leg.get('strike')}"
                )[:64]
                l_row.underlying = str(leg.get("underlying") or s_row.underlying)[:32]
                opt_t = str(leg.get("option_type") or "CE").upper()
                l_row.option_type = opt_t if opt_t in ("CE", "PE") else "CE"
                l_row.strike = _dec_zero(leg.get("strike"))
                l_row.expiry = (
                    date.fromisoformat(leg["expiry"]) if leg.get("expiry") else expiry_d
                )
                side_s = str(leg.get("side") or "BUY").upper()
                l_row.side = side_s if side_s in ("BUY", "SELL") else "BUY"
                l_row.quantity = int(leg.get("quantity", 0) or 0)
                l_row.lot_size = int(leg.get("lot_size", 25) or 25)
                l_row.entry_price = _dec_zero(leg.get("entry_price"))
                l_row.last_mark_price = _dec_or_none(leg.get("current_price"))
                prev_m = _dec_or_none(leg.get("prev_close_mark"))
                if prev_m is not None:
                    l_row.prev_close_mark = prev_m
                l_row.unrealized_pnl = _dec_zero(leg.get("unrealized_pnl"))
                l_row.realized_pnl = _dec_zero(leg.get("realized_pnl"))
                l_row.commission = _dec_zero(leg.get("commission"))
                l_status = str(leg.get("status") or status).lower()
                l_row.status = "closed" if l_status == "closed" else "open"
                l_row.opened_at = leg_opened
                l_row.closed_at = leg_closed
                l_row.last_updated = leg_updated

    @staticmethod
    def _upsert_risk_slice(
        session: Any,
        RiskRow: Any,
        *,
        sess_date: date,
        slice_key: str,
        peak_daily_pnl: Decimal,
        current_daily_pnl: Decimal,
        deployed_capital: Decimal,
        halted: bool,
        halt_reason: Optional[str],
        now: datetime,
    ) -> None:
        r_row = session.get(RiskRow, (sess_date, slice_key))
        if r_row is None:
            r_row = RiskRow(session_date=sess_date, slice_key=slice_key)
            session.add(r_row)
        r_row.peak_daily_pnl = peak_daily_pnl
        r_row.current_daily_pnl = current_daily_pnl
        r_row.deployed_capital = deployed_capital
        r_row.halted = bool(halted)
        r_row.halt_reason = str(halt_reason) if halt_reason else None
        r_row.updated_at = now

    # -- Event 5: Risk Milestone Write ---------------------------------------

    def save_risk_milestone(
        self, session_date_iso: str, slices: Dict[str, Dict[str, Any]]
    ) -> bool:
        """Persist high-water mark or halt-latch milestone in one lightweight tx."""
        if self.db is None or not slices:
            return False
        self.ensure_schema()
        from backtest.db.models import RiskSessionStateRecord

        now = now_utc()
        sess_date = date.fromisoformat(session_date_iso)
        with self.db.session() as session:
            for slice_key, bdata in slices.items():
                if not isinstance(bdata, dict):
                    continue
                self._upsert_risk_slice(
                    session,
                    RiskSessionStateRecord,
                    sess_date=sess_date,
                    slice_key=str(slice_key)[:64],
                    peak_daily_pnl=_dec_zero(bdata.get("peak_daily_pnl")),
                    current_daily_pnl=_dec_zero(bdata.get("current_daily_pnl")),
                    deployed_capital=_dec_zero(bdata.get("deployed_capital")),
                    halted=bool(bdata.get("halted", False)),
                    halt_reason=bdata.get("halt_reason"),
                    now=now,
                )
        return True

    # -- Event 6: 60s Crash Checkpoint & 15:30 IST Official EOD Settlement ---

    def flush_mark_checkpoint(
        self, runners: Iterable[Any], *, settle_prev_close: bool = False
    ) -> int:
        """Batch-flush live marks (and at 15:30 IST stamp ``prev_close_mark``)."""
        if self.db is None:
            return 0
        self.ensure_schema()
        from backtest.db.models import (
            OptionOpenLegRecord,
            OptionOpenStructureRecord,
            RunnerInstanceRecord,
        )

        now = now_utc()
        today_iso = current_session_date().isoformat()
        updated = 0
        with self.db.session() as session:
            for runner in runners:
                source = str(getattr(runner.config, "source", "") or "").lower()
                if not self.allow_synthetic and source in _NON_PERSISTENT_SOURCES:
                    continue
                r_row = session.get(RunnerInstanceRecord, runner.instance_id)
                if r_row is not None and runner.last_price:
                    first_px = next(iter(runner.last_price.values()), None)
                    px_dec = _dec_or_none(first_px)
                    if px_dec is not None:
                        r_row.last_mark_price = px_dec
                        r_row.last_mark_at = now
                        if settle_prev_close:
                            r_row.prev_close_mark = px_dec
                        updated += 1

                bridge = getattr(runner, "options_bridge", None)
                if bridge is None:
                    continue
                for s in bridge.option_broker.get_open_structures():
                    if settle_prev_close:
                        net_open = (
                            s.total_unrealized_pnl
                            - s.total_commission
                            - getattr(s, "statutory_fees", Decimal("0"))
                        )
                        s.prior_close_net_pnl = net_open
                        s.last_mark_net_pnl = net_open
                        s.last_mark_session = today_iso
                        for leg in s.legs:
                            leg.prev_close_mark = leg.current_price

                    s_row = session.get(OptionOpenStructureRecord, s.structure_id)
                    if s_row is not None:
                        s_row.last_mark_net_pnl = getattr(
                            s, "last_mark_net_pnl", Decimal("0")
                        )
                        s_row.prior_close_net_pnl = getattr(
                            s, "prior_close_net_pnl", Decimal("0")
                        )
                        s_row.last_mark_at = now
                        if settle_prev_close:
                            legs_snap = [_opt_position_to_snapshot(lg) for lg in s.legs]
                            s_row.prev_close_mark = _net_structure_mark(
                                legs_snap, "prev_close_mark"
                            )
                        updated += 1
                    for leg in s.legs:
                        l_row = session.get(OptionOpenLegRecord, leg.position_id)
                        if l_row is not None:
                            l_row.last_mark_price = leg.current_price
                            l_row.unrealized_pnl = leg.unrealized_pnl
                            l_row.last_updated = now
                            if settle_prev_close and leg.prev_close_mark is not None:
                                l_row.prev_close_mark = leg.prev_close_mark
                            updated += 1
        return updated

    # -- Event 1: Boot Hydration (READ once) ---------------------------------

    def load(self) -> Optional[Dict[str, Any]]:
        """Hydrate full PortfolioManager state payload from PostgreSQL."""
        if self.db is None:
            return None
        self.ensure_schema()
        from backtest.db.models import (
            OptionOpenStructureRecord,
            RiskSessionStateRecord,
            RunnerInstanceRecord,
        )

        today_d = current_session_date()
        with self.db.session() as session:
            r_rows = (
                session.query(RunnerInstanceRecord)
                .filter(RunnerInstanceRecord.status != "RETIRED")
                .order_by(RunnerInstanceRecord.created_at.asc())
                .all()
            )
            if not r_rows:
                risk_rows = (
                    session.query(RiskSessionStateRecord)
                    .filter(RiskSessionStateRecord.session_date == today_d)
                    .all()
                )
                if not risk_rows:
                    return None
            else:
                risk_rows = (
                    session.query(RiskSessionStateRecord)
                    .filter(RiskSessionStateRecord.session_date == today_d)
                    .all()
                )

            runners_out: List[Dict[str, Any]] = []
            for r_row in r_rows:
                cfg = dict(r_row.config_json or {})
                cfg["instance_id"] = r_row.instance_id
                snap = dict(r_row.runtime_state_json or {})
                snap["config"] = cfg
                snap["status"] = r_row.status

                # Overlay authoritative relational option structures & legs:
                s_rows = (
                    session.query(OptionOpenStructureRecord)
                    .filter(
                        OptionOpenStructureRecord.instance_id == r_row.instance_id
                    )
                    .all()
                )
                if s_rows and isinstance(snap.get("bridge"), dict):
                    by_sid = {sr.structure_id: sr for sr in s_rows}
                    broker_book = snap["bridge"].setdefault("broker", {})
                    for s_dict in broker_book.get("structures") or []:
                        sr = by_sid.get(s_dict.get("structure_id"))
                        if sr is None:
                            continue
                        s_dict["last_mark_net_pnl"] = str(sr.last_mark_net_pnl)
                        s_dict["prior_close_net_pnl"] = str(sr.prior_close_net_pnl)
                        s_dict["statutory_fees"] = str(sr.statutory_fees)
                        if sr.closed_at is not None:
                            s_dict["closed_at"] = sr.closed_at.isoformat()
                        if sr.exit_reason:
                            s_dict["exit_reason"] = sr.exit_reason
                        leg_by_id = {lr.position_id: lr for lr in sr.legs}
                        for l_dict in s_dict.get("legs") or []:
                            lr = leg_by_id.get(l_dict.get("position_id"))
                            if lr is None:
                                continue
                            if lr.last_mark_price is not None:
                                l_dict["current_price"] = str(lr.last_mark_price)
                            if lr.prev_close_mark is not None:
                                l_dict["prev_close_mark"] = str(lr.prev_close_mark)
                            l_dict["unrealized_pnl"] = str(lr.unrealized_pnl)
                            l_dict["realized_pnl"] = str(lr.realized_pnl)
                            l_dict["status"] = lr.status
                        if sr.status == "open" and (
                            sr.stop_loss is not None or sr.target is not None
                        ):
                            snap["bridge_manual_rules"] = {
                                "stop_loss": (
                                    float(sr.stop_loss)
                                    if sr.stop_loss is not None
                                    else None
                                ),
                                "target": (
                                    float(sr.target) if sr.target is not None else None
                                ),
                            }
                runners_out.append(snap)

            buckets_out: Dict[str, Any] = {}
            latest_sess = today_d.isoformat()
            mgr_out: Dict[str, Any] = {"session_date_ist": latest_sess}
            for rr in risk_rows:
                if rr.slice_key == "global":
                    mgr_out.update(
                        {
                            "session_peak_daily_pnl": float(rr.peak_daily_pnl),
                            "halted": bool(rr.halted),
                            "halt_reason": rr.halt_reason,
                        }
                    )
                    continue
                buckets_out[rr.slice_key] = {
                    "session_date_ist": latest_sess,
                    "session_peak_daily_pnl": float(rr.peak_daily_pnl),
                    "peak_daily_pnl": float(rr.peak_daily_pnl),
                    "current_daily_pnl": float(rr.current_daily_pnl),
                    "deployed_capital": float(rr.deployed_capital),
                    "halted": bool(rr.halted),
                    "halt_reason": rr.halt_reason,
                }

        return {
            "schema": STATE_SCHEMA_VERSION,
            "saved_at": _now_iso(),
            "manager": mgr_out,
            "buckets": buckets_out,
            "runners": runners_out,
        }


def verify_state_parity(
    json_payload: Optional[Dict[str, Any]],
    pg_payload: Optional[Dict[str, Any]],
    *,
    allow_synthetic: bool = False,
    tolerance_inr: float = 0.01,
) -> Dict[str, Any]:
    """Compare JSON vs. PostgreSQL snapshots (Phase D1-a / D1-b Parity Gate).

    Checks runner count, open structure/leg IDs, ``prev_close_mark``, and risk
    slice ``peak_daily_pnl`` / ``halted`` within ``tolerance_inr`` (₹0.01).
    """
    diffs: List[str] = []

    def _eligible_runners(payload: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        if not isinstance(payload, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        items = list(payload.get("runners") or []) + list(
            (payload.get("parked_runners") or {}).values()
        )
        for snap in items:
            if not isinstance(snap, dict):
                continue
            cfg = snap.get("config") or {}
            src = str(cfg.get("source") or "").strip().lower()
            if not allow_synthetic and src in _NON_PERSISTENT_SOURCES:
                continue
            iid = str(cfg.get("instance_id") or "").strip()
            if iid:
                out[iid] = snap
        return out

    j_runners = _eligible_runners(json_payload)
    p_runners = _eligible_runners(pg_payload)

    if set(j_runners.keys()) != set(p_runners.keys()):
        diffs.append(
            f"runner_ids json={sorted(j_runners.keys())} pg={sorted(p_runners.keys())}"
        )

    for iid in sorted(set(j_runners.keys()) & set(p_runners.keys())):
        jb = ((j_runners[iid].get("bridge") or {}).get("broker") or {}).get(
            "structures"
        ) or []
        pb = ((p_runners[iid].get("bridge") or {}).get("broker") or {}).get(
            "structures"
        ) or []
        j_open = {
            s["structure_id"]: s
            for s in jb
            if isinstance(s, dict) and not s.get("closed_at")
        }
        p_open = {
            s["structure_id"]: s
            for s in pb
            if isinstance(s, dict) and not s.get("closed_at")
        }
        if set(j_open.keys()) != set(p_open.keys()):
            diffs.append(
                f"runner {iid[:8]} open_structures json={sorted(j_open)} pg={sorted(p_open)}"
            )
            continue
        for sid in sorted(j_open.keys()):
            j_legs = {lg["position_id"]: lg for lg in j_open[sid].get("legs") or []}
            p_legs = {lg["position_id"]: lg for lg in p_open[sid].get("legs") or []}
            if set(j_legs.keys()) != set(p_legs.keys()):
                diffs.append(f"structure {sid} leg_ids mismatch")
                continue
            for pid in sorted(j_legs.keys()):
                for field_name in ("entry_price", "current_price", "prev_close_mark"):
                    jp = _dec_or_none(j_legs[pid].get(field_name))
                    pp = _dec_or_none(p_legs[pid].get(field_name))
                    if (jp is None) != (pp is None) or (
                        jp is not None
                        and pp is not None
                        and abs(float(jp - pp)) > tolerance_inr
                    ):
                        diffs.append(
                            f"leg {pid} {field_name} json={jp} pg={pp}"
                        )

    # Compare today's risk session state when both sides carry today's IST session:
    today_iso = current_session_date().isoformat()
    j_sess = ((json_payload or {}).get("manager") or {}).get("session_date_ist")
    p_sess = ((pg_payload or {}).get("manager") or {}).get("session_date_ist")
    if j_sess == today_iso and p_sess == today_iso and j_runners:
        j_buckets = (json_payload or {}).get("buckets") or {}
        p_buckets = (pg_payload or {}).get("buckets") or {}
        for sk in ("paper", "live"):
            if sk in j_buckets and sk in p_buckets:
                jb_s = j_buckets[sk] or {}
                pb_s = p_buckets[sk] or {}
                j_peak = float(
                    jb_s.get(
                        "session_peak_daily_pnl", jb_s.get("peak_daily_pnl", 0.0)
                    )
                    or 0.0
                )
                p_peak = float(
                    pb_s.get(
                        "session_peak_daily_pnl", pb_s.get("peak_daily_pnl", 0.0)
                    )
                    or 0.0
                )
                if abs(j_peak - p_peak) > tolerance_inr:
                    diffs.append(f"risk[{sk}].peak_daily_pnl json={j_peak} pg={p_peak}")
                if bool(jb_s.get("halted", False)) != bool(pb_s.get("halted", False)):
                    diffs.append(
                        f"risk[{sk}].halted json={jb_s.get('halted')} pg={pb_s.get('halted')}"
                    )

    ok = len(diffs) == 0
    status = "PARITY_OK" if ok else "PARITY_DIFF"
    if ok:
        logger.info("[state-parity] %s (runners=%d)", status, len(p_runners))
    else:
        logger.warning("[state-parity] %s: %s", status, "; ".join(diffs))
    return {"ok": ok, "status": status, "diffs": diffs, "runner_count": len(p_runners)}
