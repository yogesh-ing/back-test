"""Broker session manager (mStock Authentication UI epic, Tasks 1.3 + 2.2).

Central registry holding the single active broker instance, exposed to the
API routes and the Forward Engine:

* ``login`` / ``verify_totp`` / ``logout`` — thin delegation so callers
  depend only on the manager, never on a concrete broker class.
* :meth:`BrokerSessionManager.get_active_session_token` — the Forward
  Engine's only way to obtain the raw session token.
* :meth:`BrokerSessionManager.get_status` — status polling for the API.

Task 2.2 — session expiry background monitor: a daemon thread polls the
broker session every 5 minutes. Transition to ``expiring_soon`` raises an
internal flag (one notification per expiry cycle); transition to ``expired``
clears the session token via ``broker.logout()``. It never auto-renews —
the user must re-authenticate manually.

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
    BrokerAuthBase,
)

__all__ = ["BrokerSessionManager", "get_session_manager", "reset_default_manager"]

logger = logging.getLogger("backtest.brokers.session_manager")

# Task 2.2: poll session expiry every 5 minutes.
MONITOR_INTERVAL_SECONDS = 300.0


def _default_broker_factory() -> BrokerAuthBase:
    """Create the default broker (mStock — the only implementation today).

    Imported lazily so that engine/API code importing this module has no
    direct dependency on :class:`~backtest.brokers.mstock.MStockBroker`;
    future brokers plug in via ``set_broker`` / a custom factory.
    """
    from backtest.brokers.mstock import MStockBroker

    return MStockBroker()


class BrokerSessionManager:
    """Holds the single active broker instance and its session lifecycle.

    This is the only component outside the broker itself that ever touches
    the raw session token — and it never includes the token in ``get_status``
    output or any API response.
    """

    def __init__(self, broker_factory: Callable[[], BrokerAuthBase] | None = None) -> None:
        self._lock = threading.RLock()
        self._broker_factory = broker_factory or _default_broker_factory
        self._broker: BrokerAuthBase | None = None
        # Task 2.2 notification flags — set once per transition, consumed by
        # the API layer to fire a single toast per expiry cycle.
        self._expiring_soon_flag = False
        self._expired_flag = False
        self._last_observed_status: str | None = None
        self._monitor_interval = MONITOR_INTERVAL_SECONDS
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Active broker registry
    # ------------------------------------------------------------------

    def get_active_broker(self) -> BrokerAuthBase:
        """Return the active broker, creating the default one on first use."""
        with self._lock:
            if self._broker is None:
                self._broker = self._broker_factory()
            return self._broker

    def set_broker(self, broker: BrokerAuthBase) -> None:
        """Swap the active broker instance (future broker selector / tests)."""
        with self._lock:
            self._broker = broker
            self._expiring_soon_flag = False
            self._expired_flag = False
            self._last_observed_status = None

    # ------------------------------------------------------------------
    # Auth flow delegation (API routes depend only on these)
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Delegate credentials step to the active broker.

        Credentials are passed through to the broker call only — never
        stored or logged here.
        """
        with self._lock:
            broker = self.get_active_broker()
            return broker.login(username, password)

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        """Delegate TOTP step; on success reset the expiry notification cycle."""
        with self._lock:
            result = self.get_active_broker().verify_totp(totp_code)
        if result.get("success"):
            with self._lock:
                self._expiring_soon_flag = False
                self._expired_flag = False
                self._last_observed_status = STATUS_AUTHENTICATED
        return result

    def logout(self) -> None:
        """Clear the active session and all notification state."""
        with self._lock:
            self.get_active_broker().logout()
            self._expiring_soon_flag = False
            self._expired_flag = False
            self._last_observed_status = None

    # ------------------------------------------------------------------
    # Status / token access
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Poll-friendly session status (never includes the session token)."""
        with self._lock:
            broker = self.get_active_broker()
            status = broker.get_session_status()
            return {
                "status": status.get("status"),
                "broker": status.get("broker", broker.broker_name),
                "broker_display_name": getattr(broker, "broker_display_name", broker.broker_name),
                "expires_at": status.get("expires_at"),
            }

    def is_authenticated(self) -> bool:
        """True while a valid session exists (``authenticated`` or ``expiring_soon``).

        Used by the Forward Test start guard (Tasks 4.1/4.2).
        """
        return self.get_status()["status"] in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON)

    def get_active_session_token(self) -> str | None:
        """Raw session token for the Forward Engine — backend use only.

        Returns ``None`` unless the session is currently valid. Brokers are
        expected to expose ``get_session_token()`` (as ``MStockBroker`` does);
        a broker without that accessor yields no token rather than an error.
        The value must never be serialized into an API response or log line.
        """
        with self._lock:
            broker = self.get_active_broker()
            status = broker.get_session_status().get("status")
            if status not in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
                return None
            getter = getattr(broker, "get_session_token", None)
            if not callable(getter):
                return None
            return getter()

    # ------------------------------------------------------------------
    # Expiry notifications (consumed by the API layer for toasts)
    # ------------------------------------------------------------------

    def consume_expiring_soon_notification(self) -> bool:
        """True once per ``expiring_soon`` transition (Task 3.3 toast guard)."""
        with self._lock:
            value = self._expiring_soon_flag
            self._expiring_soon_flag = False
            return value

    def consume_expired_notification(self) -> bool:
        """True once per ``expired`` transition (session-expired toast)."""
        with self._lock:
            value = self._expired_flag
            self._expired_flag = False
            return value

    # ------------------------------------------------------------------
    # Task 2.2 — session expiry background monitor
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
            self._expiring_soon_flag = False
            self._expired_flag = False
            self._last_observed_status = None
            self._broker = None

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self._monitor_interval):
            self._poll_once()

    def _poll_once(self) -> None:
        """One monitor tick: flag ``expiring_soon``, clear expired sessions.

        Never auto-renews — after expiry the user must re-authenticate.
        """
        with self._lock:
            broker = self.get_active_broker()
            try:
                status = broker.get_session_status().get("status")
            except Exception:
                logger.exception("broker status poll failed for %s", broker.broker_name)
                return

            previous = self._last_observed_status
            if status == STATUS_EXPIRING_SOON and previous != STATUS_EXPIRING_SOON:
                self._expiring_soon_flag = True
                logger.warning(
                    "%s session expiring soon — re-authentication advised",
                    broker.broker_name,
                )
            elif status == STATUS_EXPIRED:
                if previous != STATUS_EXPIRED:
                    self._expired_flag = True
                broker.logout()  # clear the token; no auto-renew
                logger.warning(
                    "%s session expired — token cleared, manual re-authentication required",
                    broker.broker_name,
                )
            self._last_observed_status = status


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
