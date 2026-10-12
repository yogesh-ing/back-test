"""Dhan broker — generic auth contract (BrokerAuthBase) implementation.

Auth flow (from the official DhanHQ Python SDK, reference-code/DhanHQ-py-main):

    POST https://auth.dhan.co/app/generateAccessToken
         ?dhanClientId=<client-id>&pin=<pin>&totp=<totp>
    → ``{"accessToken": "...", "expiryTime": "...", ...}`` (token + ISO expiry)

Unlike mStock's two-step flow, Dhan is a SINGLE call: client-id + PIN + TOTP
together mint the access token. To keep the UI and the
:class:`~backtest.brokers.session_manager.BrokerSessionManager` contract
identical across brokers, we split it across the two standard steps:

* ``login(client_id, pin)`` — validates shape only (no network call); sets a
  temp auth context so ``verify_totp`` can proceed.
* ``verify_totp(totp)`` — performs the actual ``generateAccessToken`` call.

Security invariants (same as MStockBroker):

* the PIN is used for the token call and immediately discarded — never
  stored, logged, or echoed in any response;
* the raw access token never appears in any return value of the contract
  methods (only ``get_session_token()``, consumed by the session manager);
* all session state is in-memory only.

The Dhan *data* API key (``DHAN_API_KEY``) is configured server-side in
``.env`` as a placeholder — the client-id and PIN arrive at runtime from the
login form, never from the environment.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
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

__all__ = ["DhanBroker", "DhanAuthError", "DhanOrderError"]

logger = logging.getLogger("backtest.brokers.dhan")

# Dhan auth + data API endpoints (DhanHQ v2).
_AUTH_BASE_URL = "https://auth.dhan.co"
_GENERATE_TOKEN_PATH = "/app/generateAccessToken"

# Order lifecycle endpoints (DhanHQ v2 REST — Phase C, closes F-12).
_API_BASE_URL = "https://api.dhan.co/v2"
_ORDERS_PATH = "/orders"
_MARGIN_PATH = "/margincalculator"

# Instrument master (symbol → securityId). The compact scrip master is a
# public CSV; downloaded lazily ONLY when an order references a symbol not
# covered by the DHAN_SECURITY_IDS env override.
_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Dhan order statuses that mean "no more fills will come".
_DHAN_TERMINAL_STATUSES = {"TRADED", "REJECTED", "CANCELLED", "EXPIRED"}

# Fallback session lifetime when the API response carries no expiry hint.
# Dhan access tokens are typically valid for the trading day (24h nominal);
# override via DHAN_SESSION_TTL_MINUTES in .env.
DEFAULT_SESSION_TTL_MINUTES = 390.0

# PRD session state machine: "expiring_soon" inside the last 30 minutes.
EXPIRING_SOON_WINDOW_MINUTES = 30.0

_TOTP_PATTERN = re.compile(r"\d{6}")
_PIN_PATTERN = re.compile(r"\d{4,6}")
_MAX_MESSAGE_LEN = 200


class DhanAuthError(Exception):
    """Dhan rejected the request (bad client-id, bad PIN, bad TOTP)."""


class DhanOrderError(RuntimeError):
    """Dhan rejected an order call (place/modify/cancel/book/margin)."""


# Process-wide scrip-master cache: symbol (upper) → securityId. Downloaded at
# most once per process; an order for an unmapped symbol fails closed.
_SCRIP_MASTER_CACHE: dict[str, str] | None = None


def _load_scrip_master(timeout: float = 30.0) -> dict[str, str]:
    """Download + parse the Dhan compact scrip master (NSE equity rows).

    Returns ``{trading_symbol: security_id}``. Cached per process — the CSV
    is a few MB and changes at most daily. A download failure returns an
    empty map (the caller then fails closed for unmapped symbols with a
    message pointing at the ``DHAN_SECURITY_IDS`` override).
    """
    global _SCRIP_MASTER_CACHE
    if _SCRIP_MASTER_CACHE is not None:
        return _SCRIP_MASTER_CACHE
    url = os.getenv("DHAN_SCRIP_MASTER_URL", _SCRIP_MASTER_URL)
    mapping: dict[str, str] = {}
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            exch = (row.get("SEM_EXM_EXCH_ID") or row.get("EXCH_ID") or "").strip().upper()
            segment = (row.get("SEM_SEGMENT") or row.get("SEGMENT") or "").strip().upper()
            instrument = (
                (row.get("SEM_INSTRUMENT_NAME") or row.get("INSTRUMENT") or "").strip().upper()
            )
            if exch != "NSE" or segment not in ("E", "EQ", "EQUITY"):
                continue
            if instrument and instrument not in ("EQUITY", "ES"):
                continue
            sym = (row.get("SEM_TRADING_SYMBOL") or row.get("SYMBOL_NAME") or "").strip().upper()
            sec_id = (row.get("SEM_SMST_SECURITY_ID") or row.get("SECURITY_ID") or "").strip()
            if sym and sec_id and sym not in mapping:
                mapping[sym] = sec_id
        logger.info("Dhan scrip master loaded: %s NSE equity symbols", len(mapping))
    except requests.RequestException as exc:
        logger.warning("Dhan scrip master download failed: %s", exc)
    _SCRIP_MASTER_CACHE = mapping
    return mapping


def _rejection_reason(payload: Any) -> str | None:
    """Extract a user-facing rejection reason from a Dhan payload, if any."""
    if not isinstance(payload, dict):
        return None
    for key in ("errorMessage", "error_message", "error", "message", "remarks"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_MESSAGE_LEN]
    return None


class DhanBroker(BrokerAuthBase, BrokerOrderBase):
    """Dhan implementation of the generic two-step auth contract.

    State held in-memory only; lost on restart by design (same as mStock).
    The temp auth context between ``login()`` and ``verify_totp()`` holds
    server-returned data only — never the PIN.
    """

    broker_name = "dhan"
    broker_display_name = "Dhan"

    def __init__(
        self,
        session_ttl_minutes: float | None = None,
        http_timeout: float = 10.0,
    ) -> None:
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._temp_auth_context: dict[str, Any] | None = None
        self._temp_username: str | None = None
        self._temp_pin: str | None = None
        if session_ttl_minutes is None:
            try:
                session_ttl_minutes = float(
                    os.getenv("DHAN_SESSION_TTL_MINUTES", DEFAULT_SESSION_TTL_MINUTES)
                )
            except (TypeError, ValueError):
                session_ttl_minutes = DEFAULT_SESSION_TTL_MINUTES
        self._session_ttl = timedelta(minutes=max(session_ttl_minutes, 0.0))
        self._http_timeout = http_timeout
        # Transient single-request retry guard (same live-session lesson as
        # mStock: one retry on transient network failures).
        self._retries = 1
        # Order contract (Phase C): the client id every /v2 order call must
        # carry; kept after verify_totp, env-fallback for restored sessions.
        self._client_id: str | None = None
        # symbol (upper) → Dhan securityId, resolved lazily (env override
        # first, scrip master download as fallback) and cached per instance.
        self._order_security_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Step 1 — credentials (client-id + PIN; local validation only)
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Validate client-id + PIN shape; defer the token call to TOTP.

        Dhan's ``generateAccessToken`` needs client-id + PIN + TOTP in one
        request, so step 1 only validates and parks the credentials in a
        temp auth context (discarded on failure/logout/TOTP success).
        """
        client_id = (username or "").strip()
        pin = password or ""
        if not client_id or not pin:
            return self._login_failure("Dhan client ID and PIN are required")
        if not _PIN_PATTERN.fullmatch(pin):
            return self._login_failure("PIN must be 4–6 digits")

        # Validate server-side config presence early so the user gets a
        # clear message instead of a mystery TOTP failure.
        if not self._api_key_available():
            return self._login_failure(
                "Dhan data API access is not configured on the server (DHAN_API_KEY)"
            )

        # Parked ONLY for the duration of the TOTP step; cleared on success,
        # failure, and logout. Never logged.
        self._temp_username = client_id
        self._temp_pin = pin
        self._temp_auth_context = {"received_at": self._now().isoformat()}
        return {
            "success": True,
            "message": "Credentials accepted — enter the code from your authenticator app",
            "requires_totp": True,
        }

    # ------------------------------------------------------------------
    # Step 2 — TOTP (the actual token call)
    # ------------------------------------------------------------------

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        """Call Dhan's ``generateAccessToken`` with the parked client-id+PIN+TOTP.

        On success the access token and expiry are stored in memory and the
        temp context (including the PIN) is cleared. On a rejected code the
        temp context is kept so the user can retry without re-entering the
        PIN.
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

        # NOTE: the parked credentials are read here and never logged.
        client_id = (self._temp_username or "").strip()
        pin = self._temp_pin or ""

        try:
            payload = self._generate_token(client_id, pin, code)
        except DhanAuthError as exc:
            return {"success": False, "message": str(exc), "expires_at": ""}
        except requests.RequestException:
            logger.warning("Dhan token request failed (network error)")
            return {
                "success": False,
                "message": "Could not reach Dhan — check your connection and try again",
                "expires_at": "",
            }

        token = self._extract_token(payload)
        if not token:
            return {
                "success": False,
                "message": "Dhan did not return an access token",
                "expires_at": "",
            }

        expires_at = self._compute_expiry(payload)
        self._access_token = token
        self._expires_at = expires_at
        # Phase C: order calls need dhanClientId — keep it past the handshake
        # (it is an account identifier, not a secret credential like the PIN).
        self._client_id = client_id
        self._temp_auth_context = None
        self._temp_username = None
        self._temp_pin = None
        logger.info("Dhan session established (expires at %s)", expires_at.isoformat())
        return {
            "success": True,
            "message": "Dhan session established",
            "expires_at": expires_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # Status / teardown
    # ------------------------------------------------------------------

    def get_session_status(self) -> dict[str, Any]:
        """Compute session status from the in-memory expiry (same machine as mStock)."""
        if not self._access_token or self._expires_at is None:
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
        """Raw access token — backend use only (session manager / engine)."""
        if self._access_token and self._expires_at and self._now() < self._expires_at:
            return self._access_token
        return None

    def restore_session(self, token: str, expires_at: Any) -> None:
        """Seed the in-memory session from a remembered token (remember-session).

        Coerce the expiry to naive-local (the convention of ``_now`` and
        ``_compute_expiry``): the remember-session store returns aware
        datetimes, and mixing them with the naive clock raises TypeError in
        ``get_session_status`` — which blanks the header broker strip.
        """
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at is not None and expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone().replace(tzinfo=None)
        self._access_token = token
        self._expires_at = expires_at
        self._temp_auth_context = None
        logger.info("Dhan session restored from remembered token (expires %s)", expires_at)

    def logout(self) -> None:
        """Clear all in-memory session state (token, expiry, temp context)."""
        had_session = self._access_token is not None
        self._access_token = None
        self._expires_at = None
        self._temp_auth_context = None
        self._temp_username = None
        self._temp_pin = None
        if had_session:
            logger.info("Dhan session cleared (logout)")

    # ------------------------------------------------------------------
    # Order lifecycle contract (Phase C — closes F-12)
    #
    # DhanHQ v2 REST: JSON bodies, `access-token` header, and every call
    # carries `dhanClientId`. Fail-closed: no authenticated session → raise
    # before any bytes leave the process (never half-send).
    # ------------------------------------------------------------------

    def place_order(self, order: BrokerOrder) -> BrokerOrderId:
        """Place ``order`` — ``POST /v2/orders`` (JSON packet).

        Returns the broker's order id (what modify/cancel reference).
        ``order.client_order_id`` is sent as ``correlationId`` (Dhan echoes
        it back on the order book — the idempotency/audit key).
        """
        payload = self._api_request(
            "POST", _ORDERS_PATH, json_body=self._map_order_to_broker_payload(order)
        )
        order_id = self._extract_order_id(payload)
        if order_id is None:
            raise DhanOrderError("Dhan did not return an order id for the placed order")
        logger.info("Dhan order placed: %s %s x%s", order.side, order.symbol, order.quantity)
        return BrokerOrderId(order_id)

    def modify_order(self, order: BrokerOrder) -> None:
        """Amend an open order — ``PUT /v2/orders/{order-id}``."""
        broker_order_id = self._require_broker_order_id(order)
        body = {
            "dhanClientId": self._require_client_id(),
            "orderId": broker_order_id,
            "orderType": self._map_order_type(order.order_type),
            "quantity": int(order.quantity),
            "price": float(order.limit_price) if order.limit_price is not None else 0.0,
            "disclosedQuantity": 0,
            "triggerPrice": 0.0,
            "validity": "DAY",
        }
        self._api_request("PUT", f"{_ORDERS_PATH}/{broker_order_id}", json_body=body)

    def cancel_order(self, order: BrokerOrder) -> None:
        """Cancel an open order — ``DELETE /v2/orders/{order-id}``."""
        broker_order_id = self._require_broker_order_id(order)
        self._api_request("DELETE", f"{_ORDERS_PATH}/{broker_order_id}")

    def get_order_book(self) -> list[BrokerOrder]:
        """Every order Dhan currently knows — ``GET /v2/orders``."""
        return [self._order_from_row(row) for row in self._fetch_order_rows()]

    def poll_fill(self, broker_order_id: Any) -> dict[str, Any] | None:
        """Poll one order's fill — for :class:`BrokerFillProvider` (ticket #8).

        Same contract as :meth:`MStockBroker.poll_fill`: a normalized fill
        row (keys :meth:`Fill.from_broker` understands) once the order has
        actually traded, ``None`` while it is still open/pending. A partial
        fill reports the FILLED quantity at the average traded price, never
        the requested quantity. Unknown id → ``None`` (not-yet-filled).
        """
        target = str(broker_order_id)
        for row in self._fetch_order_rows():
            if str(row.get("orderId") or row.get("order_id")) != target:
                continue
            status = str(row.get("orderStatus") or row.get("status") or "").upper()
            filled = row.get("filledQty", row.get("filled_quantity"))
            try:
                filled_qty = int(filled) if filled is not None else 0
            except (TypeError, ValueError):
                filled_qty = 0
            if filled_qty <= 0 and status != "TRADED":
                return None  # open or otherwise not executed — no fill yet
            price = row.get("averageTradedPrice")
            if price in (None, "", 0):
                price = row.get("price")
            fill_row = {
                "tradingsymbol": row.get("tradingSymbol") or row.get("tradingsymbol"),
                "transaction_type": row.get("transactionType") or row.get("transaction_type"),
                "quantity": filled_qty or row.get("quantity"),
                "filled_quantity": filled_qty or row.get("quantity"),
                "price": price,
                "order_id": row.get("orderId") or row.get("order_id"),
            }
            logger.info("Dhan order %s polled: %s", target, status)
            return fill_row
        return None

    def calculate_order_margin(self, order: BrokerOrder) -> MarginInfo:
        """Pre-trade margin check — ``POST /v2/margincalculator`` (JSON)."""
        body = {
            "dhanClientId": self._require_client_id(),
            "exchangeSegment": self._map_exchange_segment(order),
            "transactionType": (order.side or "BUY").upper(),
            "quantity": int(order.quantity),
            "productType": self._map_product(order.product),
            "securityId": self._security_id_for_order(order.symbol),
            "price": float(order.limit_price) if order.limit_price is not None else 0.0,
        }
        payload = self._api_request("POST", _MARGIN_PATH, json_body=body)
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

        initial = _num("totalMargin", "total_margin", "margin")
        available = _num("availableBalance", "available_balance")
        insufficient = _num("insufficientBalance", "insufficient_balance")
        if initial is None:
            raise DhanOrderError("Dhan margin response carried no margin amount")
        if insufficient is not None:
            funded = insufficient <= 0
        else:
            funded = (available is None) or (available >= initial)
        return MarginInfo(
            initial_margin=initial,
            maintenance_margin=initial,
            available_margin=available,
            is_funded=funded,
        )

    # ------------------------------------------------------------------
    # Order internals
    # ------------------------------------------------------------------

    def _require_order_session(self) -> str:
        """Valid access token or raise — order calls never half-send."""
        token = self.get_session_token()
        if not token:
            raise DhanOrderError(
                "No authenticated Dhan session — log in before placing orders (fail-closed)"
            )
        return token

    def _require_client_id(self) -> str:
        """The dhanClientId order calls must carry (env fallback for restored sessions)."""
        client_id = (self._client_id or os.getenv("DHAN_CLIENT_ID", "")).strip()
        if not client_id:
            raise DhanOrderError(
                "Dhan client id unknown — log in again or set DHAN_CLIENT_ID in .env"
            )
        return client_id

    @staticmethod
    def _require_broker_order_id(order: BrokerOrder) -> str:
        if order.broker_order_id is None or not str(order.broker_order_id):
            raise DhanOrderError("order has no broker_order_id — cannot modify/cancel")
        return str(order.broker_order_id)

    def _api_request(self, method: str, path: str, json_body: dict | None = None) -> Any:
        """One DhanHQ v2 REST call (single retry on transient network errors)."""
        token = self._require_order_session()
        url = f"{self._api_base_url()}{path}"
        headers = {"access-token": token, "Content-Type": "application/json"}
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = requests.request(
                    method, url, json=json_body, headers=headers, timeout=self._http_timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "Dhan %s %s failed (attempt %s): %s", method, path, attempt + 1, exc
                )
                continue
            if resp.status_code in (401, 403):
                raise DhanOrderError(
                    "Dhan rejected the request (session expired or unauthorized) — log in again"
                )
            try:
                payload = resp.json() if resp.content else {}
            except ValueError:
                payload = {}
            if resp.status_code >= 400:
                reason = _rejection_reason(payload) or f"HTTP {resp.status_code}"
                raise DhanOrderError(f"Dhan order call failed: {reason}")
            return payload
        raise DhanOrderError(f"Could not reach Dhan ({last_exc}) — order call NOT sent")

    def _map_order_to_broker_payload(self, order: BrokerOrder) -> dict[str, Any]:
        """Translate the generic :class:`BrokerOrder` into a Dhan v2 packet."""
        packet: dict[str, Any] = {
            "dhanClientId": self._require_client_id(),
            "transactionType": (order.side or "BUY").upper(),
            "exchangeSegment": self._map_exchange_segment(order),
            "productType": self._map_product(order.product),
            "orderType": self._map_order_type(order.order_type),
            "validity": "DAY",
            "securityId": self._security_id_for_order(order.symbol),
            "quantity": int(order.quantity),
            "disclosedQuantity": 0,
            "price": float(order.limit_price) if order.limit_price is not None else 0.0,
            "afterMarketOrder": False,
        }
        if order.client_order_id:
            packet["correlationId"] = str(order.client_order_id)[:25]
        return packet

    @staticmethod
    def _map_exchange_segment(order: BrokerOrder) -> str:
        """Dhan segment token — NSE equity by default (options come with F-13)."""
        exchange = (order.exchange or "NSE").upper()
        if exchange in ("NSE", "NSE_EQ"):
            return "NSE_EQ"
        if exchange in ("BSE", "BSE_EQ"):
            return "BSE_EQ"
        if exchange in ("NFO", "NSE_FNO"):
            return "NSE_FNO"
        raise DhanOrderError(f"unsupported exchange for Dhan orders: {exchange!r}")

    @staticmethod
    def _map_product(product: str | None) -> str:
        mapping = {
            None: "INTRADAY",
            "": "INTRADAY",
            "INTRADAY": "INTRADAY",
            "MIS": "INTRADAY",
            "DELIVERY": "CNC",
            "CNC": "CNC",
            "MARGIN": "MARGIN",
            "MTF": "MTF",
        }
        key = product.upper() if isinstance(product, str) else product
        if key not in mapping:
            raise DhanOrderError(f"unsupported product for Dhan orders: {product!r}")
        return mapping[key]

    @staticmethod
    def _map_order_type(order_type: str | None) -> str:
        mapping = {
            None: "MARKET",
            "": "MARKET",
            "MARKET": "MARKET",
            "LIMIT": "LIMIT",
            "STOP_LOSS": "STOP_LOSS",
            "SL": "STOP_LOSS",
            "STOP_LOSS_MARKET": "STOP_LOSS_MARKET",
            "SL-M": "STOP_LOSS_MARKET",
        }
        key = order_type.upper() if isinstance(order_type, str) else order_type
        if key not in mapping:
            raise DhanOrderError(f"unsupported order type for Dhan orders: {order_type!r}")
        return mapping[key]

    @staticmethod
    def _extract_order_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for key in ("orderId", "order_id"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    def _fetch_order_rows(self) -> list[dict]:
        """Raw Dhan v2 order-book rows (``GET /v2/orders``)."""
        payload = self._api_request("GET", _ORDERS_PATH)
        if isinstance(payload, dict):
            payload = payload.get("data")
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise DhanOrderError("Dhan order book response was not a list of orders")
        return [row for row in payload if isinstance(row, dict)]

    def _order_from_row(self, row: dict[str, Any]) -> BrokerOrder:
        """Best-effort mapping of one Dhan order-book row to :class:`BrokerOrder`."""

        def _int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        def _float(value: Any) -> float | None:
            try:
                return float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        status = str(row.get("orderStatus") or row.get("status") or "OPEN").upper()
        order_id = row.get("orderId") or row.get("order_id")
        return BrokerOrder(
            broker_order_id=BrokerOrderId(str(order_id)) if order_id not in (None, "") else None,
            client_order_id=row.get("correlationId") or row.get("correlation_id"),
            symbol=str(row.get("tradingSymbol") or row.get("tradingsymbol") or ""),
            side=str(row.get("transactionType") or row.get("transaction_type") or "BUY").upper(),
            quantity=_int(row.get("quantity")),
            order_type=str(row.get("orderType") or row.get("order_type") or "MARKET").upper(),
            limit_price=_float(row.get("price")),
            status=status,
            filled_quantity=_int(row.get("filledQty") or row.get("filled_quantity")),
            average_fill_price=_float(row.get("averageTradedPrice") or row.get("average_price")),
            exchange=str(row.get("exchangeSegment") or "NSE_EQ"),
            product=row.get("productType") or row.get("product"),
            created_at=row.get("createTime") or row.get("created_at"),
            tag={"raw_status": status},
        )

    def _security_id_for_order(self, symbol: str) -> str:
        """Dhan securityId for ``symbol`` — env override first, scrip master second.

        * ``DHAN_SECURITY_IDS`` (JSON ``{"RELIANCE": "2885", ...}``) wins;
        * otherwise the public compact scrip master CSV is downloaded once
          per process and filtered to NSE equity rows;
        * unknown symbol → :class:`DhanOrderError` (fail-closed — an order
          for an unmapped instrument must never guess an id).
        """
        key = (symbol or "").strip().upper()
        if not key:
            raise DhanOrderError("order symbol is empty — cannot resolve a Dhan security id")
        if key in self._order_security_ids:
            return self._order_security_ids[key]

        raw = os.getenv("DHAN_SECURITY_IDS", "").strip()
        if raw:
            try:
                overrides = json.loads(raw)
                if isinstance(overrides, dict):
                    self._order_security_ids.update(
                        {str(k).upper(): str(v) for k, v in overrides.items()}
                    )
            except json.JSONDecodeError:
                logger.warning("DHAN_SECURITY_IDS is not valid JSON — ignored")
        if key in self._order_security_ids:
            return self._order_security_ids[key]

        self._order_security_ids.update(_load_scrip_master(self._http_timeout))
        if key not in self._order_security_ids:
            raise DhanOrderError(
                f"no Dhan security id for {key!r} — not in the scrip master; "
                "set DHAN_SECURITY_IDS in .env to map it explicitly"
            )
        return self._order_security_ids[key]

    @staticmethod
    def _api_base_url() -> str:
        return os.getenv("DHAN_API_BASE_URL", _API_BASE_URL).rstrip("/")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _login_failure(self, message: str) -> dict[str, Any]:
        """Record a failed login: no temp context may survive a rejection."""
        self._temp_auth_context = None
        return {"success": False, "message": message, "requires_totp": False}

    def _generate_token(self, client_id: str, pin: str, totp: str) -> dict[str, Any]:
        """One ``generateAccessToken`` call (single retry on transient errors).

        Raises :class:`DhanAuthError` with a user-facing message when the
        request is rejected (HTTP 401/403, error payload, non-success).
        """
        url = f"{self._auth_base_url()}{_GENERATE_TOKEN_PATH}"
        params = {
            "dhanClientId": client_id,
            "pin": pin,
            "totp": totp,
        }
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = requests.post(url, params=params, timeout=self._http_timeout)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self._retries:
                    time.sleep(1.0)
        else:
            raise DhanAuthError(
                "Could not reach Dhan — check your connection and try again"
            ) from last_exc

        try:
            payload: Any = resp.json()
        except ValueError:
            payload = None

        if not resp.ok:
            default = (
                "Invalid Dhan client ID, PIN, or TOTP"
                if resp.status_code in (401, 403)
                else f"Dhan request failed (HTTP {resp.status_code})"
            )
            raise DhanAuthError(_rejection_reason(payload) or default)
        reason = _rejection_reason(payload)
        if reason:
            raise DhanAuthError(reason)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _extract_token(payload: dict[str, Any]) -> str | None:
        """Pull the access token out of either known response shape."""
        token = (
            payload.get("accessToken")
            or payload.get("access_token")
            or (payload.get("data") or {}).get("accessToken")
        )
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    def _compute_expiry(self, payload: dict[str, Any]) -> datetime:
        """Prefer a server-provided expiry; fall back to the configured TTL."""
        raw = (
            payload.get("expiryTime")
            or payload.get("expiry_time")
            or payload.get("expires_at")
        )
        if isinstance(raw, str) and raw.strip():
            try:
                dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    # Session comparisons are naive-local everywhere (same
                    # convention as MStockBroker._now); normalize to local.
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except ValueError:
                logger.warning("Dhan returned unparseable expiryTime %r — using TTL", raw)
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            return self._now() + timedelta(seconds=int(expires_in))
        return self._now() + self._session_ttl

    @staticmethod
    def _auth_base_url() -> str:
        return os.getenv("DHAN_AUTH_BASE_URL", _AUTH_BASE_URL).rstrip("/")

    @staticmethod
    def _api_key_available() -> bool:
        """True when a Dhan data API key is configured in the environment."""
        return bool(os.getenv("DHAN_API_KEY", "").strip())

    @staticmethod
    def _now() -> datetime:
        return datetime.now()
