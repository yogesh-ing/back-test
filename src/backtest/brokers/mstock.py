"""mStock broker (mStock Authentication UI epic, Task 1.2; orders: P3.1/P3.2).

Implements both generic contracts from :mod:`backtest.brokers.base` for
mStock's TypeA connect API:

* :class:`BrokerAuthBase` — mirroring the endpoint contract already used by
  :mod:`backtest.live.auth`:
  step 1 — ``POST /openapi/typea/connect/login`` (Username + Password)
  step 2 — ``POST /openapi/typea/session/verifytotp`` (api_key + totp)
* :class:`BrokerOrderBase` (ticket P3.2) — order lifecycle against the
  TypeA order endpoints, reusing the session established by the auth flow:
  authenticated calls carry ``Authorization: token <api_key>:<access_token>``.
  ``place_order`` → ``POST /openapi/typea/orders/regular`` (form packet)
  ``modify_order`` → ``PUT /openapi/typea/orders/regular/{id}``
  ``cancel_order`` → ``DELETE /openapi/typea/orders/regular/{id}``
  ``get_order_book`` → ``GET /openapi/typea/orders``
  ``calculate_order_margin`` → ``POST /openapi/typea/margins/orders`` (JSON)
  (endpoint map: ``docs/archive/mstock-typea-api-reference.md``)

API credentials (``MSTOCK_API_KEY`` etc.) come from ``.env`` — loaded at
import time via python-dotenv by ``backtest/__init__.py``. The username and
password arrive at runtime from the UI and are never stored: they live in
local variables for the duration of the ``login()`` call only.

All session state is in-memory only, per the PRD. The raw session token
never appears in any return value of the contract methods, and no order
call may go out without a live session (fail cleanly, never half-send).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any

import requests

from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
    BrokerOrder,
    BrokerOrderBase,
    BrokerOrderId,
    MarginInfo,
)

__all__ = ["MStockBroker", "MStockOrderError"]

logger = logging.getLogger("backtest.brokers.mstock")

# mStock TypeA endpoints (same contract as backtest.live.auth).
_LOGIN_PATH = "/openapi/typea/connect/login"
_VERIFY_TOTP_PATH = "/openapi/typea/session/verifytotp"

# mStock TypeA order endpoints (ticket P3.2; route table in
# docs/archive/mstock-typea-api-reference.md).
_ORDER_PLACEMENT_PATH = "/openapi/typea/orders/regular"
_ORDER_PATH_TEMPLATE = "/openapi/typea/orders/regular/"
_ORDER_BOOK_PATH = "/openapi/typea/orders"
_ORDER_MARGIN_PATH = "/openapi/typea/margins/orders"

_TYPEA_HEADERS = {
    "X-Mirae-Version": "1",
    "Content-Type": "application/x-www-form-urlencoded",
}

_TOTP_PATTERN = re.compile(r"\d{6}")

# Fallback session lifetime when the API response carries no expiry hint.
# Defaults to a trading-session length (6.5h); override via
# MSTOCK_SESSION_TTL_MINUTES in .env.
DEFAULT_SESSION_TTL_MINUTES = 390.0

# PRD session state machine: "expiring_soon" inside the last 30 minutes.
EXPIRING_SOON_WINDOW_MINUTES = 30.0

# Payload keys that signal a rejected request / carry a user-facing reason.
_ERROR_KEYS = ("error", "error_message", "errorMessage")
_MAX_MESSAGE_LEN = 200


class _MStockAuthError(Exception):
    """mStock rejected the request (bad credentials, bad TOTP, error payload)."""


class MStockOrderError(RuntimeError):
    """An order lifecycle call could not be completed (ticket P3.2).

    Raised when there is no active session, the order has no broker id,
    mStock answers non-2xx, or the payload carries an error reason. The
    message is user-facing (no stack traces or internal details).
