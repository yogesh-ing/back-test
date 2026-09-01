"""Generic broker contract layer (mStock Authentication UI epic + P3.x).

Defines the broker-agnostic contracts every broker implementation must
satisfy:

* :class:`BrokerAuthBase` — two-step auth (Task 1.1): mStock is the only
  implementation today; Zerodha / Upstox join later by subclassing it.
* :class:`BrokerOrderBase` — the order lifecycle (ticket P3.1): place /
  modify / cancel / book / margin. A live broker composes BOTH contracts
  (``class MStockBroker(BrokerAuthBase, BrokerOrderBase)``); an
  auth-only stub must not inherit the order contract.

Sessions established through the auth layer are consumed exclusively by
the Forward Testing engine and by order calls (P3.2+). Raw session
tokens never leave the backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BrokerAuthBase",
    "BrokerOrderBase",
    "BrokerOrderId",
    "BrokerOrder",
    "MarginInfo",
    "ORDER_STATUSES",
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


# ---------------------------------------------------------------------------
# Order lifecycle values (ticket P3.1)
# ---------------------------------------------------------------------------

#: Every value a :class:`BrokerOrder` may report for ``status``.
ORDER_STATUSES = frozenset({"OPEN", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"})


# ---------------------------------------------------------------------------
# Order contract value types (ticket P3.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerOrderId:
    """A broker-issued order identifier (wraps the raw string id)."""

    value: str

    def __str__(self) -> str:  # ``f"/orders/{order_id}"`` stays URL-safe
        return self.value

    def __repr__(self) -> str:
        return f"BrokerOrderId({self.value!r})"


@dataclass
class BrokerOrder:
    """One order from the broker's point of view.

    Deliberately generic — the fields are the union the order lifecycle
    needs (place / modify / cancel / book / margin); broker-specific
    extensions (segment, product, disclosure) are optional.
    """

    broker_order_id: BrokerOrderId | None = None
    client_order_id: str | None = None
    symbol: str = ""
    side: str = "BUY"  # "BUY" | "SELL"
    quantity: int = 0
    order_type: str = "MARKET"  # "MARKET" | "LIMIT" | "STOP_LOSS" | ...
    limit_price: float | None = None
    status: str = "OPEN"
    filled_quantity: int = 0
    average_fill_price: float | None = None
    exchange: str | None = None
    product: str | None = None  # e.g. "INTRADAY" | "DELIVERY" | "MARGIN"
    created_at: str | None = None
    tag: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MarginInfo:
    """Margin requirement for a prospective order (pre-trade check)."""

    initial_margin: float
    maintenance_margin: float
    available_margin: float | None = None
    is_funded: bool = True


# ---------------------------------------------------------------------------
# Order lifecycle contract (ticket P3.1)
# ---------------------------------------------------------------------------


class BrokerOrderBase(ABC):
    """Abstract order lifecycle contract — a broker that trades must provide all five.

    Composed with :class:`BrokerAuthBase` by live brokers; the order calls
    may only execute while an authenticated session is active (token from
    :meth:`BrokerAuthBase.verify_totp`) — an unauthenticated order call must
    fail cleanly, never half-send.

    * ``place_order(order)``              → the broker's order id
    * ``modify_order(order)``             → amend quantity/price/type
    * ``cancel_order(order)``             → cancel one open order
    * ``get_order_book()``                → every order the broker knows
    * ``calculate_order_margin(order)``   → pre-trade margin check
    """

    broker_name: str = "unnamed"

    @abstractmethod
    def place_order(self, order: BrokerOrder) -> BrokerOrderId:
        """Place ``order`` with the broker and return the broker's order id.

        ``order.client_order_id`` is the idempotency key the broker should
        echo back (client-side dedupe); the returned
        :class:`BrokerOrderId` is what modify/cancel reference.
        """

    @abstractmethod
    def modify_order(self, order: BrokerOrder) -> None:
        """Amend the open order ``order.broker_order_id`` (qty/price/type).

        Raises on an unknown or already-settled id.
        """

    @abstractmethod
    def cancel_order(self, order: BrokerOrder) -> None:
        """Cancel the open order ``order.broker_order_id``.

        Raises on an unknown or already-settled id.
        """

    @abstractmethod
    def get_order_book(self) -> list[BrokerOrder]:
        """Every order the broker currently knows for this session."""

    @abstractmethod
    def calculate_order_margin(self, order: BrokerOrder) -> MarginInfo:
        """Margin the broker would require for ``order`` (without placing it)."""
