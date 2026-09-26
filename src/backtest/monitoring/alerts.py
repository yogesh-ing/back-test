"""Alert lifecycle: raise → (escalate) → resolve, with acknowledgement.

The analytics re-derive their findings from scratch on every evaluation; the
book is what turns that stream into *events*. A condition that stays true
across evaluations is one alert (``last_seen`` advances, ``occurrences``
counts), not a new line every tick. Only **transitions** are returned to the
caller — first raise, escalation (warning → critical), and resolution — so the
audit log and any notifier see each state change exactly once. This mirrors
the order-aging pattern in the portfolio manager (one entry per band).

Books are scoped (``all`` / ``paper`` / ``live``): evaluating the paper bucket
must never resolve an alert raised by the live bucket.

Resolution has hysteresis (``resolve_after`` consecutive absent evaluations);
while clearing, the record stays active with ``misses`` > 0 so the UI can
show it as "clearing". Escalation is measured against the alert's
``peak_severity`` for its active lifetime, so a metric hovering on a threshold
(warning → info → warning) escalates once, not every crossing.

This is deliberately the *minimal* engine the monitor needs today; the rule
engine planned for a later phase (composite rules, cooldowns, channels) plugs
in above ``reconcile``.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from backtest.monitoring.models import SEVERITY_RANK, Alert

EVENT_RAISED = "raised"
EVENT_ESCALATED = "escalated"
EVENT_RESOLVED = "resolved"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AlertBook:
    def __init__(self, history_size: int = 500, resolve_after: int = 3) -> None:
        #: Consecutive evaluations a condition must be absent before it
        #: resolves — hysteresis, so a metric hovering at a threshold does not
        #: raise/resolve/raise into the audit log every sweep.
        self.resolve_after = max(1, int(resolve_after))
        self._lock = threading.RLock()
        # scope → key → record
        self._active: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_size)

    def reconcile(
        self,
        scope: str,
        alerts: List[Alert],
        categories: Optional[List[str]] = None,
        now: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Merge one evaluation's findings; return the transition events.

        ``categories`` limits resolution to the categories that were actually
        evaluated — a Greeks-only pass must not resolve correlation alerts.
        """
        stamp = now or _now()
        events: List[Dict[str, Any]] = []
        with self._lock:
            book = self._active.setdefault(scope, {})
            seen = set()
            for alert in alerts:
                seen.add(alert.key)
                rec = book.get(alert.key)
                payload = alert.to_dict()
                if rec is None:
                    rec = {
                        **payload,
                        "id": uuid.uuid4().hex[:12],
                        "scope": scope,
                        "status": "active",
                        "first_seen": stamp,
                        "last_seen": stamp,
                        "occurrences": 1,
                        "acknowledged": False,
                        "acknowledged_at": None,
                        "peak_severity": alert.severity,
                        "misses": 0,
                    }
                    book[alert.key] = rec
                    events.append(self._event(EVENT_RAISED, rec, stamp))
                    continue
                previous = rec["severity"]
                keep = {k: rec[k] for k in (
                    "id", "scope", "status", "first_seen", "occurrences",
                    "acknowledged", "acknowledged_at", "peak_severity",
                )}
                rec.clear()
                rec.update(payload)
                rec.update(keep)
                rec["last_seen"] = stamp
                rec["occurrences"] += 1
                rec["misses"] = 0
                peak = rec.get("peak_severity") or previous
                if SEVERITY_RANK.get(alert.severity, 0) > SEVERITY_RANK.get(peak, 0):
                    # Worse than it has EVER been while active → a fresh event,
                    # and the ack no longer covers it (you acknowledged a
                    # warning, not a critical). Dipping back to a level already
                    # reached (warning → info → warning) is not news: no event,
                    # and the ack stands — otherwise a metric hovering at a
                    # threshold re-pages the operator every sweep.
                    rec["acknowledged"] = False
                    rec["acknowledged_at"] = None
                    rec["peak_severity"] = alert.severity
                    events.append(
                        self._event(EVENT_ESCALATED, rec, stamp, previous=previous)
                    )

            for key in list(book):
                if key in seen:
                    continue
                if categories is not None and book[key]["category"] not in categories:
                    continue
                book[key]["misses"] = book[key].get("misses", 0) + 1
                if book[key]["misses"] < self.resolve_after:
                    continue  # clearing, not cleared — see resolve_after
                rec = book.pop(key)
                rec["status"] = "resolved"
                rec["resolved_at"] = stamp
                self._history.appendleft(dict(rec))
                events.append(self._event(EVENT_RESOLVED, rec, stamp))
        return events

    @staticmethod
    def _event(kind: str, rec: Dict[str, Any], stamp: str, **extra: Any) -> Dict[str, Any]:
        return {
            "event": kind,
            "ts": stamp,
            "id": rec["id"],
            "key": rec["key"],
            "scope": rec["scope"],
            "category": rec["category"],
            "severity": rec["severity"],
            "title": rec["title"],
            "message": rec["message"],
            **extra,
        }

    def active(self, scope: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for r in self._active.get(scope, {}).values()]
        return sorted(
            rows,
            key=lambda r: (-SEVERITY_RANK.get(r["severity"], 0), r["first_seen"], r["key"]),
        )

    def history(self, scope: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._history if scope is None or r["scope"] == scope]
        return rows[:limit]

    def acknowledge(self, alert_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for book in self._active.values():
                for rec in book.values():
                    if rec["id"] == alert_id:
                        rec["acknowledged"] = True
                        rec["acknowledged_at"] = _now()
                        return dict(rec)
        return None

    def counts(self, scope: str) -> Dict[str, int]:
        out = {"critical": 0, "warning": 0, "info": 0, "unacknowledged": 0}
        for rec in self.active(scope):
            out[rec["severity"]] = out.get(rec["severity"], 0) + 1
            if not rec["acknowledged"] and rec["severity"] != "info":
                out["unacknowledged"] += 1
        return out
