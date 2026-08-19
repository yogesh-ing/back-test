"""Preflight health checks for live mStock connectivity."""

from __future__ import annotations

import os
import socket
from typing import Any

import requests


def check_dns(host: str) -> dict[str, Any]:
    """Check DNS resolution for a host."""
    try:
        ip = socket.gethostbyname(host)
        return {"ok": True, "host": host, "ip": ip}
    except socket.gaierror as e:
        return {"ok": False, "host": host, "error": f"DNS failed: {e}"}


def check_https(url: str) -> dict[str, Any]:
    """Check HTTPS reachability."""
    try:
        resp = requests.head(url, timeout=10, verify=True)
        return {"ok": resp.status_code < 400, "url": url, "status": resp.status_code}
    except requests.RequestException as e:
        return {"ok": False, "url": url, "error": f"HTTPS failed: {e}"}


def check_auth() -> dict[str, Any]:
    """Check auth env vars are set. TOTP is entered at runtime, not stored as env."""
    required = ["MSTOCK_API_KEY", "MSTOCK_USERNAME", "MSTOCK_PASSWORD"]
    mode = os.getenv("MSTOCK_AUTH_MODE", "otp").lower()

    if mode == "otp":
        required.append("MSTOCK_OTP")

    missing = [k for k in required if not os.getenv(k)]

    if missing:
        return {"ok": False, "auth_mode": mode, "error": f"missing env: {', '.join(missing)}"}

    return {"ok": True, "auth_mode": mode}


def run_preflight() -> dict[str, Any]:
    """Run all preflight checks."""
    base_url = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade")
    host = base_url.replace("https://", "").replace("http://", "").split("/")[0]
    
    results = {
        "dns": check_dns(host),
        "https": check_https(base_url),
        "auth": check_auth(),
    }
    
    all_ok = all(r.get("ok", False) for r in results.values())
    results["all_ok"] = all_ok
    
    return results


def print_preflight() -> int:
    """Run preflight and print results. Return exit code."""
    results = run_preflight()
    
    print("Preflight checks:")
    print(f"  DNS:    {'✓' if results['dns'].get('ok') else '✗'} {results['dns']}")
    print(f"  HTTPS:  {'✓' if results['https'].get('ok') else '✗'} {results['https']}")
    print(f"  Auth:   {'✓' if results['auth'].get('ok') else '✗'} {results['auth']}")
    
    if results["all_ok"]:
        print("All checks passed.")
        return 0
    else:
        print("Some checks failed. Fix network or env vars.")
        return 1
