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

from flask import Blueprint, jsonify, request

from backtest.brokers.session_manager import (
    available_brokers,
    get_session_manager,
)
from backtest.logging_config import get_logger

__all__ = ["broker_auth_bp"]

logger = get_logger(__name__)

broker_auth_bp = Blueprint("broker_auth_api", __name__)

_GENERIC_ERROR_MESSAGE = "Internal server error"


def _mask(value: str) -> str:
    """`trader@x.com` → `tra…com` — enough to correlate a session, no PII dump."""
    if not value:
        return "-"
    return value if len(value) <= 6 else f"{value[:3]}…{value[-3:]}"


def _string_field(data: dict, key: str) -> str | None:
    """Return a non-empty string field from a JSON body, else ``None``."""
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@broker_auth_bp.get("/api/broker/list")
def list_brokers() -> tuple:
    """Brokers the login UI can offer (broker selector in the auth modal)."""
    return jsonify({"success": True, "brokers": available_brokers()}), 200


@broker_auth_bp.post("/api/broker/select")
def select_broker() -> tuple:
    """Switch the active broker (one live session at a time).

    Body: ``{"broker": "mstock" | "dhan" | ...}``. Switching away from a
    broker with a live session drops that session (the Forward Engine
    consumes a single token).
    """
    data = request.get_json(silent=True) or {}
    broker_name = _string_field(data, "broker")
    if broker_name is None:
        return jsonify({"success": False, "message": "broker is required"}), 400
    try:
        result = get_session_manager().switch_broker(broker_name)
        status_code = 200 if result.get("success") else 400
        logger.info("broker select → %s", result.get("broker") or result.get("message"))
        return jsonify(result), status_code
    except Exception:  # noqa: BLE001 — generic message to browser, detail to log
        logger.exception("broker select endpoint failed")
        return (jsonify({"success": False, "message": _GENERIC_ERROR_MESSAGE}), 500)


@broker_auth_bp.post("/api/broker/login")
def login() -> tuple:
    """Step 1 — credentials. The password is used once, then discarded.

    Body: ``{broker, username, password}`` where ``broker`` selects which
    broker's flow to use (defaults to the active broker when omitted).
    """
    data = request.get_json(silent=True) or {}
    broker_name = _string_field(data, "broker")
    username = _string_field(data, "username")
    password = data.get("password")
    password = password if isinstance(password, str) and password else None
    if username is None or password is None:
        # Only which *fields* were missing — never the values.
        logger.warning(
            "login rejected: missing %s",
            " and ".join(
                n for n, v in (("username", username), ("password", password)) if v is None
            ),
        )
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
        if broker_name is not None:
            switch = get_session_manager().switch_broker(broker_name)
            if not switch.get("success"):
                return (
                    jsonify(
                        {"success": False, "message": switch.get("message", "Unknown broker"),
                         "requires_totp": False}
                    ),
                    400,
                )
        # Credentials are passed as call arguments only — never stored or
        # logged anywhere past this line.
        result = get_session_manager().login(username, password)
        outcome = "accepted" if result.get("success") else "rejected"
        reason = result.get("message") or ("TOTP required" if result.get("requires_totp") else "ok")
        logger.info("login %s for user=%s → %s", outcome, _mask(username), reason)
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
        verified = bool(result.get("success"))
        detail = "" if verified or not result.get("message") else f" ({result['message']})"
        logger.info(
            "TOTP %s — session %s%s",
            "verified" if verified else "rejected",
            result.get("expires_at") or "-",
            detail,
        )
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
        payload = get_session_manager().get_status()
        # Remember-session-today (2026-09-24): expose the toggle state so the
        # auth modal's checkbox reflects the server's actual behaviour.
        try:
            from backtest.brokers.remember_session import get_toggle, has_saved_session

            payload["remember_session"] = {
                "enabled": get_toggle(),
                "has_saved": has_saved_session(),
            }
        except Exception:  # noqa: BLE001 — status must never fail on the extra key
            payload["remember_session"] = {"enabled": False, "has_saved": False}
        return jsonify(payload), 200
    except Exception:  # noqa: BLE001 — fail closed
        logger.exception("broker status endpoint failed")
        return (
            jsonify(
                {
                    "status": "unauthenticated",
                    "broker": "unknown",
                    "broker_display_name": "Unknown Broker",
                    "expires_at": None,
                    "remember_session": {"enabled": False, "has_saved": False},
                }
            ),
            200,
        )


