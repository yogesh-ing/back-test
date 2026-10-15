"""Upstox broker — generic auth contract (BrokerAuthBase) implementation.

Auth flow (from the official Upstox Python SDK,
``reference-code/upstox-python-master`` + the current Upstox developer docs):

    GET  https://api.upstox.com/v2/login/authorization/dialog
             ?client_id=<api-key>&redirect_uri=<uri>&response_type=code&state=<s>
         → the user signs in ON UPSTOX (username/password/TOTP happen there,
           never through this server — Upstox has no credentials endpoint)
    GET  <redirect_uri>?code=<auth-code>&state=<s>   (browser redirect)
    POST https://api.upstox.com/v2/login/authorization/token   (form-encoded)
         code, client_id, client_secret, redirect_uri,
         grant_type=authorization_code        Api-Version: 2.0
    → ``{"access_token": "...", ...}``

Unlike mStock (user+password then TOTP) and Dhan (client-id+PIN then TOTP),
Upstox is a browser OAuth dance — there is no server-side login endpoint to
call with credentials. To keep the UI and the
:class:`~backtest.brokers.session_manager.BrokerSessionManager` contract
identical across brokers, the two standard steps map like this:

* ``login(api_key, api_secret)`` — validates the Upstox **app** credentials
  (client_id = API key, client_secret = API secret from
  ``account.upstox.com/developer/apps``) plus the server-side
  ``UPSTOX_REDIRECT_URI``, parks them, mints a CSRF ``state`` and returns
  ``requires_totp: True`` together with ``auth_url`` — the Upstox dialog the
  UI opens for the user.
* ``verify_totp(auth_code)`` — exchanges the authorization code for the
  access token. The code arrives either from the local OAuth callback
  (``GET /api/broker/upstox/callback``, state-validated and parked by
  :meth:`UpstoxBroker.park_auth_code`) or pasted by hand into the code field
  (the auth modal widens that input for this broker).

Session lifetime: an Upstox access token dies at **03:30 IST the next day**
regardless of when it was issued, so expiry is computed from that wall clock
(``UPSTOX_SESSION_TTL_MINUTES`` only as a fallback when parsing fails).

Security invariants (same as MStockBroker / DhanBroker):

* the API secret is used for the token call and immediately discarded —
  never stored, logged, or echoed in any response;
* the raw access token never appears in any return value of the contract
  methods (only ``get_session_token()``, consumed by the session manager);
* all session state is in-memory only (lost on restart, by design).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
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

__all__ = [
    "UpstoxBroker",
    "UpstoxAuthError",
    "UpstoxOrderError",
    "resolve_instrument_key",
]

logger = logging.getLogger("backtest.brokers.upstox")

# Upstox API v2 endpoints (per reference-code/upstox-python-master).
_AUTH_BASE_URL = "https://api.upstox.com/v2"
_DIALOG_PATH = "/login/authorization/dialog"
_TOKEN_PATH = "/login/authorization/token"

_API_BASE_URL = "https://api.upstox.com/v2"
_ORDERS_PATH = "/order/place"
_ORDER_MODIFY_PATH = "/order/modify"
_ORDER_CANCEL_PATH = "/order/cancel"
_ORDER_BOOK_PATH = "/order/retrieve-all"
_MARGIN_PATH = "/charges/margin"
_FUNDS_PATH = "/user/get-funds-and-margin"

#: Sent as ``Api-Version`` on every v2 call (the SDK always sets it).
_API_VERSION = "2.0"

#: Instrument master — trading_symbol → instrument_key (e.g. NSE_EQ|INE002A01018).
#: Downloaded lazily, at most once per process; a failure fails closed for
#: unmapped symbols (an order must never guess an instrument).
_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

#: NSE index instrument keys (Upstox convention, documented examples:
#: ``NSE_INDEX|Nifty 50`` / ``NSE_INDEX|Nifty Bank``). Override via the
#: ``UPSTOX_INSTRUMENT_KEYS`` env JSON if Upstox renames them again.
_INDEX_INSTRUMENT_KEYS: dict[str, str] = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "NIFTY50": "NSE_INDEX|Nifty 50",
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "NIFTY BANK": "NSE_INDEX|Nifty Bank",
    "NIFTYBANK": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|Sensex",
    "BANKSENSEX": "BSE_INDEX|BSE Bank",
}

#: Upstox order statuses that mean "no more fills will come".
_UPSTOX_TERMINAL_STATUSES = {"complete", "cancelled", "rejected", "expired"}

#: Upstox status → the contract's :data:`~backtest.brokers.base.ORDER_STATUSES`.
_STATUS_MAP: dict[str, str] = {
    "open": "OPEN",
    "modified": "OPEN",
    "not modified": "OPEN",
    "validation pending": "OPEN",
    "trigger pending": "OPEN",
    "open pending": "OPEN",
    "modify pending": "OPEN",
    "cancel pending": "OPEN",
    "complete": "FILLED",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "expired": "EXPIRED",
}

# Fallback session lifetime when the expiry cannot be computed. Upstox tokens
# run until 03:30 IST next day — at most ~24h from issue.
DEFAULT_SESSION_TTL_MINUTES = 1440.0

# PRD session state machine: "expiring_soon" inside the last 30 minutes.
EXPIRING_SOON_WINDOW_MINUTES = 30.0

_AUTH_CODE_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{4,}$")
_MAX_MESSAGE_LEN = 200

#: NSE compact option symbol — ``NIFTY26OCT24500CE``: YY + MON + strike +
#: side (the NSE convention every Indian broker reuses, incl. Upstox).
_OPTION_SYMBOL_RE = re.compile(
    r"^(NIFTY|BANKNIFTY)(\d{2})([A-Z]{3})(\d+)(CE|PE)$"
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: Asia/Kolkata — the wall clock Upstox cuts sessions on.
_IST = timezone(timedelta(hours=5, minutes=30))


class UpstoxAuthError(Exception):
    """Upstox rejected the auth request (bad key/secret, bad auth code)."""


class UpstoxOrderError(RuntimeError):
    """Upstox rejected an order call (place/modify/cancel/book/margin)."""


# Process-wide instrument-master caches. Rows are parsed once (a few MB,
# changes at most daily) and both the symbol → key map and the option-chain
# builder read from them; a failure fails closed for unmapped symbols (an
# order must never guess an instrument).
_MASTER_ROWS_CACHE: list[dict] | None = None
_INSTRUMENT_KEY_CACHE: dict[str, str] | None = None


def _load_master_rows(timeout: float = 30.0) -> list[dict]:
    """Download + parse the Upstox instrument master (gzipped JSON) once.

    Returns the raw rows (dicts only). Cached per process — the file is a
    few MB and changes at most daily. A download failure caches an empty
    list (one attempt per process, then callers fail closed).
    """
    global _MASTER_ROWS_CACHE
    if _MASTER_ROWS_CACHE is not None:
        return _MASTER_ROWS_CACHE
    url = os.getenv("UPSTOX_INSTRUMENT_MASTER_URL", _MASTER_URL)
    rows: list[dict] = []
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        raw = resp.content
        if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        payload = json.loads(raw.decode("utf-8"))
        candidates = payload.values() if isinstance(payload, dict) else payload
        rows = [row for row in candidates if isinstance(row, dict)]
        logger.info("Upstox instrument master loaded: %s rows", len(rows))
    except (requests.RequestException, ValueError, OSError) as exc:
        logger.warning("Upstox instrument master download failed: %s", exc)
    _MASTER_ROWS_CACHE = rows
    return rows


def _load_instrument_keys(timeout: float = 30.0) -> dict[str, str]:
    """``{TRADING_SYMBOL: instrument_key}`` plus composite ``EXCHANGE:SYMBOL``.

    Built from :func:`_load_master_rows`; an empty map means the download
    failed (the caller then fails closed, pointing at the
    ``UPSTOX_INSTRUMENT_KEYS`` override).
    """
    global _INSTRUMENT_KEY_CACHE
    if _INSTRUMENT_KEY_CACHE is not None:
        return _INSTRUMENT_KEY_CACHE
    mapping: dict[str, str] = {}
    for row in _load_master_rows(timeout):
        key = str(row.get("instrument_key") or row.get("trading_token") or "").strip()
        symbol = str(
            row.get("trading_symbol") or row.get("tradingsymbol") or row.get("symbol") or ""
        ).strip().upper()
        exchange = str(row.get("exchange") or "").strip().upper()
        if not key or not symbol:
            continue
        # Plain symbol first-wins (NSE equity / index rows win over the
        # rest); the EXCHANGE:SYMBOL composite is always unambiguous.
        mapping.setdefault(symbol, key)
        if exchange:
            mapping.setdefault(f"{exchange}:{symbol}", key)
    _INSTRUMENT_KEY_CACHE = mapping
    return mapping


def resolve_instrument_key(symbol: str, timeout: float = 30.0) -> str:
    """``symbol`` → Upstox ``instrument_key`` (``NSE_EQ|INE002A01018``).

    * already an instrument key (contains ``|``) → passed through;
    * ``UPSTOX_INSTRUMENT_KEYS`` env JSON (``{"NIFTY": "NSE_INDEX|Nifty 50"}``)
      wins;
    * seeded index keys (``NIFTY``/``BANKNIFTY``/…) next;
    * the instrument master CSV-equivalent (complete.json.gz) last.

    Unknown symbol → :class:`UpstoxOrderError` (fail-closed — an order for an
    unmapped instrument must never guess a key).
    """
    key = str(symbol or "").strip()
    if not key:
        raise UpstoxOrderError("order symbol is empty — cannot resolve an Upstox instrument key")
    if "|" in key:
        return key

    raw = os.getenv("UPSTOX_INSTRUMENT_KEYS", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                normalized = {str(k).upper(): str(v) for k, v in overrides.items()}
                if key.upper() in normalized:
                    return normalized[key.upper()]
        except json.JSONDecodeError:
            logger.warning("UPSTOX_INSTRUMENT_KEYS is not valid JSON — ignored")

    if key.upper() in _INDEX_INSTRUMENT_KEYS:
        return _INDEX_INSTRUMENT_KEYS[key.upper()]

    keys = _load_instrument_keys(timeout)
    hit = keys.get(key.upper()) or keys.get(f"NSE:{key.upper()}") or keys.get(f"NFO:{key.upper()}")
    if not hit:
        raise UpstoxOrderError(
            f"no Upstox instrument key for {key!r} — not in the instrument master; "
            "set UPSTOX_INSTRUMENT_KEYS in .env to map it explicitly"
        )
    return hit


def _rejection_reason(payload: Any) -> str | None:
    """Extract a user-facing rejection reason from an Upstox payload, if any."""
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                for key in ("message", "error", "details"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:_MAX_MESSAGE_LEN]
            elif isinstance(item, str) and item.strip():
                return item.strip()[:_MAX_MESSAGE_LEN]
    for key in ("message", "error", "remarks", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_MESSAGE_LEN]
    return None


def _to_decimal(value: Any) -> Decimal | None:
    """Coerce a master field to Decimal; ``None`` when absent/unparseable."""
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except Exception:  # noqa: BLE001 — bad numeric field → caller drops the row
        return None
    if result != result:  # NaN
        return None
    return result


def _parse_expiry(raw: Any) -> date | None:
    """Parse an instrument-master expiry (``2026-10-30`` / ``30-10-2026`` /
    ``30 Oct 2026`` styles) — ``None`` when unparseable (never guessed)."""
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y"):
        try:
            return datetime.strptime(text[:15].strip(), fmt).date()
        except ValueError:
            continue
    try:
        parsed = date.fromisoformat(text[:10])
        return parsed
    except ValueError:
        return None


def _expiry_from_symbol(match: re.Match) -> date | None:
    """Expiry from a compact NSE option symbol (YY + MON, month-end).

    Fallback for master rows without a real ``expiry`` field — the same
    approximation MStockBroker uses. Returns ``None`` when the result would
    be in the past (an expired contract is dropped, never dated into the
    future).
    """
    try:
        year = 2000 + int(match.group(2))
        month = _MONTHS[match.group(3)]
        last_day = (date(year, month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        expiry = last_day
        return expiry if expiry >= date.today() else None
    except (ValueError, KeyError):
        return None


def _depth_best(side: Any, price_key: str = "price") -> float | None:
    """Best price from one side of a market-depth array (or ``None``)."""
    if not isinstance(side, list) or not side:
        return None
    first = side[0]
    if isinstance(first, dict):
        value = first.get(price_key, first.get("price"))
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    try:
        return float(first)
    except (TypeError, ValueError):
        return None


class UpstoxBroker(BrokerAuthBase, BrokerOrderBase):
    """Upstox implementation of the generic two-step auth contract.

    State held in-memory only; lost on restart by design (same as mStock and
    Dhan). The temp auth context between ``login()`` and ``verify_totp()``
    holds the API key/secret + CSRF state — the secret is dropped the moment
    the token is minted (or the flow fails).
    """

    broker_name = "upstox"
    broker_display_name = "Upstox"

    def __init__(
        self,
        session_ttl_minutes: float | None = None,
        http_timeout: float = 10.0,
    ) -> None:
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._temp_auth_context: dict[str, Any] | None = None
        self._temp_api_key: str | None = None
        self._temp_api_secret: str | None = None
        #: Authorization code parked by the local OAuth callback (state-checked).
        self._parked_auth_code: str | None = None
        if session_ttl_minutes is None:
            try:
                session_ttl_minutes = float(
                    os.getenv("UPSTOX_SESSION_TTL_MINUTES", DEFAULT_SESSION_TTL_MINUTES)
                )
            except (TypeError, ValueError):
                session_ttl_minutes = DEFAULT_SESSION_TTL_MINUTES
        self._session_ttl = timedelta(minutes=max(session_ttl_minutes, 0.0))
        self._http_timeout = http_timeout
        # Transient single-request retry guard (same live-session lesson as
        # mStock/Dhan: one retry on transient network failures).
        self._retries = 1
        # Kept past the handshake — the API key is an app identifier, not a
        # secret (the secret itself is discarded after the token exchange).
        self._client_id: str | None = None

    # ------------------------------------------------------------------
    # Step 1 — app credentials (client_id + client_secret; local validation)
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Validate the Upstox app credentials; defer the exchange to step 2.

        Upstox's token endpoint needs an authorization code that only the
        browser OAuth dance can produce, so step 1 validates and parks the
        app credentials and hands the UI the ``auth_url`` to open. Nothing
        here touches the network.
        """
        api_key = (username or "").strip()
        api_secret = password or ""
        if not api_key or not api_secret:
            return self._login_failure("Upstox API key and API secret are required")
        if any(ch.isspace() for ch in api_key) or any(ch.isspace() for ch in api_secret):
            return self._login_failure("Upstox API key / secret must not contain spaces")
        if len(api_key) < 6 or len(api_secret) < 6:
            return self._login_failure("Upstox API key / secret look too short")

        # Validate server-side config presence early so the user gets a clear
        # message instead of a mystery redirect failure later.
        if not self._redirect_uri():
            return self._login_failure(
                "Upstox redirect URI is not configured on the server (UPSTOX_REDIRECT_URI)"
            )

        # Parked ONLY for the duration of the code exchange; cleared on
        # success, failure and logout. Never logged.
        self._temp_api_key = api_key
        self._temp_api_secret = api_secret
        self._parked_auth_code = None
        self._temp_auth_context = {
            "received_at": self._now().isoformat(),
            "state": secrets.token_urlsafe(16),
        }
        auth_url = self.auth_url()
        return {
            "success": True,
            "message": (
                "App credentials accepted — open the Upstox login page, sign in "
                "there, then paste the auth code from the redirect URL"
            ),
            "requires_totp": True,
            "auth_url": auth_url,
        }

    # ------------------------------------------------------------------
    # Step 2 — authorization code (the actual token exchange)
    # ------------------------------------------------------------------

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        """Exchange the Upstox authorization code for an access token.

        The code comes from the local OAuth callback when the redirect landed
        on this app (:meth:`park_auth_code`, state-validated), otherwise from
        whatever the user pasted into the code field. The API secret parked
        by ``login()`` is read here and never logged; it is dropped the
        moment the exchange succeeds (or fails permanently).
        """
        typed = (totp_code or "").strip()
        if self._temp_auth_context is None:
            return {
                "success": False,
                "message": "Log in with your Upstox API key/secret before pasting the code",
                "expires_at": "",
            }

        code = self._parked_auth_code or typed
        if not code:
            return {
                "success": False,
                "message": (
                    "Enter the authorization code from the Upstox redirect URL "
                    "(a long code, not a 6-digit TOTP)"
                ),
                "expires_at": "",
            }
        if not self._parked_auth_code:
            # Manual paste path — the callback never landed (state mismatch,
            # app restarted, redirect URI not reachable). Upstox codes are
            # long opaque strings; a 6-digit TOTP is never valid here.
            if not _AUTH_CODE_PATTERN.fullmatch(code):
                return {
                    "success": False,
                    "message": (
                        "That does not look like an Upstox authorization code — "
                        "copy the full ``code`` value from the redirect URL"
                    ),
                    "expires_at": "",
                }

        api_key = (self._temp_api_key or "").strip()
        api_secret = self._temp_api_secret or ""
        try:
            payload = self._exchange_code(code, api_key, api_secret)
        except UpstoxAuthError as exc:
            # A rejected code is single-use upstream — force a fresh browser
            # round trip instead of letting the user retry a dead code.
            self._parked_auth_code = None
            return {"success": False, "message": str(exc), "expires_at": ""}
        except requests.RequestException:
            logger.warning("Upstox token request failed (network error)")
            return {
                "success": False,
                "message": "Could not reach Upstox — check your connection and try again",
                "expires_at": "",
            }

        token = self._extract_token(payload)
        if not token:
            self._parked_auth_code = None
            return {
                "success": False,
                "message": "Upstox did not return an access token",
                "expires_at": "",
            }

        expires_at = self._compute_expiry(payload)
        self._access_token = token
        self._expires_at = expires_at
        self._client_id = api_key  # account identifier, not a secret
        self._temp_auth_context = None
        self._temp_api_key = None
        self._temp_api_secret = None
        self._parked_auth_code = None
        logger.info("Upstox session established (expires at %s)", expires_at.isoformat())
        return {
            "success": True,
            "message": "Upstox session established",
            "expires_at": expires_at.isoformat(),
        }

    def auth_url(self) -> str:
        """The Upstox authorization dialog for the pending (or default) login.

        Built from the API key + redirect URI parked by ``login()`` plus the
        CSRF ``state`` — the browser sends both back on the redirect.
        """
        ctx = self._temp_auth_context or {}
        client_id = (self._temp_api_key or os.getenv("UPSTOX_CLIENT_ID", "")).strip()
        state = str(ctx.get("state") or "")
        params = [
            ("client_id", client_id),
            ("redirect_uri", self._redirect_uri() or ""),
            ("response_type", "code"),
        ]
        if state:
            params.append(("state", state))
        query = "&".join(f"{k}={requests.utils.quote(v, safe='')}" for k, v in params)
        return f"{self._auth_base_url()}{_DIALOG_PATH}?{query}"

    def park_auth_code(self, code: str, state: str) -> tuple[bool, str]:
        """Receive the authorization code from the local OAuth callback.

        Called by ``GET /api/broker/upstox/callback``. Fail-closed: a code is
        only parked while a ``login()`` is pending AND the CSRF state matches
        the one minted there; the code is single-use (exchanged once, then
        dropped). The code itself is never logged or echoed back.
        """
        if self._temp_auth_context is None:
            return False, "No Upstox login is in progress — start the login in the app first"
        expected = str(self._temp_auth_context.get("state") or "")
        if not expected or not state or not secrets.compare_digest(expected, state):
            return False, "State mismatch — paste the authorization code into the app instead"
        code = (code or "").strip()
        if not _AUTH_CODE_PATTERN.fullmatch(code):
            return False, "The redirect did not carry a usable authorization code"
        self._parked_auth_code = code
        logger.info("Upstox authorization code parked from the OAuth callback")
        return True, "ok"

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
        self._temp_api_key = None
        self._temp_api_secret = None
        self._parked_auth_code = None
        logger.info("Upstox session restored from remembered token (expires %s)", expires_at)

    def logout(self) -> None:
        """Clear all in-memory session state (token, expiry, temp context)."""
        had_session = self._access_token is not None
        self._access_token = None
        self._expires_at = None
        self._temp_auth_context = None
        self._temp_api_key = None
        self._temp_api_secret = None
        self._parked_auth_code = None
        if had_session:
            logger.info("Upstox session cleared (logout)")

    # ------------------------------------------------------------------
    # Order lifecycle contract
    #
    # Upstox v2 REST: JSON bodies, ``Authorization: Bearer`` +
    # ``Api-Version: 2.0``. Fail-closed: no authenticated session → raise
    # before any bytes leave the process (never half-send).
    # ------------------------------------------------------------------

    def place_order(self, order: BrokerOrder) -> BrokerOrderId:
        """Place ``order`` — ``POST /v2/order/place``.

        Returns the broker's order id (what modify/cancel reference).
        ``order.client_order_id`` is sent as ``tag`` (Upstox's only free-form
        audit field; truncated to its 20-char cap).
        """
        payload = self._api_request(
            "POST", _ORDERS_PATH, json_body=self._map_order_to_broker_payload(order)
        )
        order_id = self._extract_order_id(payload)
        if order_id is None:
            raise UpstoxOrderError("Upstox did not return an order id for the placed order")
        logger.info("Upstox order placed: %s %s x%s", order.side, order.symbol, order.quantity)
        return BrokerOrderId(order_id)

    def modify_order(self, order: BrokerOrder) -> None:
        """Amend an open order — ``PUT /v2/order/modify``."""
        broker_order_id = self._require_broker_order_id(order)
        body = {
            "order_id": broker_order_id,
            "validity": "DAY",
            "price": float(order.limit_price) if order.limit_price is not None else 0.0,
            "order_type": self._map_order_type(order.order_type),
            "trigger_price": float(order.tag.get("trigger_price") or 0.0),
            "quantity": int(order.quantity),
            "disclosed_quantity": 0,
        }
        self._api_request("PUT", _ORDER_MODIFY_PATH, json_body=body)

    def cancel_order(self, order: BrokerOrder) -> None:
        """Cancel an open order — ``DELETE /v2/order/cancel?order_id=...``."""
        broker_order_id = self._require_broker_order_id(order)
        self._api_request(
            "DELETE",
            _ORDER_CANCEL_PATH,
            params={"order_id": broker_order_id},
        )

    def get_order_book(self) -> list[BrokerOrder]:
        """Every order Upstox currently knows — ``GET /v2/order/retrieve-all``."""
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
            if str(row.get("order_id") or "") != target:
                continue
            status = str(row.get("status") or "").strip().lower()
            try:
                filled_qty = int(row.get("filled_quantity") or 0)
            except (TypeError, ValueError):
                filled_qty = 0
            if filled_qty <= 0 and status not in _UPSTOX_TERMINAL_STATUSES:
                return None  # open or otherwise not executed — no fill yet
            if status == "rejected":
                return None
            price = row.get("average_price") or row.get("price")
            fill_row = {
                "tradingsymbol": row.get("instrument_token"),
                "transaction_type": row.get("transaction_type"),
                "quantity": filled_qty or row.get("quantity"),
                "filled_quantity": filled_qty or row.get("quantity"),
                "price": price,
                "order_id": row.get("order_id"),
            }
            logger.info("Upstox order %s polled: %s", target, status)
            return fill_row
        return None

    # ------------------------------------------------------------------
    # Option chain + option quotes (P1.1 quote-provider contract)
    #
    # The bridge's LiveChainProvider calls these two on whatever broker the
    # runner's source resolves to. Chains come from the instrument master
    # (same approach as MStockBroker.get_option_chain); quotes come from the
    # authenticated /market-quote/quotes endpoint (depth → bid/ask).
    # ------------------------------------------------------------------

    def get_option_chain(self, underlying: str) -> list[Any]:
        """All valid option contracts for ``underlying`` (instrument master).

        Filters the Upstox instrument master to index-option rows of the
        given underlying (``"NIFTY"`` / ``"BANKNIFTY"`` — the V1 set), then
        parses each row into an :class:`OptionContract`. Unparseable rows are
        skipped, never guessed. The master itself is a public asset, so no
        session is required for this call (quotes are session-gated).
        """
        from backtest.instruments.option import OptionContract

        name = str(underlying or "").strip().upper()
        if not name:
            return []
        rows = _load_master_rows(self._http_timeout)
        if not rows:
            logger.warning("Upstox option chain for %s: instrument master empty", name)
            return []

        contracts: list[Any] = []
        for row in rows:
            symbol = str(
                row.get("trading_symbol") or row.get("tradingsymbol") or row.get("symbol") or ""
            ).strip().upper()
            if not symbol.startswith(name):
                continue
            opt_type = str(row.get("instrument_type") or "").strip().upper()
            if opt_type not in ("CE", "PE") and not _OPTION_SYMBOL_RE.fullmatch(symbol):
                continue
            contract = self._parse_option_contract(row, name, symbol)
            if isinstance(contract, OptionContract) and contract.is_valid():
                contracts.append(contract)
        logger.info("Upstox option chain for %s: %d contracts", name, len(contracts))
        return contracts

    def get_option_quote(self, instrument_token: Any) -> dict[str, Any]:
        """L1 quote for one contract — ``GET /v2/market-quote/quotes``.

        Accepts an instrument key (``NFO|...``), a trading symbol, or a bare
        :class:`OptionContract` (same acceptance as MStockBroker). Returns
        ``{"ltp", "bid", "ask", "volume", "oi"}`` — the keys
        :class:`LiveQuoteProvider` reads. Bid/ask come from market depth when
        Upstox returns it; otherwise they stay 0 and the caller falls back to
        LTP.
        """
        key = self._quote_key(instrument_token)
        payload = self._api_request(
            "GET", "/market-quote/quotes", params={"instrument_key": key}
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        entry = None
        if isinstance(data, dict):
            entry = data.get(key)
            if entry is None and len(data) == 1:
                entry = next(iter(data.values()))
        if not isinstance(entry, dict):
            raise UpstoxOrderError(f"Upstox returned no quote row for {key!r}")

        depth = entry.get("depth") if isinstance(entry.get("depth"), dict) else {}
        bid = _depth_best(depth.get("buy"), price_key="price")
        ask = _depth_best(depth.get("sell"), price_key="price")
        return {
            "ltp": float(entry.get("last_price") or 0.0),
            "bid": bid if bid is not None else float(entry.get("bid") or 0.0),
            "ask": ask if ask is not None else float(entry.get("ask") or 0.0),
            "volume": float(entry.get("volume") or entry.get("total_quantity") or 0.0),
            "oi": float(entry.get("oi") or entry.get("open_interest") or 0.0),
        }

    @staticmethod
    def _quote_key(instrument_token: Any) -> str:
        """Resolve whatever the caller passed into a quote API key."""
        symbol = getattr(instrument_token, "trading_symbol", None)
        token = getattr(instrument_token, "instrument_token", None)
        raw = str(symbol or token or instrument_token or "").strip()
        if "|" in raw:
            return raw
        resolved = resolve_instrument_key(raw)
        return resolved

    @staticmethod
    def _parse_option_contract(
        row: dict[str, Any], underlying: str, symbol: str
    ) -> Any | None:
        """Parse one instrument-master row into an OptionContract (or ``None``).

        Structured master fields win (``strike_price`` / ``expiry`` /
        ``lot_size``); the compact ``NIFTY26OCT24500CE`` symbol pattern is the
        fallback for rows that omit them. Never guesses: an unparseable row is
        dropped.
        """
        try:
            from datetime import date as _date

            from backtest.instruments.base import ExerciseType, SettlementType
            from backtest.instruments.option import OptionContract

            match = _OPTION_SYMBOL_RE.fullmatch(symbol)
            strike = _to_decimal(row.get("strike_price"))
            if strike is None and match:
                strike = Decimal(match.group(4))
            if strike is None or strike <= 0:
                return None

            expiry = _parse_expiry(row.get("expiry"))
            if expiry is None and match:
                expiry = _expiry_from_symbol(match)
            if expiry is None or expiry < _date.today():
                return None

            opt_type = str(row.get("instrument_type") or "").strip().upper()
            if opt_type not in ("CE", "PE"):
                opt_type = match.group(5) if match else ""
            if opt_type not in ("CE", "PE"):
                return None

            lot_size = int(_to_decimal(row.get("lot_size")) or 0)
            if lot_size < 1:
                return None

            # V1 constraints (OptionContract.validate): NSE/NFO book-keeping
            # regardless of Upstox's own exchange/segment labels.
            token = str(
                row.get("instrument_key") or row.get("trading_token") or ""
            ).strip() or symbol
            return OptionContract(
                instrument_token=token,
                trading_symbol=symbol,
                underlying=underlying,
                exchange="NSE",
                segment="NFO",
                expiry=expiry,
                strike=strike,
                option_type=opt_type,
                lot_size=lot_size,
                tick_size=_to_decimal(row.get("tick_size")) or Decimal("0.05"),
                contract_type=ExerciseType.EUROPEAN,
                settlement_type=SettlementType.CASH,
            )
        except Exception:  # noqa: BLE001 — a bad row must never kill the chain
            logger.debug("Upstox option row unparsable: %r", row, exc_info=True)
            return None

    def calculate_order_margin(self, order: BrokerOrder) -> MarginInfo:
        """Pre-trade margin check — ``POST /v2/charges/margin``.

        Available balance comes from ``GET /v2/user/get-funds-and-margin``
        when reachable; when it is not, ``available_margin`` stays ``None``
        and the order counts as funded (the requirement itself is known).
        """
        body = {
            "instruments": [
                {
                    "instrument_key": self._instrument_key_for_order(order.symbol),
                    "quantity": int(order.quantity),
                    "transaction_type": (order.side or "BUY").upper(),
                    "product": self._map_product(order.product),
                    "price": float(order.limit_price) if order.limit_price is not None else 0.0,
                }
            ]
        }
        payload = self._api_request("POST", _MARGIN_PATH, json_body=body)
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}

        initial = _first_number(data, ("required_margin", "final_margin", "total_margin"))
        if initial is None:
            raise UpstoxOrderError("Upstox margin response carried no margin amount")
        available = self._available_margin()
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
            raise UpstoxOrderError(
                "No authenticated Upstox session — log in before placing orders (fail-closed)"
            )
        return token

    @staticmethod
    def _require_broker_order_id(order: BrokerOrder) -> str:
        if order.broker_order_id is None or not str(order.broker_order_id):
            raise UpstoxOrderError("order has no broker_order_id — cannot modify/cancel")
        return str(order.broker_order_id)

    def _api_request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """One Upstox v2 REST call (single retry on transient network errors)."""
        token = self._require_order_session()
        url = f"{self._api_base_url()}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Api-Version": _API_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=headers,
                    timeout=self._http_timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "Upstox %s %s failed (attempt %s): %s", method, path, attempt + 1, exc
                )
                continue
            if resp.status_code in (401, 403):
                raise UpstoxOrderError(
                    "Upstox rejected the request (session expired or unauthorized) — log in again"
                )
            try:
                payload = resp.json() if resp.content else {}
            except ValueError:
                payload = {}
            if resp.status_code >= 400:
                reason = _rejection_reason(payload) or f"HTTP {resp.status_code}"
                raise UpstoxOrderError(f"Upstox order call failed: {reason}")
            return payload
        raise UpstoxOrderError(f"Could not reach Upstox ({last_exc}) — order call NOT sent")

    def _map_order_to_broker_payload(self, order: BrokerOrder) -> dict[str, Any]:
        """Translate the generic :class:`BrokerOrder` into an Upstox v2 packet."""
        order_type = self._map_order_type(order.order_type)
        price = float(order.limit_price) if order.limit_price is not None else 0.0
        if order_type == "MARKET":
            price = 0.0  # Upstox expects 0.0 for market orders
        packet: dict[str, Any] = {
            "quantity": int(order.quantity),
            "product": self._map_product(order.product),
            "validity": "DAY",
            "price": price,
            "instrument_token": self._instrument_key_for_order(order.symbol),
            "order_type": order_type,
            "transaction_type": (order.side or "BUY").upper(),
            "disclosed_quantity": 0,
            "trigger_price": float(order.tag.get("trigger_price") or 0.0),
        }
        if order.client_order_id:
            packet["tag"] = str(order.client_order_id)[:20]
        return packet

    @staticmethod
    def _map_product(product: str | None) -> str:
        """Upstox product tokens: ``I`` (intraday) / ``D`` (delivery) / ``MTF``."""
        mapping = {
            None: "I",
            "": "I",
            "INTRADAY": "I",
            "MIS": "I",
            "I": "I",
            "DELIVERY": "D",
            "CNC": "D",
            "D": "D",
            "MARGIN": "MTF",
            "MTF": "MTF",
        }
        key = product.upper() if isinstance(product, str) else product
        if key not in mapping:
            raise UpstoxOrderError(f"unsupported product for Upstox orders: {product!r}")
        return mapping[key]

    @staticmethod
    def _map_order_type(order_type: str | None) -> str:
        """Upstox order types: ``MARKET`` / ``LIMIT`` / ``SL`` / ``SL-M``."""
        mapping = {
            None: "MARKET",
            "": "MARKET",
            "MARKET": "MARKET",
            "LIMIT": "LIMIT",
            "STOP_LOSS": "SL",
            "SL": "SL",
            "SL-LIMIT": "SL",
            "STOP_LOSS_MARKET": "SL-M",
            "SL-M": "SL-M",
            "SLM": "SL-M",
        }
        key = order_type.upper() if isinstance(order_type, str) else order_type
        if key not in mapping:
            raise UpstoxOrderError(f"unsupported order type for Upstox orders: {order_type!r}")
        return mapping[key]

    @staticmethod
    def _extract_order_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for key in ("order_id", "orderId"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    def _fetch_order_rows(self) -> list[dict]:
        """Raw Upstox order-book rows (``GET /v2/order/retrieve-all``)."""
        payload = self._api_request("GET", _ORDER_BOOK_PATH)
        if isinstance(payload, dict):
            payload = payload.get("data")
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise UpstoxOrderError("Upstox order book response was not a list of orders")
        return [row for row in payload if isinstance(row, dict)]

    def _order_from_row(self, row: dict[str, Any]) -> BrokerOrder:
        """Best-effort mapping of one Upstox order-book row to :class:`BrokerOrder`."""

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

        raw_status = str(row.get("status") or "open").strip().lower()
        status = _STATUS_MAP.get(raw_status, "OPEN")
        filled = _int(row.get("filled_quantity"))
        quantity = _int(row.get("quantity"))
        if status == "OPEN" and 0 < filled < quantity:
            status = "PARTIAL"
        order_id = row.get("order_id")
        return BrokerOrder(
            broker_order_id=BrokerOrderId(str(order_id)) if order_id not in (None, "") else None,
            client_order_id=row.get("tag"),
            symbol=str(row.get("instrument_token") or ""),
            side=str(row.get("transaction_type") or "BUY").upper(),
            quantity=quantity,
            order_type=str(row.get("order_type") or "MARKET").upper(),
            limit_price=_float(row.get("price")),
            status=status,
            filled_quantity=filled,
            average_fill_price=_float(row.get("average_price")),
            exchange=str(row.get("exchange") or "") or None,
            product=row.get("product"),
            created_at=row.get("created_at"),
            tag={"raw_status": raw_status},
        )

    def _instrument_key_for_order(self, symbol: str) -> str:
        """Upstox instrument key for ``symbol`` (fail-closed on unknown)."""
        try:
            return resolve_instrument_key(symbol, self._http_timeout)
        except UpstoxOrderError:
            raise
        except Exception as exc:  # noqa: BLE001 — a resolution failure must not guess
            raise UpstoxOrderError(f"instrument key lookup failed for {symbol!r}: {exc}") from exc

    def _available_margin(self) -> float | None:
        """``available_margin`` from the funds endpoint, or ``None`` if unknown."""
        try:
            payload = self._api_request("GET", _FUNDS_PATH)
        except UpstoxOrderError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        # 2025-07-19: combined equity/commodity funds live under ``equity``.
        for scope in (data, data.get("equity") if isinstance(data.get("equity"), dict) else {}):
            value = _first_number(scope, ("available_margin", "available_balance"))
            if value is not None:
                return value
        return None

    @staticmethod
    def _api_base_url() -> str:
        return os.getenv("UPSTOX_API_BASE_URL", _API_BASE_URL).rstrip("/")

    @staticmethod
    def _auth_base_url() -> str:
        return os.getenv("UPSTOX_AUTH_BASE_URL", _AUTH_BASE_URL).rstrip("/")

    @staticmethod
    def _redirect_uri() -> str:
        return os.getenv("UPSTOX_REDIRECT_URI", "").strip()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _login_failure(self, message: str) -> dict[str, Any]:
        """Record a failed login: no temp context may survive a rejection."""
        self._temp_auth_context = None
        self._temp_api_key = None
        self._temp_api_secret = None
        self._parked_auth_code = None
        return {"success": False, "message": message, "requires_totp": False}

    def _exchange_code(self, code: str, api_key: str, api_secret: str) -> dict[str, Any]:
        """One ``/v2/login/authorization/token`` call (form-encoded, single retry).

        Raises :class:`UpstoxAuthError` with a user-facing message when the
        request is rejected (HTTP 401/403, error payload, non-success).
        """
        url = f"{self._auth_base_url()}{_TOKEN_PATH}"
        headers = {
            "Api-Version": _API_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        form = {
            "code": code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": self._redirect_uri(),
            "grant_type": "authorization_code",
        }
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = requests.post(url, data=form, headers=headers, timeout=self._http_timeout)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self._retries:
                    time.sleep(1.0)
        else:
            raise UpstoxAuthError(
                "Could not reach Upstox — check your connection and try again"
            ) from last_exc

        try:
            payload: Any = resp.json()
        except ValueError:
            payload = None

        if not resp.ok:
            default = (
                "Upstox rejected the authorization code (expired or already used)"
                if resp.status_code in (401, 403)
                else f"Upstox request failed (HTTP {resp.status_code})"
            )
            raise UpstoxAuthError(_rejection_reason(payload) or default)
        reason = _rejection_reason(payload)
        if reason and not isinstance(payload, dict):
            raise UpstoxAuthError(reason)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _extract_token(payload: dict[str, Any]) -> str | None:
        """Pull the access token out of the known response shapes."""
        token = payload.get("access_token")
        if not isinstance(token, str) and isinstance(payload.get("data"), dict):
            token = payload["data"].get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    def _compute_expiry(self, payload: dict[str, Any]) -> datetime:
        """Upstox sessions end at 03:30 IST the next day (documented).

        A server-provided ``expires_in``/``expires_at`` wins when present;
        otherwise the 03:30 IST wall clock is computed, falling back to the
        configured TTL if that somehow cannot be expressed.
        """
        raw = payload.get("expires_at")
        if isinstance(raw, str) and raw.strip():
            try:
                dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except ValueError:
                logger.warning("Upstox returned unparseable expires_at %r — using 03:30 IST", raw)
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            return self._now() + timedelta(seconds=int(expires_in))
        try:
            now_ist = datetime.now(_IST)
            expiry_ist = now_ist.replace(hour=3, minute=30, second=0, microsecond=0)
            if expiry_ist <= now_ist:
                expiry_ist += timedelta(days=1)
            # Naive-local, the convention everywhere else in this module.
            return expiry_ist.astimezone().replace(tzinfo=None)
        except Exception:  # noqa: BLE001 — never let the wall clock kill a login
            return self._now() + self._session_ttl

    @staticmethod
    def _now() -> datetime:
        return datetime.now()


def _first_number(scope: Any, keys: tuple[str, ...]) -> float | None:
    """First numeric value under ``keys`` in ``scope`` (bools excluded)."""
    if not isinstance(scope, dict):
        return None
    for key in keys:
        value = scope.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None
