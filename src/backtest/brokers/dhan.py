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
)

__all__ = ["DhanBroker", "DhanAuthError"]

logger = logging.getLogger("backtest.brokers.dhan")

# Dhan auth + data API endpoints (DhanHQ v2).
_AUTH_BASE_URL = "https://auth.dhan.co"
_GENERATE_TOKEN_PATH = "/app/generateAccessToken"

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


def _rejection_reason(payload: Any) -> str | None:
    """Extract a user-facing rejection reason from a Dhan payload, if any."""
    if not isinstance(payload, dict):
        return None
    for key in ("errorMessage", "error_message", "error", "message", "remarks"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_MESSAGE_LEN]
    return None


class DhanBroker(BrokerAuthBase):
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
        """Seed the in-memory session from a remembered token (remember-session)."""
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
