"""Broker authentication endpoints (mStock Authentication UI epic, Task 2.1).

* ``POST /api/broker/login``       body ``{username, password}``
* ``POST /api/broker/verify-totp`` body ``{totp_code}``
* ``GET  /api/broker/status``
* ``POST /api/broker/logout``

Every route delegates to the ``BrokerSessionManager`` singleton — no route
imports a concrete broker class or touches a raw session token. Security
rules from the PRD:

* the password is used for the login call and immediately discarded —
  never stored, logged, or echoed in any response;
* the session token never appears in any response payload;
* unexpected failures return generic messages — stack traces go to the
  server log only;
* ``GET /status`` fails closed: on internal error it reports
  ``unauthenticated`` so the Forward Test start button stays disabled.

Flow-level failures (wrong credentials, bad TOTP) are ``200`` with
``success: false`` — the UI shows the message inline. Malformed request
bodies are ``400``; unexpected server errors are ``500``.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from backtest.brokers.session_manager import get_session_manager

__all__ = ["broker_auth_bp"]

logger = logging.getLogger("backtest.api.broker_auth")

broker_auth_bp = Blueprint("broker_auth_api", __name__)

_GENERIC_ERROR_MESSAGE = "Internal server error"


def _string_field(data: dict, key: str) -> str | None:
    """Return a non-empty string field from a JSON body, else ``None``."""
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@broker_auth_bp.post("/api/broker/login")
def login() -> tuple:
    """Step 1 — credentials. The password is used once, then discarded."""
    data = request.get_json(silent=True) or {}
    username = _string_field(data, "username")
    password = data.get("password")
    password = password if isinstance(password, str) and password else None
    if username is None or password is None:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "Username and password are required",
                    "requires_totp": False,
                }
            ),
            400,
        )

    try:
        # Credentials are passed as call arguments only — never stored or
        # logged anywhere past this line.
        result = get_session_manager().login(username, password)
    except Exception:  # noqa: BLE001 — generic message to browser, detail to log
        logger.exception("broker login endpoint failed")
        return (
            jsonify({"success": False, "message": _GENERIC_ERROR_MESSAGE, "requires_totp": False}),
            500,
        )
    return jsonify(result), 200


@broker_auth_bp.post("/api/broker/verify-totp")
def verify_totp() -> tuple:
    """Step 2 — TOTP finalization (only valid after a successful login)."""
    data = request.get_json(silent=True) or {}
    code = _string_field(data, "totp_code")
    if code is None:
        return (
            jsonify({"success": False, "message": "totp_code is required", "expires_at": ""}),
            400,
        )

    try:
        result = get_session_manager().verify_totp(code)
    except Exception:  # noqa: BLE001
        logger.exception("broker TOTP verification endpoint failed")
        return (
            jsonify({"success": False, "message": _GENERIC_ERROR_MESSAGE, "expires_at": ""}),
            500,
        )
    return jsonify(result), 200


@broker_auth_bp.get("/api/broker/status")
def status() -> tuple:
    """Session status for nav-icon polling. Never includes the token."""
    try:
        return jsonify(get_session_manager().get_status()), 200
    except Exception:  # noqa: BLE001 — fail closed
        logger.exception("broker status endpoint failed")
        return (
            jsonify(
                {
                    "status": "unauthenticated",
                    "broker": "unknown",
                    "broker_display_name": "Unknown Broker",
                    "expires_at": None,
                }
            ),
            200,
        )


@broker_auth_bp.post("/api/broker/logout")
def logout() -> tuple:
    """Clear the active session and all notification state."""
    try:
        get_session_manager().logout()
    except Exception:  # noqa: BLE001
        logger.exception("broker logout endpoint failed")
        return jsonify({"success": False, "message": _GENERIC_ERROR_MESSAGE}), 500
    return jsonify({"success": True}), 200
