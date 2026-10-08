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
import time
from datetime import datetime, timedelta
from decimal import Decimal
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
from backtest.instruments.option import OptionContract
from backtest.instruments.base import ExerciseType, SettlementType

__all__ = ["MStockBroker", "MStockOrderError", "OrderOutcomeUnknown"]

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

# mStock option chain endpoints (options PRD P1.3/P1.4).
_OPTION_CHAIN_MASTER_PATH = "/openapi/typea/getoptionchainmaster/{exchange_id}"
_OPTION_CHAIN_DATA_PATH = "/openapi/typea/GetOptionChain/{exchange_id}/{expiry}/{token}"
_INSTRUMENT_MASTER_PATH = "/openapi/typea/instruments/scriptmaster"
_OPTION_QUOTE_PATH = "/openapi/typea/instruments/quote/ltp"

# NFO exchange ID for mStock (derivatives segment)
_NFO_EXCHANGE_ID = "5"

_TYPEA_HEADERS = {
    "X-Mirae-Version": "1",
    "Content-Type": "application/x-www-form-urlencoded",
}

_TOTP_PATTERN = re.compile(r"\d{6}")

# Transient-gateway retry for the two auth POSTs (login + verify-totp).
# mStock's gateway intermittently answers 502/503/504 under load — the very
# same flakiness already worked around for the historical fetcher
# (`_get_historical_with_retry`, ae437e3). The auth path never got that
# protection, so a single 502 surfaced as "mStock request failed (HTTP 502)"
# and forced the user to re-submit the TOTP 2-3 times until a request landed
# on a healthy gateway (observed live 2026-10-03 11:34). Only the 5xx statuses
# are retried: a wrong credential or wrong code is a 401/403 and must fail
# fast, never retried — resubmitting a rejected TOTP could waste its 30s
# window or trip the broker's lockout.
_AUTH_RETRY_STATUSES = (502, 503, 504)
AUTH_RETRY_ATTEMPTS = 4
AUTH_RETRY_BASE_SLEEP = 0.8  # linear backoff: 0.8, 1.6, 2.4 s between attempts

