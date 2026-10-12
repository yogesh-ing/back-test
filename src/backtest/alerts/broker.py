"""AlertBroker — the pub/sub core of the Portfolio Intelligence layer.

Platform role: **calculate + inform + broadcast**. The broker never acts on
an alert. It

1. keeps one alert per dedupe key (``type:subject``) while the condition is
   true, updating its numbers instead of spamming new alerts;
2. broadcasts new (and escalated) alerts to subscribed strategies — each
   callback isolated, so one failing strategy never blocks another;
3. notifies listeners (the persistence layer, the UI change counter);
4. resolves state alerts when their condition clears, expires event alerts
   after a TTL, and auto-dismisses ignored non-critical alerts.

Thread-safety: all state sits behind one ``RLock``. Strategy callbacks and
listeners run **after** the lock is released — a callback that takes its
runner's lock can therefore never deadlock against the broker.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from backtest.alerts.types import (
    EVENT_ALERT_TYPES,
    SEVERITY_RANK,
    Alert,
    Severity,
    utcnow,
)

logger = logging.getLogger("backtest.alerts")

AlertCallback = Callable[[str, Dict[str, Any]], Any]
Listener = Callable[[str, Alert], None]

#: Default bounds — overridable per broker (tests use tiny values).
DEFAULT_HISTORY_SIZE = 2000
DEFAULT_EVENT_TTL_S = 3600.0
DEFAULT_IGNORE_TTL_S = 3600.0
#: A key that resolved and re-fires within this window re-notifies nobody
#: (the trader still sees it in the widget) — flapping guard.
DEFAULT_RENOTIFY_COOLDOWN_S = 60.0


class _Subscription:
    __slots__ = ("alert_type", "callback", "subscriber_id", "meta", "data_hook", "on_resolve")

    def __init__(
        self,
        alert_type: str,
        callback: AlertCallback,
        subscriber_id: str,
        meta: Dict[str, Any],
        data_hook: Optional[Callable[[Alert], Dict[str, Any]]] = None,
        on_resolve: Optional[AlertCallback] = None,
    ) -> None:
        self.alert_type = alert_type
        self.callback = callback
        self.subscriber_id = subscriber_id
        self.meta = meta
        self.data_hook = data_hook
        self.on_resolve = on_resolve


class AlertBroker:
    """Pub/sub for alerts: strategies subscribe, the platform publishes."""

    def __init__(
        self,
        history_size: int = DEFAULT_HISTORY_SIZE,
        event_ttl_s: float = DEFAULT_EVENT_TTL_S,
        ignore_ttl_s: float = DEFAULT_IGNORE_TTL_S,
        renotify_cooldown_s: float = DEFAULT_RENOTIFY_COOLDOWN_S,
        auto_dismiss_exempt: Iterable[str] = ("critical",),
    ) -> None:
        self._lock = threading.RLock()
        self._subscriptions: List[_Subscription] = []
        self._by_key: Dict[str, Alert] = {}  # open (unresolved) alerts
        self._by_id: Dict[str, Alert] = {}
        self._history: Deque[Alert] = deque(maxlen=int(history_size))
        self._listeners: List[Listener] = []
        self._last_notified: Dict[str, datetime] = {}
        self._pending_resolved: List[Tuple[Alert, List[_Subscription]]] = []
        self.event_ttl_s = float(event_ttl_s)
        self.ignore_ttl_s = float(ignore_ttl_s)
        self.renotify_cooldown_s = float(renotify_cooldown_s)
        self.auto_dismiss_exempt = {str(s) for s in auto_dismiss_exempt}
        #: Bumped on every visible change — the UI polls/streams this to know
        #: whether its copy is stale without diffing payloads.
        self.version = 0
        self.callback_failures = 0

    # ------------------------------------------------------------------ #
    # Subscriptions
    # ------------------------------------------------------------------ #

    def subscribe(
        self,
        alert_type: str,
        callback: AlertCallback,
        subscriber_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        data_hook: Optional[Callable[[Alert], Dict[str, Any]]] = None,
        on_resolve: Optional[AlertCallback] = None,
    ) -> str:
        """Register ``callback(alert_type, data)`` for one alert type.

        ``subscriber_id`` groups a subscriber's registrations so they can be
        dropped together (a runner being removed). ``meta`` is display data
        (runner name, strategy) for the alert-detail "who is subscribed"
        panel. ``data_hook(alert)`` may return a per-subscriber copy of the
        payload (e.g. to add *this* strategy's own contribution).
        ``on_resolve(alert_type, data)`` is called when an alert this
        subscriber was notified about resolves (condition cleared / expired).
        """
        sid = subscriber_id or f"sub-{id(callback)}"
        with self._lock:
            self._subscriptions.append(
                _Subscription(
                    str(alert_type), callback, sid, dict(meta or {}), data_hook, on_resolve
                )
            )
        return sid

    def unsubscribe(self, subscriber_id: str, alert_type: Optional[str] = None) -> int:
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [
                s
                for s in self._subscriptions
                if not (
                    s.subscriber_id == subscriber_id
                    and (alert_type is None or s.alert_type == str(alert_type))
                )
            ]
            return before - len(self._subscriptions)

    def subscribers_for(self, alert_type: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"subscriber_id": s.subscriber_id, **s.meta}
                for s in self._subscriptions
                if s.alert_type == str(alert_type)
            ]

    def subscriptions(self) -> Dict[str, List[Dict[str, Any]]]:
        """``{subscriber_id: {..meta, "alert_types": [...]}}`` as a list view."""
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for s in self._subscriptions:
                entry = out.setdefault(
                    s.subscriber_id,
                    {"subscriber_id": s.subscriber_id, **s.meta, "alert_types": []},
                )
                entry["alert_types"].append(s.alert_type)
        return {"subscribers": list(out.values())}  # type: ignore[return-value]

    def add_listener(self, listener: Listener) -> None:
        """``listener(event, alert)`` with event ∈ created|updated|resolved|
        dismissed|reviewed. Used by persistence; failures are swallowed."""
        with self._lock:
            self._listeners.append(listener)

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #

    def publish(self, alert: Alert) -> Alert:
        """Broadcast a fully-formed alert (PRD API). Dedupes on ``alert.key``."""
        return self.raise_alert(
            alert.alert_type,
            alert.severity,
            alert.message,
            data=alert.data,
            subject=alert.subject,
            _template=alert,
        )[0]

    def raise_alert(
        self,
        alert_type: str,
        severity: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        subject: str = "",
        _template: Optional[Alert] = None,
    ) -> Tuple[Alert, bool]:
        """Create or refresh the alert for ``type:subject``.

        Returns ``(alert, created)``. A refresh updates message/data/severity
        in place; subscribers are notified only on creation or escalation
        (warning → critical), and never twice for the same key inside the
        re-notify cooldown.
        """
        alert_type = str(alert_type)
        severity = str(severity)
        key = f"{alert_type}:{subject}"
        now = utcnow()
        notify = False
        created = False
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                escalated = SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(
                    str(existing.severity), 0
                )
                existing.message = message
                existing.data = dict(data or {})
                existing.severity = severity
                existing.updated_at = now
                existing.occurrences += 1
                if escalated:
                    # An escalation is new information: re-surface it even if
                    # the trader dismissed the milder version.
                    existing.dismissed_at = None
                    existing.dismissed_by = None
                    existing.reviewed_at = None
                    notify = True
                alert = existing
                event = "updated"
            else:
                alert = _template or Alert(
                    alert_type=alert_type,
                    severity=severity,
                    message=message,
                    data=dict(data or {}),
                    subject=subject,
                )
                alert.alert_type = alert_type
                alert.severity = severity
                alert.subject = subject
                self._by_key[key] = alert
                self._by_id[alert.alert_id] = alert
                self._history.append(alert)
                if len(self._by_id) > self._history.maxlen * 2:  # type: ignore[operator]
                    live_ids = {a.alert_id for a in self._history}
                    live_ids.update(a.alert_id for a in self._by_key.values())
                    self._by_id = {k: v for k, v in self._by_id.items() if k in live_ids}
                created = True
                event = "created"
                last = self._last_notified.get(key)
                notify = last is None or (now - last).total_seconds() >= self.renotify_cooldown_s
            subs: List[_Subscription] = []
            if notify:
                subs = [s for s in self._subscriptions if s.alert_type == alert_type]
            if notify:
                self._last_notified[key] = now
            if created or notify:
                self.version += 1
            listeners = list(self._listeners)

        if created:
            log = logger.warning if severity != Severity.INFO.value else logger.info
            log("[alert] %s %s — %s", severity.upper(), alert_type, message)
        if subs:
            self._notify_subscribers(alert, subs)
        self._emit(listeners, event, alert)
        return alert, created

    def _notify_subscribers(self, alert: Alert, subs: List[_Subscription]) -> None:
        results: List[Dict[str, Any]] = []
        for sub in subs:
            payload = dict(alert.data)
            payload.setdefault("alert_id", alert.alert_id)
            payload.setdefault("severity", str(alert.severity))
            payload.setdefault("message", alert.message)
            if sub.data_hook is not None:
                try:
                    payload = sub.data_hook(alert) or payload
                except Exception:  # noqa: BLE001 — enrichment is best-effort
                    logger.debug("[alert] data hook failed for %s", sub.subscriber_id)
            entry: Dict[str, Any] = {"subscriber_id": sub.subscriber_id, **sub.meta, "ok": True}
            try:
                sub.callback(str(alert.alert_type), payload)
            except Exception as exc:  # noqa: BLE001 — isolate every subscriber
                self.callback_failures += 1
                entry["ok"] = False
                entry["error"] = f"{exc.__class__.__name__}: {exc}"
                logger.error(
                    "[alert] strategy callback failed (%s on %s): %s",
                    sub.subscriber_id,
                    alert.alert_type,
                    exc,
                )
            entry["ts"] = utcnow().isoformat()
            results.append(entry)
        with self._lock:
            alert.notified_strategies.extend(results)
            self.version += 1

    def _emit(self, listeners: List[Listener], event: str, alert: Alert) -> None:
        for listener in listeners:
            try:
                listener(event, alert)
            except Exception:  # noqa: BLE001 — persistence must never break alerts
                logger.debug("[alert] listener failed on %s", event, exc_info=True)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def resolve_alert(self, alert_id: str, reason: str = "manual") -> Optional[Alert]:
        """Mark one alert resolved (PRD API name)."""
        with self._lock:
            alert = self._by_id.get(alert_id)
            if alert is None or alert.resolved:
                return alert
            self._resolve_locked(alert, reason)
        self._drain_resolved()
        return alert

    def resolve_key(self, alert_type: str, subject: str = "", reason: str = "cleared") -> bool:
        with self._lock:
            alert = self._by_key.get(f"{alert_type}:{subject}")
            if alert is None:
                return False
            self._resolve_locked(alert, reason)
        self._drain_resolved()
        return True

    def resolve_missing(
        self, alert_type: str, still_true: Iterable[str], reason: str = "cleared"
    ) -> List[Alert]:
        """Resolve every open ``alert_type`` alert whose subject is not in
        ``still_true`` — the evaluator's "condition no longer holds" sweep."""
        keep = {str(s) for s in still_true}
        resolved: List[Alert] = []
        with self._lock:
            for alert in list(self._by_key.values()):
                if str(alert.alert_type) == str(alert_type) and alert.subject not in keep:
                    resolved.append(self._resolve_locked(alert, reason))
        if resolved:
            self._drain_resolved()
        return resolved

    def _resolve_locked(self, alert: Alert, reason: str) -> Alert:
        alert.resolved_at = utcnow()
        alert.updated_at = alert.resolved_at
        alert.data = {**alert.data, "resolution": reason}
        self._by_key.pop(alert.key, None)
        self.version += 1
        notified = {n.get("subscriber_id") for n in alert.notified_strategies}
        subs = [
            sub
            for sub in self._subscriptions
            if sub.on_resolve is not None
            and sub.alert_type == str(alert.alert_type)
            and sub.subscriber_id in notified
        ]
        if subs:
            # Delivered by _drain_resolved() once the lock is released, so a
            # strategy callback (which takes its runner's lock) never runs
            # under the broker lock.
            self._pending_resolved.append((alert, subs))
        listeners = list(self._listeners)
        # Listeners outside the lock would be nicer, but resolve is called
        # from inside sweeps that already hold it; listeners are cheap and
        # never call back into the broker.
        self._emit(listeners, "resolved", alert)
        return alert

    def _drain_resolved(self) -> None:
        with self._lock:
            pending, self._pending_resolved = self._pending_resolved, []
        for alert, subs in pending:
            for sub in subs:
                payload = dict(alert.data)
                payload.setdefault("alert_id", alert.alert_id)
                payload.setdefault("message", alert.message)
                payload["resolved"] = True
                try:
                    sub.on_resolve(str(alert.alert_type), payload)  # type: ignore[misc]
                except Exception as exc:  # noqa: BLE001 — isolate every subscriber
                    self.callback_failures += 1
                    logger.error(
                        "[alert] resolve callback failed (%s on %s): %s",
                        sub.subscriber_id,
                        alert.alert_type,
                        exc,
                    )

    def dismiss(self, alert_id: str, by: str = "trader") -> Optional[Alert]:
        """Hide from the widget. The condition may still be true — the alert
        stays open (and keeps auto-resolving) but does not re-pop unless it
        escalates or resolves and fires again."""
        with self._lock:
            alert = self._by_id.get(alert_id)
            if alert is None:
                return None
            if alert.dismissed_at is None:
                alert.dismissed_at = utcnow()
                alert.dismissed_by = by
                alert.updated_at = alert.dismissed_at
                self.version += 1
            listeners = list(self._listeners)
        self._emit(listeners, "dismissed", alert)
        return alert

    def review(self, alert_id: str, by: str = "trader") -> Optional[Alert]:
        """Mark as reviewed (archived) — hidden from the widget, kept in history."""
        with self._lock:
            alert = self._by_id.get(alert_id)
            if alert is None:
                return None
            if alert.reviewed_at is None:
                alert.reviewed_at = utcnow()
                alert.updated_at = alert.reviewed_at
                if alert.dismissed_at is None:
                    alert.dismissed_at = alert.reviewed_at
                    alert.dismissed_by = by
                self.version += 1
            listeners = list(self._listeners)
        self._emit(listeners, "reviewed", alert)
        return alert

    def sweep(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """Time-based housekeeping: expire event alerts past their TTL and
        auto-dismiss ignored alerts (critical exempt by default — an unread
        critical condition that is still true must not quietly vanish)."""
        now = now or utcnow()
        expired = dismissed = 0
        listeners: List[Listener]
        to_emit: List[Tuple[str, Alert]] = []
        with self._lock:
            for alert in list(self._by_key.values()):
                age = (now - alert.created_at).total_seconds()
                if str(alert.alert_type) in EVENT_ALERT_TYPES and age >= self.event_ttl_s:
                    self._resolve_locked(alert, "expired")
                    expired += 1
                    continue
                if (
                    alert.dismissed_at is None
                    and alert.reviewed_at is None
                    and str(alert.severity) not in self.auto_dismiss_exempt
                    and age >= self.ignore_ttl_s
                ):
                    alert.dismissed_at = now
                    alert.dismissed_by = "auto"
                    self.version += 1
                    dismissed += 1
                    to_emit.append(("dismissed", alert))
            listeners = list(self._listeners)
        for event, alert in to_emit:
            self._emit(listeners, event, alert)
        self._drain_resolved()
        return {"expired": expired, "auto_dismissed": dismissed}

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def get(self, alert_id: str) -> Optional[Alert]:
        with self._lock:
            return self._by_id.get(alert_id)

    def open_alerts(self) -> List[Alert]:
        """Every unresolved alert (dismissed ones included)."""
        with self._lock:
            return list(self._by_key.values())

    def get_active_alerts(self, include_dismissed: bool = False) -> List[Alert]:
        """Unresolved alerts for the UI, most severe then newest first."""
        with self._lock:
            alerts = [a for a in self._by_key.values() if include_dismissed or a.visible]
        alerts.sort(
            key=lambda a: (-SEVERITY_RANK.get(str(a.severity), 0), -a.created_at.timestamp())
        )
        return alerts

    def history(
        self,
        since: Optional[datetime] = None,
        alert_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 200,
    ) -> List[Alert]:
        with self._lock:
            rows = list(self._history)
        out = []
        for alert in reversed(rows):  # newest first
            if since is not None and alert.created_at < since:
                continue
            if alert_type and alert_type.lower() not in str(alert.alert_type):
                continue
            if severity and str(alert.severity) != severity:
                continue
            out.append(alert)
            if len(out) >= limit:
                break
        return out

    def counts(self) -> Dict[str, int]:
        active = self.get_active_alerts()
        out = {"total": len(active), "critical": 0, "warning": 0, "info": 0}
        for a in active:
            out[str(a.severity)] = out.get(str(a.severity), 0) + 1
        return out

    def clear(self) -> None:
        """Forget everything (tests / manager reset). Subscriptions survive."""
        with self._lock:
            self._by_key.clear()
            self._by_id.clear()
            self._history.clear()
            self._last_notified.clear()
            self._pending_resolved = []
            self.version += 1


def since_hours(hours: float) -> datetime:
    return utcnow() - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Process-wide singleton (PRD: ``alert_broker``)
# ---------------------------------------------------------------------------

_BROKER: Optional[AlertBroker] = None
_BROKER_LOCK = threading.Lock()


def get_alert_broker() -> AlertBroker:
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = AlertBroker()
        return _BROKER


def reset_alert_broker(broker: Optional[AlertBroker] = None) -> AlertBroker:
    """Swap the singleton (tests)."""
    global _BROKER
    with _BROKER_LOCK:
        _BROKER = broker or AlertBroker()
        return _BROKER