"""


def _rejection_reason(payload: Any) -> str | None:
    """Extract a user-facing rejection reason from an mStock payload, if any."""
    if not isinstance(payload, dict):
        return None
    for key in _ERROR_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_MESSAGE_LEN]
    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() not in ("success", "ok", ""):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:_MAX_MESSAGE_LEN]
        return status.strip()[:_MAX_MESSAGE_LEN]
    return None


class MStockBroker(BrokerAuthBase, BrokerOrderBase):
    """mStock implementation of the auth + order contracts.

    State held in-memory only; lost on restart by design (this epic). The
    temp auth context links a successful ``login()`` to the subsequent
    ``verify_totp()`` call and holds *server-returned* data only — never the
    username or password.

    Order calls (P3.2) reuse the session established by the auth flow;
    every one is guarded by :meth:`_require_session` so an unauthenticated
    broker can never half-send an order.
    """

    broker_name = "mstock"
    broker_display_name = "mStock"

    def __init__(
        self,
        session_ttl_minutes: float | None = None,
        http_timeout: float = 10.0,
    ) -> None:
        self._session_token: str | None = None
        self._expires_at: datetime | None = None
        self._temp_auth_context: dict[str, Any] | None = None
        if session_ttl_minutes is None:
            try:
                session_ttl_minutes = float(
                    os.getenv("MSTOCK_SESSION_TTL_MINUTES", DEFAULT_SESSION_TTL_MINUTES)
                )
            except (TypeError, ValueError):
                session_ttl_minutes = DEFAULT_SESSION_TTL_MINUTES
        self._session_ttl = timedelta(minutes=max(session_ttl_minutes, 0.0))
        self._http_timeout = http_timeout

    # ------------------------------------------------------------------
    # Step 1 — credentials
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Verify credentials against the mStock TypeA login endpoint.

        On success a temp auth context (server response only) is stored for
        the subsequent ``verify_totp()`` call. The username and password are
        used for this request and immediately discarded — never stored.
        """
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            return self._login_failure("Username and password are required")
        if not self._api_key():
            return self._login_failure(
                "mStock API key is not configured on the server (MSTOCK_API_KEY)"
            )

        try:
            payload = self._post(
                _LOGIN_PATH,
                {"Username": username, "Password": password},
                rejected_default="Invalid username or password",
            )
        except _MStockAuthError as exc:
            return self._login_failure(str(exc))
        except requests.RequestException:
            logger.warning("mStock login request failed (network error)")
            return self._login_failure(
                "Could not reach mStock — check your connection and try again"
            )

        self._temp_auth_context = {
            "received_at": self._now().isoformat(),
            "login_payload": payload,
        }
        return {
            "success": True,
            "message": "Credentials verified — enter the code from your authenticator app",
            "requires_totp": True,
        }

    # ------------------------------------------------------------------
    # Step 2 — TOTP
    # ------------------------------------------------------------------

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        """Finalize the session using the temp auth context + 6-digit TOTP.

        On success the session token and expiry are stored in memory and the
        temp context is cleared. On a rejected code the temp context is kept
        so the user can retry without re-entering credentials.
        """
        code = (totp_code or "").strip()
        if not _TOTP_PATTERN.fullmatch(code):
            return {
                "success": False,
                "message": "Enter the 6-digit code from your authenticator app",
                "expires_at": "",
            }
        if self._temp_auth_context is None:
            return {
                "success": False,
                "message": "Log in with your credentials before entering the TOTP",
                "expires_at": "",
            }

        try:
            payload = self._post(
                _VERIFY_TOTP_PATH,
                {"api_key": self._api_key(), "totp": code},
                rejected_default="Invalid TOTP code — check your authenticator and try again",
            )
        except _MStockAuthError as exc:
            return {"success": False, "message": str(exc), "expires_at": ""}
        except requests.RequestException:
            logger.warning("mStock TOTP verification failed (network error)")
            return {
                "success": False,
                "message": "Could not reach mStock — check your connection and try again",
                "expires_at": "",
            }

        token = self._extract_token(payload)
        if not token:
            return {
                "success": False,
                "message": "mStock did not return a session token",
                "expires_at": "",
            }

        expires_at = self._compute_expiry(payload)
        self._session_token = token
        self._expires_at = expires_at
        self._temp_auth_context = None
        logger.info("mStock session established (expires at %s)", expires_at.isoformat())
        return {
            "success": True,
            "message": "mStock session established",
            "expires_at": expires_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # Status / teardown
    # ------------------------------------------------------------------

    def get_session_status(self) -> dict[str, Any]:
        """Compute session status from the in-memory expiry (Task 1.2).

        Reports ``expiring_soon`` inside the last
        :data:`EXPIRING_SOON_WINDOW_MINUTES` minutes and ``expired`` once the
        expiry has passed. Never includes the raw session token.
        """
        if not self._session_token or self._expires_at is None:
            return {
                "status": STATUS_UNAUTHENTICATED,
                "expires_at": None,
                "broker": self.broker_name,
            }

        remaining = self._expires_at - self._now()
        if remaining <= timedelta(0):
            status = STATUS_EXPIRED
        elif remaining <= timedelta(minutes=EXPIRING_SOON_WINDOW_MINUTES):
            status = STATUS_EXPIRING_SOON
        else:
            status = STATUS_AUTHENTICATED
        return {
            "status": status,
            "expires_at": self._expires_at.isoformat(),
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        """Raw session token — backend use only.

        Consumed by the ``BrokerSessionManager`` / Forward Engine (Task 1.3).
        Returns ``None`` unless the session is currently valid; the value
        must never be serialized into an API response or log line.
        """
        if self._session_token and self._expires_at and self._now() < self._expires_at:
            return self._session_token
        return None

    def logout(self) -> None:
        """Clear all in-memory session state (token, expiry, temp context)."""
        had_session = self._session_token is not None
        self._session_token = None
        self._expires_at = None
        self._temp_auth_context = None
        if had_session:
            logger.info("mStock session cleared (logout)")

    # ------------------------------------------------------------------
    # Order lifecycle (ticket P3.2) — all guarded by an active session
    # ------------------------------------------------------------------

    def place_order(self, order: BrokerOrder) -> BrokerOrderId:
        """Place ``order`` — ``POST /openapi/typea/orders/regular`` (form packet).

        Returns the broker's order id (what modify/cancel reference).
        """
        token = self._require_session()
        payload = self._request(
            "POST", _ORDER_PLACEMENT_PATH, token,
            form=self._map_order_to_broker_payload(order),
        )
        order_id = self._extract_order_id(payload)
        if order_id is None:
            raise MStockOrderError("mStock did not return an order id for the placed order")
        logger.info("mStock order placed: %s %s x%s", order.side, order.symbol, order.quantity)
        return BrokerOrderId(order_id)

    def modify_order(self, order: BrokerOrder) -> None:
        """Amend an open order — ``PUT /openapi/typea/orders/regular/{id}``."""
        token = self._require_session()
        path = f"{_ORDER_PATH_TEMPLATE}{self._require_broker_order_id(order)}"
        self._request("PUT", path, token, form=self._map_order_to_broker_payload(order))

    def cancel_order(self, order: BrokerOrder) -> None:
        """Cancel an open order — ``DELETE /openapi/typea/orders/regular/{id}``."""
        token = self._require_session()
        path = f"{_ORDER_PATH_TEMPLATE}{self._require_broker_order_id(order)}"
        self._request("DELETE", path, token)

    def get_order_book(self) -> list[BrokerOrder]:
        """Every order mStock currently knows — ``GET /openapi/typea/orders``."""
        return [self._order_from_row(row) for row in self._fetch_order_rows()]

    def poll_fill(self, broker_order_id: Any) -> dict[str, Any] | None:
        """Poll one order's fill — for :class:`BrokerFillProvider` (ticket #8).

        Queries the order book and returns a normalized **fill row** for
        ``broker_order_id`` (the mStock TypeA keys :meth:`Fill.from_broker`
        understands) once the order has actually executed, or ``None`` while
        it is still open/pending. A partial fill reports the FILLED quantity
        with the average price, never the requested quantity.

        The mStock TypeA API exposes one order-book endpoint rather than a
        per-order fill endpoint; row fields are read best-effort with the
        same leniency as :meth:`_order_from_row` (the archived reference
        documents the endpoints, not the exact row schema). An unknown
        ``broker_order_id`` is treated as not-yet-filled (``None``).
        """
        target = str(broker_order_id)
        for row in self._fetch_order_rows():
            if str(row.get("order_id")) != target:
                continue
            status = str(row.get("status") or "").upper()
            filled = row.get("filled_quantity")
            try:
                filled_qty = int(filled) if filled is not None else 0
            except (TypeError, ValueError):
                filled_qty = 0
            if filled_qty <= 0 and status not in ("COMPLETE", "FILLED", "EXECUTED"):
                # Open or otherwise not executed — no fill to report yet.
                return None
            # Normalize to a fill row: quantity = FILLED quantity, price =
            # average price (fall back to the placed price), so a partial
            # fill never over-states.
            price = row.get("average_price")
            if price in (None, "", 0):
                price = row.get("price")
            fill_row = {
                "tradingsymbol": row.get("tradingsymbol"),
                "transaction_type": row.get("transaction_type"),
                "quantity": filled_qty or row.get("quantity"),
                "filled_quantity": filled_qty or row.get("quantity"),
                "price": price,
                "brokerage": row.get("brokerage"),
                "order_id": row.get("order_id"),
            }
            logger.info("mStock order %s polled: %s", target, status)
            return fill_row
        return None

    def _fetch_order_rows(self) -> list[dict]:
        """Raw mStock TypeA order-book rows (``GET /openapi/typea/orders``)."""
        token = self._require_session()
        payload = self._request("GET", _ORDER_BOOK_PATH, token)
        if isinstance(payload, dict):
            payload = payload.get("data")
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise MStockOrderError("mStock order book response was not a list of orders")
        return [row for row in payload if isinstance(row, dict)]

    def calculate_order_margin(self, order: BrokerOrder) -> MarginInfo:
        """Pre-trade margin check — ``POST /openapi/typea/margins/orders`` (JSON)."""
        token = self._require_session()
        payload = self._request(
            "POST", _ORDER_MARGIN_PATH, token,
            json_body=self._map_order_to_broker_payload(order),
        )
        data = payload if isinstance(payload, dict) else {}
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}

        def _num(*keys: str) -> float | None:
            for key in keys:
                value = data.get(key)
                if value is None:
                    value = inner.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
            return None

        initial = _num("initial_margin", "buy_margin", "margin")
        maintenance = _num("maintenance_margin", "maintenance")
        available = _num("available_margin", "available_funds")
        if initial is None:
            raise MStockOrderError("mStock margin response carried no margin amount")
        return MarginInfo(
            initial_margin=initial,
            maintenance_margin=maintenance if maintenance is not None else initial,
            available_margin=available,
            is_funded=(available is None) or (available >= initial),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _login_failure(self, message: str) -> dict[str, Any]:
        """Record a failed login: no temp context may survive a rejection."""
        self._temp_auth_context = None
        return {"success": False, "message": message, "requires_totp": False}

    def _post(self, path: str, form: dict[str, str], rejected_default: str) -> dict[str, Any]:
        """POST a urlencoded form to a TypeA endpoint and return the payload.

        Raises :class:`_MStockAuthError` with a user-facing message when the
        request is rejected (HTTP 401/403, error payload, non-success
        status) — generic messages only, no stack traces or internals.
        """
        url = f"{self._base_url()}{path}"
        resp = requests.post(url, data=form, headers=_TYPEA_HEADERS, timeout=self._http_timeout)
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = None

        if not resp.ok:
            default = (
                rejected_default
                if resp.status_code in (401, 403)
                else f"mStock request failed (HTTP {resp.status_code})"
            )
            raise _MStockAuthError(_rejection_reason(payload) or default)
        reason = _rejection_reason(payload)
        if reason:
            raise _MStockAuthError(reason)
        return payload if isinstance(payload, dict) else {}

    # -- order internals (ticket P3.2) -------------------------------------

    def _require_session(self) -> str:
        """Return the live session token or fail cleanly — never half-send."""
        token = self.get_session_token()
        if not token:
            raise MStockOrderError(
                "no active mStock session — log in (credentials + TOTP) before placing orders"
            )
        return token

    @staticmethod
    def _require_broker_order_id(order: BrokerOrder) -> str:
        if order.broker_order_id is None:
            raise MStockOrderError("order has no broker_order_id — place it before modify/cancel")
        return str(order.broker_order_id)

    def _session_token_headers(self, token: str) -> dict[str, str]:
        """TypeA headers + the authenticated session (api_key:access_token)."""
        return {
            **_TYPEA_HEADERS,
            "Authorization": f"token {self._api_key()}:{token}",
        }

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        form: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """One authenticated TypeA order call; return the parsed payload.

        Raises :class:`MStockOrderError` on non-2xx or an error payload.
        """
        headers = self._session_token_headers(token)
        kwargs: dict[str, Any] = {"headers": headers, "timeout": self._http_timeout}
        url = f"{self._base_url()}{path}"
        if method == "GET":
            resp = requests.get(url, **kwargs)
        elif method == "PUT":
            resp = requests.put(url, data=form, **kwargs)
        elif method == "DELETE":
            resp = requests.delete(url, **kwargs)
        else:  # POST — form packet for orders, JSON packet for margin
            if json_body is not None:
                headers["Content-Type"] = "application/json"
                resp = requests.post(url, json=json_body, **kwargs)
            else:
                resp = requests.post(url, data=form, **kwargs)
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = None

        if not resp.ok:
            raise MStockOrderError(
                _rejection_reason(payload)
                or f"mStock {method} failed (HTTP {resp.status_code})"
            )
        reason = _rejection_reason(payload)
        if reason:
            raise MStockOrderError(reason)
        return payload

    @staticmethod
    def _map_order_to_broker_payload(order: BrokerOrder) -> dict[str, Any]:
        """Generic :class:`BrokerOrder` → mStock TypeA order packet (form fields).

        Field names per docs/archive/mstock-typea-api-reference.md; defaults
        match the V1 intraday delivery flow.
        """
        order_type = (order.order_type or "MARKET").upper()
        return {
            "tradingsymbol": order.symbol.strip().upper(),
            "exchange": (order.exchange or "NSE").strip().upper(),
            "transaction_type": (order.side or "BUY").strip().upper(),
            "order_type": order_type,
            "quantity": int(order.quantity),
            "product": (order.product or "INTRADAY").strip().upper(),
            "validity": "DAY",
            "price": float(order.limit_price) if order_type == "LIMIT" and order.limit_price else 0,
            "trigger_price": 0,
            "disclosed_quantity": 0,
            "tag": str(order.tag.get("tag", "")) if order.tag else "",
        }

    @staticmethod
    def _extract_order_id(payload: Any) -> str | None:
        """Pull the new order id out of either known response shape."""
        if not isinstance(payload, dict):
            return None
        order_id = payload.get("order_id") or (payload.get("data") or {}).get("order_id")
        if isinstance(order_id, str) and order_id.strip():
            return order_id.strip()
        if isinstance(order_id, int):
            return str(order_id)
        return None

    @staticmethod
    def _order_from_row(row: dict[str, Any]) -> BrokerOrder:
        """Lenient row → :class:`BrokerOrder` mapping.

        The archived reference documents the endpoints but not the exact
        order-book row schema; fields are read best-effort by their
        documented names (order_id/tradingsymbol/transaction_type/...).
        """
        def _str(key: str) -> str | None:
            value = row.get(key)
            return str(value) if value is not None else None

        def _num(key: str) -> float:
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return float(value)

        order_id = _str("order_id")
        limit_price = _num("price")
        average_price = _num("average_price")
        return BrokerOrder(
            broker_order_id=BrokerOrderId(order_id) if order_id else None,
            symbol=_str("tradingsymbol") or "",
            side=(_str("transaction_type") or "BUY").upper(),
            quantity=int(_num("quantity")),
            order_type=(_str("order_type") or "MARKET").upper(),
            limit_price=limit_price if limit_price else None,
            status=(_str("status") or "OPEN").upper(),
            filled_quantity=int(_num("filled_quantity")),
            average_fill_price=average_price if average_price else None,
            exchange=_str("exchange"),
            product=_str("product"),
        )

    @staticmethod
    def _extract_token(payload: dict[str, Any]) -> str | None:
        """Pull the access token out of either known response shape."""
        token = payload.get("access_token") or (payload.get("data") or {}).get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    def _compute_expiry(self, payload: dict[str, Any]) -> datetime:
        """Prefer a server-provided lifetime; fall back to the configured TTL."""
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            return self._now() + timedelta(seconds=int(expires_in))
        return self._now() + self._session_ttl

    @staticmethod
    def _base_url() -> str:
        return os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")

    @staticmethod
    def _api_key() -> str:
        return os.getenv("MSTOCK_API_KEY", "").strip()

    @staticmethod
    def _now() -> datetime:
        return datetime.now()
