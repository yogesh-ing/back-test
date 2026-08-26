"""mStock broker authentication (mStock Authentication UI epic, Task 1.2).

Implements the generic :class:`~backtest.brokers.base.BrokerAuthBase` contract
for mStock's TypeA connect API, mirroring the endpoint contract already used
by :mod:`backtest.live.auth`:

* step 1 — ``POST /openapi/typea/connect/login`` (Username + Password)
* step 2 — ``POST /openapi/typea/session/verifytotp`` (api_key + totp)

API credentials (``MSTOCK_API_KEY`` etc.) come from ``.env`` — loaded at
import time via python-dotenv by ``backtest/__init__.py``. The username and
password arrive at runtime from the UI and are never stored: they live in
local variables for the duration of the ``login()`` call only.

All session state is in-memory only, per the PRD. The raw session token never
appears in any return value of the contract methods.
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
)

__all__ = ["MStockBroker"]

logger = logging.getLogger("backtest.brokers.mstock")

# mStock TypeA endpoints (same contract as backtest.live.auth).
_LOGIN_PATH = "/openapi/typea/connect/login"
_VERIFY_TOTP_PATH = "/openapi/typea/session/verifytotp"

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


class MStockBroker(BrokerAuthBase):
    """mStock implementation of the two-step broker auth contract.

    State held in-memory only; lost on restart by design (this epic). The
    temp auth context links a successful ``login()`` to the subsequent
    ``verify_totp()`` call and holds *server-returned* data only — never the
    username or password.
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
