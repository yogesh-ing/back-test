"""Broker session manager v2 — concurrent multi-broker sessions (PRD-001).

Central registry holding **all live broker sessions simultaneously**,
exposed to the API routes, the Forward Engine, and the execution router:

* ``login`` / ``verify_totp`` / ``logout`` — thin delegation, now
  per-broker: logging into broker B never touches broker A's session.
* :meth:`BrokerSessionManager.get_session_token` — the only way engine
  code obtains a raw session token (per broker).
* :meth:`BrokerSessionManager.get_status` / ``get_all_status`` — status
  polling for the API (single broker, or the full map).
* ``_ui_active`` — which broker the UI highlights. **Display only**: it
  never gates order routing (that's the ExecutionRouter's job) and
  changing it never drops a session.

Session expiry background monitor (Task 2.2, extended): a daemon thread
polls **every** live broker session every 5 minutes. Transitions raise
per-broker flags — ``(broker, kind)`` events consumed by the API layer —
and an expired session has its token cleared via ``broker.logout()``. It
never auto-renews; the user must re-authenticate that broker manually.

Migration notes (V1 → V2):

* ``switch_broker()`` is DEPRECATED — it now only sets the UI-active
  broker and logs a deprecation warning. It NO LONGER drops the previous
  broker's session.
* All V1 single-broker call shapes (``login(user, pass)``,
  ``get_status()``, ``is_authenticated()``, ``logout()``) keep working
  and act on the UI-active broker, so a single-broker deployment behaves
  exactly as before.

Use :func:`get_session_manager` in application code (process-wide
singleton); construct :class:`BrokerSessionManager` directly (with an
injectable broker factory) in tests.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)

__all__ = [
    "BrokerSessionManager",
    "get_session_manager",
    "reset_default_manager",
    "available_brokers",
]

logger = logging.getLogger("backtest.brokers.session_manager")

# Task 2.2: poll session expiry every 5 minutes.
MONITOR_INTERVAL_SECONDS = 300.0


class UnknownBrokerError(ValueError):
    """Raised when a broker name is not in the registry (fail-closed)."""


# Broker registry (2026-09-25): maps ``broker_name`` → lazy factory. Adding a
# new broker later is a single entry here plus its own module under
# ``backtest.brokers`` — the session manager, API routes, and UI are
# already broker-agnostic.
_BROKER_REGISTRY: dict[str, Callable[[], BrokerAuthBase]] = {
    "mstock": lambda: _lazy("mstock"),
    "dhan": lambda: _lazy("dhan"),
}


def _lazy(module: str) -> BrokerAuthBase:
    """Instantiate a broker by module name under ``backtest.brokers``.

    Imported lazily so engine/API code importing this module has no direct
    dependency on any concrete broker class. Each module must expose a
    zero-argument-constructor broker class (MStockBroker / DhanBroker).
    """
    if module == "mstock":
        from backtest.brokers.mstock import MStockBroker

        return MStockBroker()
    if module == "dhan":
        from backtest.brokers.dhan import DhanBroker

        return DhanBroker()
    if module == "mock":
        from backtest.brokers.mock import MockBroker

        return MockBroker()
    raise ValueError(f"unknown broker module: {module}")


def enable_mock_broker() -> None:
    """Register the zero-credential MockBroker (Gap-PRD P5) — OPT-IN only.

    Deliberately not in the default registry: a dry-run venue must never
    silently appear in a production login UI. Called by
    ``create_app(source="mock_broker")`` / ``--source mock_broker``; tests
    may call it directly. Idempotent.
    """
    _BROKER_REGISTRY.setdefault("mock", lambda: _lazy("mock"))


def available_brokers() -> list[dict[str, str]]:
    """Brokers the login UI can offer, ordered (mStock first, then new ones).

    Returns ``[{"name", "display_name"}, ...]`` without instantiating any
    broker (display names are static class attributes).
    """
    display_names = {"mstock": "mStock", "dhan": "Dhan", "mock": "Mock (dry-run)"}
    return [
        {"name": name, "display_name": display_names.get(name, name.title())}
        for name in _BROKER_REGISTRY
    ]


def _default_broker_factory() -> BrokerAuthBase:
    """Create the default broker (mStock — keeps pre-registry behaviour)."""
    return _lazy("mstock")


# ----------------------------------------------------------------------
# Session alerts (in-app widget + outbound channels, e.g. Telegram)
# ----------------------------------------------------------------------
#
# The expiry monitor used to only log + set toast flags: a page left closed
# meant a dead broker session went unnoticed and the feeds silently stopped.
# Transitions now also publish platform alerts (dedupe key ``type:broker``),
# which the global alert widget surfaces on every page and the outbound
# notifier fans to the routed channels. Both helpers NEVER raise — the
# monitor thread must survive a broken alerts layer.


def _alert_broker():
    from backtest.alerts.broker import get_alert_broker

    return get_alert_broker()


def _raise_session_alert(alert_type_value: str, severity: str, key: str, message: str, data: dict) -> None:
    try:
        _alert_broker().raise_alert(alert_type_value, severity, message, data=data, subject=key)
    except Exception:  # noqa: BLE001 — publishing must never break the monitor
        logger.debug("session alert publish failed (%s)", alert_type_value, exc_info=True)


def _resolve_session_alerts(key: str) -> None:
    """Close both session alerts for one broker (re-auth / logout / recovery)."""
    try:
        from backtest.alerts.types import AlertType

        broker = _alert_broker()
        broker.resolve_key(AlertType.BROKER_SESSION_EXPIRING.value, key)
        broker.resolve_key(AlertType.BROKER_SESSION_EXPIRED.value, key)
    except Exception:  # noqa: BLE001
        logger.debug("session alert resolve failed for %s", key, exc_info=True)


class BrokerSessionManager:
    """Holds ALL live broker sessions and their lifecycles (v2).

    This is the only component outside the brokers themselves that ever
    touches a raw session token — and it never includes a token in
    ``get_status`` output or any API response.

    Invariants (PRD-001 §4.2):

    * logging into broker B MUST NOT touch broker A's session;
    * the expiry monitor iterates ALL sessions; notification flags are
      per ``(broker, kind)``;
    * remember-session persistence is per broker.
    """

    def __init__(self, broker_factory: Callable[[], BrokerAuthBase] | None = None) -> None:
        self._lock = threading.RLock()
        self._broker_factory = broker_factory or _default_broker_factory
        #: ALL broker sessions live here simultaneously: name → instance.
        self._sessions: dict[str, BrokerAuthBase] = {}
        #: Which broker the UI highlights. Display only — never routing.
        self._ui_active: str | None = None
        # Per-broker notification flags — set once per transition, consumed
        # by the API layer to fire a single toast per expiry cycle.
        self._expiring_soon_flags: dict[str, bool] = {}
        self._expired_flags: dict[str, bool] = {}
        self._last_observed_status: dict[str, str | None] = {}
        self._monitor_interval = MONITOR_INTERVAL_SECONDS
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        # Remember-session-today (2026-09-24, per-broker since Phase A):
        # seed the fresh manager from the opt-in saved store so a restart
        # does not force re-logins. Failures are non-fatal.
        try:
            self._restore_remembered_sessions()
        except Exception:  # noqa: BLE001 — a bad restore must never block boot
            logger.warning("remember-session restore failed", exc_info=True)

    # ------------------------------------------------------------------
    # Legacy compatibility surface (V1 attribute shapes used by tests)
    # ------------------------------------------------------------------

    @property
    def _broker(self) -> BrokerAuthBase | None:
        """V1 compat: the UI-active broker instance (None after shutdown)."""
        with self._lock:
            if self._ui_active is None:
                return None
            return self._sessions.get(self._ui_active)

    @property
    def _expiring_soon_flag(self) -> bool:
        with self._lock:
            return any(self._expiring_soon_flags.values())

    @_expiring_soon_flag.setter
    def _expiring_soon_flag(self, value: bool) -> None:
        with self._lock:
            broker = self.get_active_broker()
            key = self._normalize(getattr(broker, "broker_name", None)) or "unnamed"
            self._expiring_soon_flags[key] = bool(value)

    @property
    def _expired_flag(self) -> bool:
        with self._lock:
            return any(self._expired_flags.values())

    @_expired_flag.setter
    def _expired_flag(self, value: bool) -> None:
        with self._lock:
            broker = self.get_active_broker()
            key = self._normalize(getattr(broker, "broker_name", None)) or "unnamed"
            self._expired_flags[key] = bool(value)

    # ------------------------------------------------------------------
    # Session registry
    # ------------------------------------------------------------------

    def _normalize(self, broker_name: str | None) -> str | None:
        return (broker_name or "").strip().lower() or None

    def _broker_for(self, broker_name: str, create: bool = True) -> BrokerAuthBase:
        """The session instance for ``broker_name``, creating it lazily.

        Raises :class:`UnknownBrokerError` for names outside the registry
        (unless an instance was explicitly registered via ``set_broker`` —
        the tests' stub path).
        """
        key = self._normalize(broker_name)
        if key is None:
            raise UnknownBrokerError("broker name required")
        with self._lock:
            instance = self._sessions.get(key)
            if instance is not None:
                return instance
            if key not in _BROKER_REGISTRY:
                raise UnknownBrokerError(f"Unknown broker: {broker_name}")
            if not create:
                raise UnknownBrokerError(f"Broker not instantiated: {broker_name}")
            instance = _BROKER_REGISTRY[key]()
            self._sessions[key] = instance
            self._reset_flags(key)
            return instance

    def _resolve(self, broker_name: str | None) -> BrokerAuthBase:
        """Resolve a broker argument: explicit name, else the UI-active one."""
        key = self._normalize(broker_name)
        if key is not None:
            return self._broker_for(key)
        return self.get_active_broker()

    def _reset_flags(self, key: str) -> None:
        self._expiring_soon_flags[key] = False
        self._expired_flags[key] = False
        self._last_observed_status[key] = None

    def get_active_broker(self) -> BrokerAuthBase:
        """Return the UI-active broker, creating the default one on first use."""
        with self._lock:
            if self._ui_active is None:
                broker = self._broker_factory()
                key = self._normalize(getattr(broker, "broker_name", None)) or "mstock"
                # Don't clobber a session that already exists for this name
                # (e.g. restored from the remember-session store at boot).
                self._sessions.setdefault(key, broker)
                if key not in self._last_observed_status:
                    self._reset_flags(key)
                self._ui_active = key
            return self._sessions[self._ui_active]

    def get_broker(self, broker_name: str) -> BrokerAuthBase:
        """The session instance for one broker (execution-router seam)."""
        return self._broker_for(broker_name)

    def set_broker(self, broker: BrokerAuthBase) -> None:
        """Register an instance and make it UI-active (tests / injection)."""
        with self._lock:
            key = self._normalize(getattr(broker, "broker_name", None)) or "unnamed"
            self._sessions[key] = broker
            self._ui_active = key
            self._reset_flags(key)

    def set_ui_active(self, broker_name: str) -> dict[str, Any]:
        """Point the UI at ``broker_name``. Display only — drops NOTHING."""
        key = self._normalize(broker_name)
        if key is None or (key not in _BROKER_REGISTRY and key not in self._sessions):
            return {"success": False, "message": f"Unknown broker: {broker_name}"}
        with self._lock:
            self._broker_for(key)
            self._ui_active = key
        logger.info("UI-active broker → %s", key)
        return {"success": True, "broker": key, "status": self.get_status()}

    def switch_broker(self, broker_name: str) -> dict[str, Any]:
        """DEPRECATED (multi-broker PRD Phase A): maps to ``set_ui_active``.

        V1 semantics dropped the previous broker's session; V2 keeps every
        session alive. Kept one release for migration.
        """
        logger.warning(
            "switch_broker() is deprecated — it now only sets the UI-active "
            "broker and never drops sessions; use set_ui_active()/login(broker=...)"
        )
        result = self.set_ui_active(broker_name)
        result["deprecated"] = True
        return result

    def get_ui_active(self) -> str | None:
        with self._lock:
            return self._ui_active

    def session_names(self) -> list[str]:
        """Names of every instantiated broker session (any status)."""
        with self._lock:
            return list(self._sessions)

    # ------------------------------------------------------------------
    # Auth flow delegation (API routes depend only on these)
    # ------------------------------------------------------------------

    def login(
        self, username: str, password: str, broker_name: str | None = None
    ) -> dict[str, Any]:
        """Delegate credentials step to one broker (default: UI-active).

        Logging into one broker never touches any other broker's session.
        Credentials are passed through to the broker call only — never
        stored or logged here.
        """
        with self._lock:
            broker = self._resolve(broker_name)
            if broker_name is not None:
                # A broker-specific login also becomes the UI focus so the
                # follow-up verify_totp (legacy single-broker clients) lands
                # on the same broker.
                self._ui_active = self._normalize(broker.broker_name) or self._ui_active
            return broker.login(username, password)

    def verify_totp(self, totp_code: str, broker_name: str | None = None) -> dict[str, Any]:
        """Delegate TOTP step; on success reset THAT broker's expiry cycle."""
        with self._lock:
            broker = self._resolve(broker_name)
            key = self._normalize(broker.broker_name) or "unnamed"
            result = broker.verify_totp(totp_code)
        if result.get("success"):
            with self._lock:
                self._expiring_soon_flags[key] = False
                self._expired_flags[key] = False
                self._last_observed_status[key] = STATUS_AUTHENTICATED
            # A fresh session resolves any open session alerts immediately —
            # the widget/Telegram shouldn't wait for the next monitor tick.
            _resolve_session_alerts(key)
            # Remember-session-today: when the toggle is ON the freshly
            # established session is persisted (per broker) for the next boot.
            self._save_remembered_session(key)
        return result

    def logout(self, broker_name: str | None = None) -> None:
        """Clear ONE broker's session (default: UI-active). Others stay live."""
        with self._lock:
            broker = self._resolve(broker_name)
            key = self._normalize(broker.broker_name) or "unnamed"
            broker.logout()
            self._reset_flags(key)
        # An explicit logout is deliberate — close any open session alerts.
        _resolve_session_alerts(key)
        # An explicit logout also forgets that broker's remembered session —
        # keeping a dead session on disk after the user said "log out" is
        # wrong. Other REGISTRY brokers' remembered sessions are preserved;
        # a duck-typed (non-registry) broker keeps the legacy whole-store
        # delete (single-broker semantics).
        try:
            from backtest.brokers.remember_session import delete_saved_session

            delete_saved_session(key if key in _BROKER_REGISTRY else None)
        except Exception:  # noqa: BLE001 — a delete failure must never break logout
            logger.warning("remember-session delete on logout failed", exc_info=True)

    # ------------------------------------------------------------------
    # Status / token access
    # ------------------------------------------------------------------

    def get_status(self, broker_name: str | None = None) -> dict[str, Any]:
        """Poll-friendly session status for ONE broker (never the token).

        ``broker_name=None`` keeps the V1 shape for the UI-active broker.
        """
        with self._lock:
            broker = self._resolve(broker_name)
            status = broker.get_session_status()
            return {
                "status": status.get("status"),
                "broker": status.get("broker", broker.broker_name),
                "broker_display_name": getattr(broker, "broker_display_name", broker.broker_name),
                "expires_at": status.get("expires_at"),
            }

    def get_all_status(self) -> dict[str, dict[str, Any]]:
        """Status map for EVERY broker: registry entries + live instances.

        Registered-but-never-instantiated brokers report ``unauthenticated``
        without being constructed (cheap poll path).
        """
        display_names = {b["name"]: b["display_name"] for b in available_brokers()}
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            names = list(dict.fromkeys(list(_BROKER_REGISTRY) + list(self._sessions)))
            for name in names:
                instance = self._sessions.get(name)
                if instance is None:
                    out[name] = {
                        "broker": name,
                        "broker_display_name": display_names.get(name, name.title()),
                        "status": STATUS_UNAUTHENTICATED,
                        "authenticated": False,
                        "expires_at": None,
                    }
                    continue
                try:
                    status = instance.get_session_status()
                except Exception:  # noqa: BLE001 — one broken broker must not hide the rest
                    logger.exception("status poll failed for %s", name)
                    status = {"status": STATUS_UNAUTHENTICATED, "expires_at": None}
                state = status.get("status")
                out[name] = {
                    "broker": name,
                    "broker_display_name": getattr(
                        instance, "broker_display_name", display_names.get(name, name.title())
                    ),
                    "status": state,
                    "authenticated": state in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON),
                    "expires_at": status.get("expires_at"),
                }
            if self._ui_active:
                for name in out:
                    out[name]["ui_active"] = name == self._ui_active
        return out

    def is_authenticated(self, broker_name: str | None = None) -> bool:
        """True while that broker's session is valid (default: UI-active).

        Used by the Forward Test start guard (Tasks 4.1/4.2) and the
        execution router's fail-closed check.
        """
        try:
            status = self.get_status(broker_name)["status"]
        except UnknownBrokerError:
            return False
        return status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON)

    def get_active_session_token(self, broker_name: str | None = None) -> str | None:
        """Raw session token for backend use only (default: UI-active broker).

        Returns ``None`` unless that session is currently valid. The value
        must never be serialized into an API response or log line.
        """
        with self._lock:
            try:
                broker = self._resolve(broker_name)
            except UnknownBrokerError:
                return None
            status = broker.get_session_status().get("status")
            if status not in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
                return None
            getter = getattr(broker, "get_session_token", None)
            if not callable(getter):
                return None
            return getter()

    def get_session_token(self, broker_name: str) -> str | None:
        """Per-broker token accessor (PRD §4.2 naming) — internal use only."""
        return self.get_active_session_token(broker_name)

    def get_authenticated_broker(self, broker_name: str) -> BrokerAuthBase | None:
        """The broker instance IF its session is valid, else ``None``.

        Execution/data seams use this so an expired session yields a clean
        refusal instead of a half-sent order.
        """
        try:
            if self.is_authenticated(broker_name):
                return self._broker_for(broker_name)
        except UnknownBrokerError:
            return None
        return None

    # ------------------------------------------------------------------
    # Expiry notifications (consumed by the API layer for toasts)
    # ------------------------------------------------------------------

    def consume_expiring_soon_notification(self) -> bool:
        """True once per ``expiring_soon`` transition on ANY broker (V1 shape)."""
        with self._lock:
            value = any(self._expiring_soon_flags.values())
            for key in self._expiring_soon_flags:
                self._expiring_soon_flags[key] = False
            return value

    def consume_expired_notification(self) -> bool:
        """True once per ``expired`` transition on ANY broker (V1 shape)."""
        with self._lock:
            value = any(self._expired_flags.values())
            for key in self._expired_flags:
                self._expired_flags[key] = False
            return value

    def consume_expiry_events(self) -> list[dict[str, str]]:
        """Per-broker expiry events since the last call: ``[{broker, kind}]``.

        ``kind`` is ``expiring_soon`` or ``expired``. Consuming clears the
        flags, so each transition is reported exactly once (toast guard).
        """
        events: list[dict[str, str]] = []
        with self._lock:
            for key, flagged in self._expiring_soon_flags.items():
                if flagged:
                    events.append({"broker": key, "kind": "expiring_soon"})
                    self._expiring_soon_flags[key] = False
            for key, flagged in self._expired_flags.items():
                if flagged:
                    events.append({"broker": key, "kind": "expired"})
                    self._expired_flags[key] = False
        return events

    # ------------------------------------------------------------------
    # Task 2.2 — session expiry background monitor (all sessions)
    # ------------------------------------------------------------------

    def start_monitor(self, interval_seconds: float | None = None) -> bool:
        """Start the daemon expiry-monitor thread (idempotent).

        Returns ``True`` if this call started a thread, ``False`` if one is
        already running.
        """
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return False
            if interval_seconds is not None:
                self._monitor_interval = max(float(interval_seconds), 0.01)
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="broker-session-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
            return True

    def stop_monitor(self, timeout: float = 5.0) -> None:
        """Stop the monitor thread (app shutdown / tests)."""
        with self._lock:
            thread = self._monitor_thread
            self._monitor_stop.set()
            self._monitor_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def shutdown(self) -> None:
        """Stop the monitor and drop all session/notification state."""
        self.stop_monitor()
        with self._lock:
            self._expiring_soon_flags.clear()
            self._expired_flags.clear()
            self._last_observed_status.clear()
            self._sessions.clear()
            self._ui_active = None

    # ------------------------------------------------------------------
    # Remember-session-today — per-broker boot seeding + save
    # ------------------------------------------------------------------

    def _restore_remembered_sessions(self) -> int:
        """Seed broker sessions from the saved remember-session store.

        Only acts when the toggle is ON. Restores each broker's in-memory
        token + expiry so the app boots Authenticated without re-logins.
        Returns the number of sessions restored.
        """
        from backtest.brokers.remember_session import get_toggle, load_sessions

        if not get_toggle():
            return 0
        saved_map = load_sessions()
        if not saved_map:
            return 0
        # V1-compat seam: with an INJECTED factory (tests / duck-typed
        # brokers) the saved session is restored onto the factory broker —
        # exactly the legacy single-broker behaviour.
        if self._broker_factory is not _default_broker_factory:
            broker = self.get_active_broker()
            key = self._normalize(getattr(broker, "broker_name", None)) or "unnamed"
            saved = saved_map.get(key) or (
                next(iter(saved_map.values())) if len(saved_map) == 1 else None
            )
            setter = getattr(broker, "restore_session", None)
            if saved is None or not callable(setter):
                return 0
            setter(saved["token"], saved["expires_at"])
            logger.info(
                "remember-session: %s session restored from disk (expires %s)",
                key,
                saved["expires_at"].isoformat(),
            )
            return 1
        restored = 0
        for name, saved in saved_map.items():
            key = self._normalize(name)
            if key is None:
                continue
            try:
                broker = self._broker_for(key)
            except UnknownBrokerError:
                logger.info("remember-session: unknown broker %s in store — skipping", name)
                continue
            setter = getattr(broker, "restore_session", None)
            if not callable(setter):
                logger.info(
                    "remember-session: %s has no restore_session() — skipping", key
                )
                continue
            setter(saved["token"], saved["expires_at"])
            restored += 1
            logger.info(
                "remember-session: %s session restored from disk (expires %s)",
                key,
                saved["expires_at"].isoformat(),
            )
        return restored

    def _save_remembered_session(self, broker_name: str | None = None) -> None:
        """Persist one live session when the toggle is ON (best-effort)."""
        try:
            from backtest.brokers.remember_session import get_toggle, save_session

            if not get_toggle():
                return
            with self._lock:
                broker = self._resolve(broker_name)
                key = self._normalize(broker.broker_name) or "unnamed"
                token = self.get_active_session_token(key)
                expires_at = broker.get_session_status().get("expires_at")
            if not token or not expires_at:
                return
            from datetime import datetime as _dt

            save_session(token, _dt.fromisoformat(str(expires_at)), broker=key)
        except Exception:  # noqa: BLE001 — a save must never break login
            logger.warning("remember-session save failed", exc_info=True)

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self._monitor_interval):
            self._poll_once()

    def _poll_once(self) -> None:
        """One monitor tick over ALL sessions: flag transitions, clear expired.

        Never auto-renews — after expiry the user must re-authenticate that
        broker. While a session is live and the remember-toggle is ON, its
        saved entry is refreshed each tick so a restore always carries the
        current token + full remaining validity.
        """
        with self._lock:
            sessions = list(self._sessions.items())
            if not sessions:
                # V1 behaviour: polling an empty manager materializes the
                # default broker (reports unauthenticated harmlessly).
                broker = self.get_active_broker()
                sessions = [(self._ui_active or broker.broker_name, broker)]

        for key, broker in sessions:
            with self._lock:
                try:
                    info = broker.get_session_status()
                    status = info.get("status")
                except Exception:
                    logger.exception("broker status poll failed for %s", key)
                    continue
                display = getattr(broker, "broker_display_name", key)
                expires_at = info.get("expires_at")

                previous = self._last_observed_status.get(key)
                if status == STATUS_EXPIRING_SOON and previous != STATUS_EXPIRING_SOON:
                    self._expiring_soon_flags[key] = True
                    logger.warning(
                        "%s session expiring soon — re-authentication advised", key
                    )
                    _raise_session_alert(
                        "broker_session_expiring",
                        "warning",
                        key,
                        f"{display} login expires in under 30 minutes"
                        f" ({expires_at or 'shortly'}) — re-authenticate (password + TOTP)"
                        " to keep live feeds and runners running.",
                        {"broker": key, "broker_display_name": display, "expires_at": expires_at},
                    )
                elif status == STATUS_EXPIRED:
                    first_observation = previous != STATUS_EXPIRED
                    if first_observation:
                        self._expired_flags[key] = True
                    broker.logout()  # clear the token; no auto-renew
                    logger.warning(
                        "%s session expired — token cleared, manual re-authentication required",
                        key,
                    )
                    try:
                        from backtest.brokers.remember_session import delete_saved_session

                        delete_saved_session(key)
                    except Exception:  # noqa: BLE001 — cleanup is best-effort
                        pass
                    if first_observation:
                        # The expiring alert (if still open) is superseded.
                        # Raise only on the transition: resolving + re-raising
                        # every tick would re-notify Telegram every 5 minutes.
                        _resolve_session_alerts(key)
                        _raise_session_alert(
                            "broker_session_expired",
                            "critical",
                            key,
                            f"{display} session EXPIRED — live feeds, forward entries and "
                            "portfolio marks are paused. Re-login (password + TOTP) now.",
                            {"broker": key, "broker_display_name": display, "expires_at": None},
                        )
                elif status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
                    if status == STATUS_AUTHENTICATED:
                        # Recovered (re-login / renewed token): close any open
                        # session alerts so the widget and Telegram clear.
                        _resolve_session_alerts(key)
                    # Keep the remembered entry fresh (remaining validity
                    # shrinks every tick; a stale entry would restore a
                    # soon-dead session).
                    try:
                        self._save_remembered_session(key)
                    except Exception:  # noqa: BLE001 — refresh must never break the monitor
                        logger.debug("remember-session refresh failed", exc_info=True)
                self._last_observed_status[key] = status


# ----------------------------------------------------------------------
# Process-wide singleton (use this in application code)
# ----------------------------------------------------------------------

_default_manager: BrokerSessionManager | None = None
_default_manager_lock = threading.Lock()


def get_session_manager() -> BrokerSessionManager:
    """Return the process-wide :class:`BrokerSessionManager` singleton."""
    global _default_manager
    if _default_manager is None:
        with _default_manager_lock:
            if _default_manager is None:
                _default_manager = BrokerSessionManager()
    return _default_manager


def reset_default_manager() -> None:
    """Drop the singleton (tests only). Stops its monitor thread if running."""
    global _default_manager
    with _default_manager_lock:
        if _default_manager is not None:
            _default_manager.shutdown()
        _default_manager = None
