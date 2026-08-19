"""mStock authentication (TOTP/OTP)."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from base64 import b32decode
from typing import Any

import requests


def generate_totp_code(secret: str | None = None, now: int | None = None) -> str:
    """Generate the current TOTP value from the configured base32 secret."""
    secret_value = (secret or os.getenv("MSTOCK_CHECKSUM", "")).strip()
    if not secret_value:
        raise ValueError("MSTOCK_CHECKSUM not set")

    try:
        key = b32decode(secret_value, casefold=True)
    except Exception as exc:
        raise ValueError(f"MSTOCK_CHECKSUM not valid base32: {exc}") from exc

    current = int(time.time()) if now is None else int(now)
    window = current // 30
    hmac_hash = hmac.new(key, window.to_bytes(8, byteorder="big"), hashlib.sha1).digest()
    offset_val = hmac_hash[-1] & 0x0f
    otp = int.from_bytes(hmac_hash[offset_val:offset_val + 4], byteorder="big") & 0x7fffffff
    return str(otp % 1_000_000).zfill(6)


def get_auth_code(explicit_code: str | None = None) -> str:
    """Get a one-time code from the caller or user prompt. TOTP is user-entered, not generated."""
    if explicit_code:
        return explicit_code

    mode = os.getenv("MSTOCK_AUTH_MODE", "otp").lower()
    if mode == "totp":
        env_code = os.getenv("MSTOCK_TOTP", "").strip()
        if env_code:
            return env_code

        raw = input("Enter TOTP from your authenticator app: ").strip()
        if not raw:
            raise ValueError("TOTP is required")
        return raw

    return os.getenv("MSTOCK_OTP", "").strip()


def login() -> dict[str, Any]:
    """Login with username/password using the real mStock TypeA connect API."""
    base_url = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")
    username = os.getenv("MSTOCK_USERNAME", "")
    password = os.getenv("MSTOCK_PASSWORD", "")

    if not username or not password:
        raise ValueError("MSTOCK_USERNAME and MSTOCK_PASSWORD required")

    payload = {"Username": username, "Password": password}
    headers = {
        "X-Mirae-Version": "1",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = requests.post(
            f"{base_url}/openapi/typea/connect/login",
            data=payload,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise ValueError(f"Login failed: {e}")


def verify_totp(code: str) -> dict[str, Any]:
    """Verify a user-entered TOTP against the mStock API and return the access token."""
    base_url = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")
    api_key = os.getenv("MSTOCK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("MSTOCK_API_KEY required")

    payload = {"api_key": api_key, "totp": code}
    headers = {
        "X-Mirae-Version": "1",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = requests.post(
            f"{base_url}/openapi/typea/session/verifytotp",
            data=payload,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token") or (data.get("data", {}) or {}).get("access_token")
        if not token:
            raise ValueError("verify_totp response did not include access_token")
        return {"token": token, "code": code}
    except requests.RequestException as e:
        raise ValueError(f"TOTP verification failed: {e}")


def generate_session(code: str) -> dict[str, Any]:
    """Generate session via SMS OTP using the mStock API contract."""
    base_url = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")
    api_key = os.getenv("MSTOCK_API_KEY", "").strip()
    checksum = os.getenv("MSTOCK_CHECKSUM", "W").strip() or "W"

    if not api_key:
        raise ValueError("MSTOCK_API_KEY required")

    payload = {"api_key": api_key, "request_token": code, "checksum": checksum}
    headers = {
        "X-Mirae-Version": "1",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = requests.post(
            f"{base_url}/openapi/typea/session/token",
            data=payload,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token") or (data.get("data", {}) or {}).get("access_token")
        if not token:
            raise ValueError("generate_session response did not include access_token")
        return {"token": token, "code": code}
    except requests.RequestException as e:
        raise ValueError(f"OTP auth failed: {e}")


def get_session_token() -> str:
    """Get session token. If not cached, authenticate via TOTP/OTP."""
    cache_file = ".mstock_session_token"
    
    # Check cached token
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                token = f.read().strip()
            if token:
                return token
        except Exception:
            pass
    
    # Real mStock flow from the SDK example: login first, then ask the user for the
    # current TOTP/OTP, then verify or generate the session on the server.
    mode = os.getenv("MSTOCK_AUTH_MODE", "otp").lower()
    login_response = login()
    _ = login_response
    code = get_auth_code()

    if mode == "totp":
        result = verify_totp(code)
    else:
        result = generate_session(code)

    token = result.get("token", "")
    if token:
        with open(cache_file, "w") as f:
            f.write(token)
    
    return token