# Order placement outcomes. A 4xx or an error payload on a 200 is mStock's
# verdict: the ticket was refused, so nothing exists at the exchange and the
# caller may act on that. These statuses are not a verdict — the request may
# have been accepted and only the answer was lost (gateway 5xx, throttled
# connection, timeout). Retrying one could double-place an order, so they
# surface as :class:`OrderOutcomeUnknown` and the caller must reconcile.
_UNCERTAIN_ORDER_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

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

    ``status_code`` is the HTTP status when mStock answered one, else ``None``.
    Callers use it to tell a definite rejection (4xx / error payload — nothing
    at the exchange) from an outcome that is merely unknown.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OrderOutcomeUnknown(MStockOrderError):
    """The call to mStock failed without an answer — the order may exist.

    A gateway 5xx, a rate limit, a connection/timeout failure, or a 2xx that
    carried no order id all leave the same question: is that ticket at the
    exchange or not? Nothing may be inferred from the silence, so this is the
    one error class the execution seam must not retry — it must reconcile
    against the order book first (Options V1 PRD §6, "never retry an uncertain
    order blindly").
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

    def restore_session(self, token: str, expires_at: Any) -> None:
        """Seed the in-memory session from a remembered token (2026-09-24).

        Used by the remember-session-today feature: the session manager
        hands back a token persisted earlier the same day. No temp auth
        context is set — TOTP re-auth would start a fresh login flow.

        The store keeps expiries as aware datetimes (``_parse_expiry``
        re-attaches the local zone), while this broker's session clock is
        naive-local everywhere (see ``_now``). Coerce the incoming value to
        naive-local so ``get_session_status`` does not raise
        ``TypeError: can't subtract offset-naive and offset-aware datetimes``
        — which used to fail-close /api/broker/status into "Unknown Broker"
        and blank the whole broker strip after a restart (2026-10-01).
        """
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at is not None and expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone().replace(tzinfo=None)
        self._session_token = token
        self._expires_at = expires_at
        self._temp_auth_context = None
        logger.info("mStock session restored from remembered token (expires %s)", expires_at)

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

        Raises :class:`MStockOrderError` when mStock clearly refused the order,
        and :class:`OrderOutcomeUnknown` when the call ended without a usable
        answer — a lost connection, a gateway 5xx / 429, or a 2xx that carried
        no order id. In that case the ticket may exist at the exchange, so the
        caller reconciles against the order book instead of placing again.
        """
        token = self._require_session()
        packet = self._map_order_to_broker_payload(order)
        try:
            payload = self._request("POST", _ORDER_PLACEMENT_PATH, token, form=packet)
        except requests.RequestException as exc:
            # The request may have reached mStock before the socket died. The
            # exception text carries the URL, so the message stays generic.
            raise OrderOutcomeUnknown(
                f"mStock order call got no answer ({exc.__class__.__name__}) "
                "— the order may still exist"
            ) from exc
        except MStockOrderError as exc:
            code = exc.status_code
            if code is not None and (code in _UNCERTAIN_ORDER_STATUSES or code >= 500):
                raise OrderOutcomeUnknown(str(exc), status_code=code) from exc
            raise
        order_id = self._extract_order_id(payload)
        if order_id is None:
            # A 2xx we could not read an id from is still an accepted call as
            # far as the exchange is concerned — do not treat it as a reject.
            raise OrderOutcomeUnknown(
                "mStock did not return an order id for the placed order — "
                "the order may still exist"
            )
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
            "POST",
            _ORDER_MARGIN_PATH,
            token,
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

    def _post(
        self,
        path: str,
        form: dict[str, str],
        rejected_default: str,
        attempts: int = AUTH_RETRY_ATTEMPTS,
        base_sleep: float = AUTH_RETRY_BASE_SLEEP,
    ) -> dict[str, Any]:
        """POST a urlencoded form to a TypeA auth endpoint, returning the payload.

        Raises :class:`_MStockAuthError` with a user-facing message when the
        request is rejected (HTTP 401/403, error payload, non-success
        status) — generic messages only, no stack traces or internals.

        A transient gateway 5xx (502/503/504) is retried up to ``attempts``
        times with linear backoff, absorbing mStock's intermittent 502s so a
        single TOTP submit lands. Everything else is mStock's actual verdict:
        a 4xx rejection (wrong credentials / wrong TOTP) or an error payload on
        a 200 raises immediately on the first attempt — a rejected code is
        never re-sent. A connection-level error (``requests.RequestException``)
        propagates untouched for the caller's generic "could not reach" path.
        """
        url = f"{self._base_url()}{path}"
        last_status: int | None = None
        for attempt in range(1, attempts + 1):
            resp = requests.post(
                url, data=form, headers=_TYPEA_HEADERS, timeout=self._http_timeout
            )
            if resp.status_code not in _AUTH_RETRY_STATUSES:
                return self._parse_auth_response(resp, rejected_default)
            last_status = resp.status_code
            logger.warning(
                "mStock POST %s got HTTP %s (attempt %d/%d)", path, last_status, attempt, attempts
            )
            if attempt < attempts:
                time.sleep(base_sleep * attempt)

        # Still 5xx after exhausting attempts — surface the gateway error.
        raise _MStockAuthError(f"mStock request failed (HTTP {last_status})")

    def _parse_auth_response(self, resp, rejected_default: str) -> dict[str, Any]:
        """Turn a non-retryable auth response into a payload or a rejection."""
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
                _rejection_reason(payload) or f"mStock {method} failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
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

    # ------------------------------------------------------------------
    # Option chain (options PRD P1.3, P1.4)
    # ------------------------------------------------------------------

    def get_option_chain(
        self, underlying: str, exchange_id: str | None = None
    ) -> list[OptionContract]:
        """Fetch the option chain for an underlying from mStock.

        Calls the instrument master endpoint and filters for NFO segment
        options matching the given underlying. Returns a list of
        ``OptionContract`` objects ready for the registry.

        Parameters
        ----------
        underlying:
            Index name — ``"NIFTY"`` or ``"BANKNIFTY"``.
        exchange_id:
            mStock exchange ID (default ``"5"`` for NFO).

        Returns
        -------
        list[OptionContract]
            All valid option contracts for the underlying.
        """
        token = self._require_session()
        # NOTE: ``exchange_id`` is accepted for API symmetry with the live
        # chain endpoint; the instrument master filter below is NFO-only.

        try:
            # Fetch the full instrument master (CSV format)
            headers = self._session_token_headers(token)
            url = f"{self._base_url()}{_INSTRUMENT_MASTER_PATH}"
            resp = requests.get(url, headers=headers, timeout=self._http_timeout)
            resp.raise_for_status()

            # Parse CSV instrument master
            lines = resp.text.strip().split("\n")
            if len(lines) < 2:
                logger.warning("mStock instrument master returned no data")
                return []

            # Header row
            header = lines[0].split(",")
            contracts: list[OptionContract] = []

            for line in lines[1:]:
                fields = line.split(",")
                if len(fields) < len(header):
                    continue
                row = dict(zip(header, fields))

                # Filter: index-option segment, matching underlying, options
                # only. REAL scriptmaster columns (verified live 2026-09-18):
                # segment="OPTIDX", instrument_type="CE"/"PE",
                # tradingsymbol="NIFTY26NOV18950CE". (The old guesses
                # instrument_segment/trading_symbol/NFO never matched a
                # single row — every option was silently dropped.)
                segment = str(row.get("segment", row.get("instrument_segment", "")))
                sym = str(row.get("tradingsymbol", row.get("trading_symbol", "")))
                inst_type = str(row.get("instrument_type", ""))

                # Match underlying (e.g. NIFTY in NIFTY26NOV18950CE)
                if not sym.startswith(underlying):
                    continue
                if "OPT" not in segment.upper() and inst_type.upper() not in ("CE", "PE"):
                    continue

                contract = self._parse_option_contract(row)
                if contract and contract.is_valid():
                    contracts.append(contract)

            logger.info(
                "mStock option chain for %s: %d contracts", underlying, len(contracts)
            )
            return contracts

        except requests.RequestException:
            logger.warning("mStock instrument master request failed")
            return []
        except Exception:
            logger.warning("Failed to parse mStock instrument master", exc_info=True)
            return []

    def get_option_chain_data(
        self, underlying: str, expiry: str, token: str
    ) -> dict[str, Any]:
        """Fetch live option chain data (bid/ask/OI) for a specific expiry.

        Calls ``GET /openapi/typea/GetOptionChain/{exchange_id}/{expiry}/{token}``.

        Parameters
        ----------
        underlying:
            Index name (for logging).
        expiry:
            Expiry date string (e.g. ``"24DEC"``).
        token:
            Instrument token of the underlying.

        Returns
        -------
        dict
            Raw API response with option chain data.
        """
        token_str = self._require_session()
        path = _OPTION_CHAIN_DATA_PATH.format(
            exchange_id=_NFO_EXCHANGE_ID, expiry=expiry, token=token
        )
        try:
            return self._request("GET", path, token_str)
        except MStockOrderError:
            logger.warning("Failed to fetch option chain data for %s", underlying)
            return {}

    def get_option_quote(self, instrument_token: str) -> dict[str, Any]:
        """Fetch L1 quote for an option contract.

        Uses the LTP endpoint with the instrument token.

        Returns
        -------
        dict
            ``{"ltp": float, "bid": float, "ask": float, ...}`` or empty dict.
        """
        token = self._require_session()
        headers = self._session_token_headers(token)
        url = f"{self._base_url()}{_OPTION_QUOTE_PATH}"
        # REAL API (verified live 2026-09-18): the quote/ltp endpoint keys
        # on the TRADING SYMBOL ("NFO:NIFTY26SEP23300CE"), not the numeric
        # instrument_token ("NFO:51201" → "Invalid symbol"). Accept a
        # bare OptionContract too so callers can pass the contract row
        # they got from get_option_chain().
        symbol = getattr(instrument_token, "trading_symbol", None) or str(
            instrument_token
        )
        # One retry on transient failures (live-session lesson 2026-09-21:
        # rate-limit blips coinciding with a fill turned into ltp=0 phantoms).
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = requests.get(
                    url, headers=headers,
                    params=[("i", f"NFO:{symbol}")],
                    timeout=self._http_timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict) and "data" in payload:
                    data = payload["data"] if isinstance(payload["data"], dict) else {}
                    inner = data.get(f"NFO:{symbol}", data)
                    if isinstance(inner, dict) and "message" not in inner:
                        return inner
                    return {}
                return payload if isinstance(payload, dict) else {}
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1.0)
        logger.warning(
            "Option quote request failed after retry for %s: %s",
            instrument_token,
            last_exc,
        )
        return {}

    @staticmethod
    def _parse_option_contract(row: dict[str, Any]) -> OptionContract | None:
        """Parse one instrument-master row into an OptionContract.

        Returns ``None`` if the row cannot be parsed.
        """
        try:
            symbol = str(row.get("tradingsymbol", row.get("trading_symbol", "")))
            token = row.get("instrument_token", row.get("security_token", ""))

            from datetime import date

            # Pattern: UNDERLYING + YY + MON + STRIKE + CE/PE
            match = re.match(
                r"^(NIFTY|BANKNIFTY)(\d{2})([A-Z]{3})(\d+)(CE|PE)$", symbol
            )
            if not match:
                return None

            underlying = match.group(1)
            year_suffix = match.group(2)
            month_str = match.group(3)
            strike = Decimal(match.group(4))
            option_type = match.group(5)

            # Parse month
            month_map = {
                "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
                "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
                "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
            }
            month = month_map.get(month_str)
            if month is None:
                return None

            year = 2000 + int(year_suffix)
            # Expiry: the master carries the REAL expiry date ("2026-11-23")
            # — use it, fall back to the last-Thursday approximation only
            # when missing/unparseable.
            expiry = None
            raw_expiry = str(row.get("expiry", "") or "").strip()
            if raw_expiry:
                try:
                    expiry = date.fromisoformat(raw_expiry[:10])
                except ValueError:
                    pass
            if expiry is None:
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                expiry = date(year, month, last_day)

            def _num(field: str, default: float) -> float:
                value = row.get(field)
                try:
                    value = float(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return default
                return value if value == value else default  # NaN check

            # Lot size comes from the master or nothing. Guessing one would size
            # the position (and every stop in rupees) off a number the exchange
            # never published for this contract.
            lot_size = int(_num("lot_size", _num("contract_size", 0.0)))
            if lot_size <= 0:
                logger.warning(
                    "option contract %s has no lot size on the instrument master — skipped",
                    symbol,
                )
                return None
            tick = Decimal(str(_num("tick_size", 0.05)))

            return OptionContract(
                instrument_token=str(token),
                trading_symbol=symbol,
                underlying=underlying,
                exchange="NSE",
                segment="NFO",
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                lot_size=lot_size,
                tick_size=tick,
                contract_type=ExerciseType.EUROPEAN,
                settlement_type=SettlementType.CASH,
            )
        except Exception:
            return None

    @staticmethod
    def _now() -> datetime:
        return datetime.now()