@broker_auth_bp.post("/api/broker/remember-session")
def remember_session_toggle() -> tuple:
    """Set the "Remember session today" toggle (2026-09-24).

    Body: ``{"enabled": true|false}``. OFF deletes the saved session file
    immediately — the NEXT app session requires fresh login + TOTP (the
    currently-running session is not killed; Logout does that explicitly).
    """
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({"success": False, "error": "'enabled' (bool) is required"}), 400
    try:
        from backtest.brokers.remember_session import set_toggle

        result = set_toggle(bool(data["enabled"]))
        logger.info("remember-session toggle → %s", result["remember"])
        return jsonify({"success": True, **result}), 200
    except Exception:  # noqa: BLE001
        logger.exception("remember-session toggle failed")
        return jsonify({"success": False, "error": "toggle failed"}), 500


@broker_auth_bp.get("/api/broker/probe-historical")
def probe_historical() -> tuple:
    """TEMP DEBUG (2026-09-18): probe the TypeA historical endpoint with the
    live session token for a set of candidate index tokens. Remove after the
    NIFTY/BANKNIFTY token convention is confirmed."""
    try:
        import requests
        import os

        from backtest.brokers.session_manager import get_session_manager
        from backtest.data.mstock_live_feed import _typea_headers

        token = get_session_manager().get_active_session_token()
        if not token:
            return jsonify({"success": False, "error": "no active session"}), 200
        api_key = os.getenv("MSTOCK_API_KEY", "")
        headers = _typea_headers(api_key, token)
        out = {}
        from flask import request as _req

        seg = _req.args.get("seg", "NSE")
        tok = _req.args.get("tok", "2885")
        interval = _req.args.get("interval", "minute")
        frm = _req.args.get("from", "2026-09-18 09:15:00")
        to = _req.args.get("to", "2026-09-18 10:30:00")
        try:
            url = (
                f"https://api.mstock.trade/openapi/typea/instruments/"
                f"historical/{seg}/{tok}/{interval}"
            )
            r = requests.get(
                url,
                headers=headers,
                params={"from": frm, "to": to},
                timeout=15,
            )
            info: dict = {"status": r.status_code}
            try:
                candles = (r.json().get("data") or {}).get("candles")
                if candles is None:
                    info["candles"] = None
                else:
                    info["count"] = len(candles)
                    info["last"] = candles[-1]
            except Exception:  # noqa: BLE001
                info["body"] = r.text[:200]
            out[f"{seg}/{tok}/{interval}"] = info
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
        return jsonify({"success": True, "probes": out}), 200
    except Exception:  # noqa: BLE001
        logger.exception("probe-historical failed")
        return jsonify({"success": False, "error": "probe failed"}), 500


@broker_auth_bp.get("/api/broker/session-token")
def session_token_bridge() -> tuple:
    """LOCALHOST-ONLY bridge: hand the live session token to first-party
    background scripts on the same machine (chain-snapshot recorder, bar
    fetcher). The script processes can't see the web app's in-memory session
    and the persisted token file goes stale after every re-login — the
    2026-09-23 chain-capture failure. Bound-check: refuse anything that is
    not a loopback request (defence in depth — the dev server binds
    127.0.0.1 anyway).
    """
    from flask import request as _req

    remote = str(_req.remote_addr or "")
    if remote not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"success": False, "error": "local only"}), 403
    token = get_session_manager().get_active_session_token()
    if not token:
        return jsonify({"success": False, "error": "no active session"}), 404
    return jsonify({"token": token}), 200


