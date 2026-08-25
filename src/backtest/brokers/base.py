"""Generic broker authentication layer (mStock Authentication UI epic).

Defines the broker-agnostic two-step auth contract that every broker
implementation must satisfy. ``mStock`` is the only implementation today
(Task 1.2); Zerodha / Upstox join later by subclassing
:class:`~backtest.brokers.base.BrokerAuthBase` — no other layer changes.

Sessions established through this layer are consumed exclusively by the
Forward Testing engine. Raw session tokens never leave the backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = [
    "BrokerAuthBase",
    "STATUS_UNAUTHENTICATED",
    "STATUS_AUTHENTICATED",
    "STATUS_EXPIRING_SOON",
    "STATUS_EXPIRED",
    "SESSION_STATUSES",
]


# ---------------------------------------------------------------------------
# Session status values (Session State Machine in
# instructions/Generic_Broker_Authentication.md)
# ---------------------------------------------------------------------------

STATUS_UNAUTHENTICATED = "unauthenticated"
STATUS_AUTHENTICATED = "authenticated"
STATUS_EXPIRING_SOON = "expiring_soon"
STATUS_EXPIRED = "expired"

#: Every value ``get_session_status()`` may report for ``status``.
SESSION_STATUSES = frozenset(
    {
        STATUS_UNAUTHENTICATED,
        STATUS_AUTHENTICATED,
        STATUS_EXPIRING_SOON,
        STATUS_EXPIRED,
    }
)


class BrokerAuthBase(ABC):
    """Abstract two-step broker authentication contract (Task 1.1).

    Every broker implementation must provide:

    * ``login(username, password)``  — step 1 (credentials)
    * ``verify_totp(totp_code)``     — step 2 (TOTP from authenticator app)
    * ``get_session_status()``       — status polling for the UI / engine guard
    * ``logout()``                   — clear all in-memory session state

    Security invariants for all implementations:

    * Credentials arrive at call time from the UI and are never stored,
      logged, or echoed in any response once the call completes.
    * The raw session token stays inside the backend (broker instance /
      session manager). It is never returned to the browser.
    * All session state is held in-memory only (lost on restart, by design
      for this epic).
    """

    broker_name: str = "unnamed"
    broker_display_name: str = "Unknown Broker"

    @abstractmethod
    def login(self, username: str, password: str) -> dict[str, Any]:
        """Step 1 auth — verify credentials.

        Parameters
        ----------
        username:
            User's broker account username (from the UI at runtime).
        password:
            User's broker account password. Used for this call only, then
            discarded — never stored on the instance.

        Returns
        -------
        dict
            ``{"success": bool, "message": str, "requires_totp": bool}``
            where ``requires_totp`` signals the UI to enable the TOTP field.
        """

    @abstractmethod
    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        """Step 2 auth — finalize the session with a TOTP code.

        Must only succeed when preceded by a successful ``login()`` (the
        temp auth context from step 1 links the two calls).

        Returns
        -------
        dict
            ``{"success": bool, "message": str, "expires_at": str}``
            with ``expires_at`` an ISO-8601 timestamp (or empty string on
            failure). On success the implementation stores the session
            token + expiry in memory and clears the temp auth context.
        """

    @abstractmethod
    def get_session_status(self) -> dict[str, Any]:
        """Report the current session status for API polling / UI gating.

        Computed by comparing the current time against the in-memory
        ``expires_at``; implementations report ``expiring_soon`` when less
        than 30 minutes remain.

        Returns
        -------
        dict
            ``{"status": str, "expires_at": str | None, "broker": str}``
            where ``status`` is one of :data:`SESSION_STATUSES`
            (``unauthenticated | authenticated | expiring_soon | expired``)
            and ``broker`` is this broker's ``broker_name``.
        """

    @abstractmethod
    def logout(self) -> None:
        """Clear all in-memory session state (token, expiry, temp context)."""
