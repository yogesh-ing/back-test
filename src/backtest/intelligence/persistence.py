"""Fail-soft DB persistence for alerts, Greeks snapshots and regime samples.

Design rules (same as the trade persister):

* Persistence must NEVER break alerting or trading — every failure is logged
  once and swallowed; a missing table disables that writer.
* Writes happen on a dedicated background thread fed by a bounded queue, so
  a slow database never stalls the tick thread or holds the alert broker's
  lock. When the queue is full the oldest-first policy drops the new item
  (and counts it) rather than blocking.
* Accepts any duck-typed ``DatabaseManager`` exposing ``session()``; tests
  use in-memory SQLite and call :meth:`flush` to drain synchronously.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from backtest.alerts.types import Alert

logger = logging.getLogger("backtest.intelligence.persistence")

QUEUE_MAX = 5000


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 2)))
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    """Round-trip through json to strip sets/datetimes/Decimals."""
    import json

    return json.loads(json.dumps(value, default=str))


class IntelligencePersister:
    def __init__(self, db_manager: Any, ensure_schema: bool = False) -> None:
        self.db = db_manager
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._disabled: Dict[str, str] = {}
        self.dropped = 0
        self.written = 0
        if ensure_schema:
            self.ensure_schema()

    # ------------------------------------------------------------------ #
    # Schema (dev convenience — production applies migration 005)
    # ------------------------------------------------------------------ #

    def ensure_schema(self) -> bool:
        """Create the three tables if missing (``checkfirst``). SQLite dev DBs
        only by default — production schemas come from migration 005."""
        try:
            from backtest.db.models import (
                Base,
                MarketRegimeSnapshot,
                PortfolioAlert,
                PortfolioGreeksSnapshot,
            )

            engine = self.db.engine if getattr(self.db, "is_connected", True) else None
            if engine is None:
                engine = self.db.connect()
            Base.metadata.create_all(
                engine,
                tables=[
                    PortfolioAlert.__table__,
                    PortfolioGreeksSnapshot.__table__,
                    MarketRegimeSnapshot.__table__,
                ],
                checkfirst=True,
            )
            return True
        except Exception:  # noqa: BLE001
            logger.info("intelligence schema bootstrap skipped", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # Queue plumbing
    # ------------------------------------------------------------------ #

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="intelligence-persister"
        )
        self._thread.start()

    def _submit(self, fn: Callable[[], None], start_thread: bool = True) -> None:
        try:
            self._queue.put_nowait(fn)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning("intelligence persistence queue full — dropped %d", self.dropped)
            return
        if start_thread:
            self._ensure_thread()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                fn = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._run(fn)

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
            self.written += 1
        except Exception:  # noqa: BLE001 — fail-soft
            logger.debug("intelligence persistence write failed", exc_info=True)
        finally:
            try:
                self._queue.task_done()
            except ValueError:
                pass

    def flush(self, timeout: float = 5.0) -> None:
        """Drain pending writes synchronously (tests / shutdown)."""
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        while datetime.now(timezone.utc).timestamp() < deadline:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                return
            self._run(fn)

    def stop(self) -> None:
        self._stop.set()
        self.flush(timeout=2.0)

    def _guarded(self, table: str, fn: Callable[[Any], None]) -> Callable[[], None]:
        def run() -> None:
            if table in self._disabled:
                return
            try:
                with self.db.session() as s:
                    fn(s)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "no such table" in msg or "does not exist" in msg or "undefined" in msg:
                    self._disabled[table] = str(exc)
                    logger.warning(
                        "[intelligence] table %s missing — apply migration 005; "
                        "persistence for it disabled this session",
                        table,
                    )
                else:
                    logger.debug("[intelligence] write to %s failed: %s", table, exc)
                raise

        return run

    # ------------------------------------------------------------------ #
    # Writers
    # ------------------------------------------------------------------ #

    def on_alert_event(self, event: str, alert: Alert) -> None:
        """AlertBroker listener — upsert the alert row on every lifecycle event."""
        snap = {
            "alert_id": alert.alert_id,
            "alert_type": str(alert.alert_type),
            "alert_key": alert.key[:200],
            "severity": str(alert.severity),
            "message": alert.message,
            "data": _json_safe(alert.data),
            "created_at": alert.created_at,
            "updated_at": alert.updated_at,
            "resolved_at": alert.resolved_at,
            "dismissed_at": alert.dismissed_at,
            "dismissed_by": alert.dismissed_by,
            "reviewed_at": alert.reviewed_at,
            "notified_strategies": _json_safe(alert.notified_strategies),
        }

        def write(s: Any) -> None:
            from backtest.db.models import PortfolioAlert

            row = s.get(PortfolioAlert, snap["alert_id"])
            if row is None:
                s.add(PortfolioAlert(**snap))
            else:
                for key, value in snap.items():
                    if key != "alert_id":
                        setattr(row, key, value)

        self._submit(self._guarded("alerts", write))

    def record_greeks(
        self,
        greeks: Dict[str, Any],
        concentration: Optional[Dict[str, Any]] = None,
        ts: Optional[datetime] = None,
    ) -> None:
        stamp = ts or datetime.now(timezone.utc)
        payload = {
            "timestamp": stamp,
            "net_delta": _dec(greeks.get("net_delta")),
            "net_gamma": _dec(greeks.get("net_gamma")),
            "net_vega": _dec(greeks.get("net_vega")),
            "net_theta": _dec(greeks.get("net_theta")),
            "greeks_by_strategy": _json_safe(greeks.get("breakdown_by_strategy") or []),
            "concentration_by_underlying": _json_safe(
                (concentration or {}).get("by_underlying") or {}
            ),
            "concentration_by_strike": _json_safe((concentration or {}).get("by_strike") or {}),
        }

        def write(s: Any) -> None:
            from backtest.db.models import PortfolioGreeksSnapshot

            s.add(PortfolioGreeksSnapshot(**payload))

        self._submit(self._guarded("portfolio_greeks_history", write))

    def record_regime(self, sample: Dict[str, Any]) -> None:
        ts = sample.get("ts")
        stamp = datetime.fromisoformat(ts) if isinstance(ts, str) else (
            ts or datetime.now(timezone.utc)
        )
        payload = {
            "timestamp": stamp,
            "regime": str(sample.get("regime") or "unknown")[:20],
            "vix": _dec(sample.get("value")),
            "realized_vol": _dec(sample.get("realized_vol")),
            "source": str(sample.get("source") or "")[:64] or None,
            "previous_regime": (str(sample.get("previous_regime"))[:20]
                                if sample.get("previous_regime") else None),
            "regime_changed": bool(sample.get("regime_changed")),
        }

        def write(s: Any) -> None:
            from backtest.db.models import MarketRegimeSnapshot

            s.add(MarketRegimeSnapshot(**payload))

        self._submit(self._guarded("market_regime_history", write))

    # ------------------------------------------------------------------ #
    # Readers (history API when a DB is attached)
    # ------------------------------------------------------------------ #

    def alert_history(
        self,
        since: Optional[datetime] = None,
        alert_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 200,
    ) -> Optional[List[Dict[str, Any]]]:
        """Rows from the ``alerts`` table, newest first — ``None`` if unavailable."""
        if "alerts" in self._disabled:
            return None
        try:
            from sqlalchemy import select

            from backtest.db.models import PortfolioAlert

            with self.db.session() as s:
                q = select(PortfolioAlert).order_by(PortfolioAlert.created_at.desc())
                if since is not None:
                    q = q.where(PortfolioAlert.created_at >= since)
                if alert_type:
                    q = q.where(PortfolioAlert.alert_type.contains(alert_type.lower()))
                if severity:
                    q = q.where(PortfolioAlert.severity == severity)
                rows = s.execute(q.limit(int(limit))).scalars().all()

                def iso(v: Any) -> Optional[str]:
                    return v.isoformat() if v is not None else None

                return [
                    {
                        "alert_id": r.alert_id,
                        "alert_type": r.alert_type,
                        "key": r.alert_key,
                        "severity": r.severity,
                        "message": r.message,
                        "data": r.data,
                        "created_at": iso(r.created_at),
                        "updated_at": iso(r.updated_at),
                        "resolved_at": iso(r.resolved_at),
                        "dismissed_at": iso(r.dismissed_at),
                        "dismissed_by": r.dismissed_by,
                        "reviewed_at": iso(r.reviewed_at),
                        "notified_strategies": r.notified_strategies or [],
                        "status": (
                            "resolved" if r.resolved_at else "reviewed" if r.reviewed_at
                            else "dismissed" if r.dismissed_at else "active"
                        ),
                    }
                    for r in rows
                ]
        except Exception:  # noqa: BLE001
            logger.debug("alert history read failed", exc_info=True)
            return None