@broker_auth_bp.get("/api/broker/probe-quote")
def probe_quote() -> tuple:
    """TEMP DEBUG (2026-09-18): probe the quote/ohlc + quote/ltp endpoints
    with the live session to discover today's-session response shape.
    Remove after the live quote bar path is wired."""
    try:
        import requests
        import os

        from backtest.brokers.session_manager import get_session_manager
        from backtest.data.mstock_live_feed import _typea_headers
        from flask import request as _req

        token = get_session_manager().get_active_session_token()
        if not token:
            return jsonify({"success": False, "error": "no active session"}), 200
        api_key = os.getenv("MSTOCK_API_KEY", "")
        headers = _typea_headers(api_key, token)
        route = _req.args.get("route", "ohlc")
        sym = _req.args.get("sym", "NSE:RELIANCE")
        if route == "chain":
            # Fetch scriptmaster server-side and dump a real NIFTY option row
            r = requests.get(
                "https://api.mstock.trade/openapi/typea/instruments/scriptmaster",
                headers=headers,
                timeout=30,
            )
            import io
            import pandas as pd

            frame = pd.read_csv(io.StringIO(r.text), low_memory=False)
            frame.columns = [c.strip().lower() for c in frame.columns]
            ts_col = "tradingsymbol" if "tradingsymbol" in frame.columns else "trading_symbol"
            sym_up = frame[ts_col].astype(str).str.upper()
            opt_mask = sym_up.str.match(r"^NIFTY\d{2}[A-Z]{3}\d+(CE|PE)$")
            sample = frame[opt_mask].head(3)
            return jsonify(
                {
                    "success": True,
                    "columns": list(frame.columns),
                    "segment_values": sorted(
                        frame.loc[opt_mask, "segment"].dropna().unique().tolist()
                    )[:8],
                    "instrument_type_values": sorted(
                        frame.loc[opt_mask, "instrument_type"].dropna().unique().tolist()
                    )[:8],
                    "sample_rows": sample.to_dict(orient="records"),
                    "nifty_option_count": int(opt_mask.sum()),
                }
            ), 200
        path = (
            "openapi/typea/instruments/quote/ohlc"
            if route == "ohlc"
            else "openapi/typea/instruments/quote/ltp"
        )
        try:
            r = requests.get(
                f"https://api.mstock.trade/{path}",
                headers=headers,
                params=[("i", sym)],
                timeout=15,
            )
            return jsonify({"success": True, "status": r.status_code, "body": r.text[:800]}), 200
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "error": str(exc)}), 200
    except Exception:  # noqa: BLE001
        logger.exception("probe-quote failed")
        return jsonify({"success": False, "error": "probe failed"}), 500


@broker_auth_bp.post("/api/broker/logout")
def logout() -> tuple:
    """Clear the active session and all notification state."""
    try:
        get_session_manager().logout()
        logger.info("broker session logged out")
    except Exception:  # noqa: BLE001
        logger.exception("broker logout endpoint failed")
        return jsonify({"success": False, "message": _GENERIC_ERROR_MESSAGE}), 500
    return jsonify({"success": True}), 200


@broker_auth_bp.get("/api/broker/feed-quality")
def feed_quality() -> tuple:
    """Feed-quality comparison — which broker delivers fast, reliable data.

    ``?live=1``  → in-memory hot tails (this process only, fast).
    Default      → full report recomputed from the durable JSONL log
                   (``data/feed_quality.log``, restart-proof, all brokers).
    """
    try:
        from backtest.forward.feed_quality import aggregate_report, all_quality_monitors

        if request.args.get("live"):
            feeds = [m.summary() for m in all_quality_monitors()]
        else:
            feeds = aggregate_report()["feeds"]
        return jsonify({"success": True, "feeds": feeds}), 200
    except Exception:  # noqa: BLE001
        logger.exception("feed-quality endpoint failed")
        return jsonify({"success": False, "message": _GENERIC_ERROR_MESSAGE}), 500
