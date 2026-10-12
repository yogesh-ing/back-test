"""Refuse to run on a disabled data source.

Flask-aware half of :mod:`backtest.data.sources_policy`. The policy decides;
this module enforces it at the request boundary, so a disabled source produces
a 409 and a sentence explaining what to do — not a run that looks real.

    from backtest.api.data_guard import guard_source, data_source_status

Guards are applied only to routes that actually consume candles. Reading run
history or listing strategies is unaffected: refusing those would be a broken
app, not a safe one.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from flask import current_app, jsonify

from backtest.data.sources_policy import SourcePolicy, source_policy

__all__ = ["guard_source", "data_source_status", "active_policy"]


def active_policy() -> SourcePolicy:
    """The policy for this app instance.

    Resolved into ``app.config`` at startup so a test (or a deployment that
    reads the config once) can substitute one without touching the module-level
    cache.
    """
    try:
        configured = current_app.config.get("DATA_SOURCE_POLICY")
    except RuntimeError:  # outside an app context
        return source_policy()
    return configured if isinstance(configured, SourcePolicy) else source_policy()


def data_source_status() -> dict[str, Any]:
    """The whole picture, for the UI to render.

    ``active`` is the source the app RESOLVED to run on (see
    ``app.resolve_source``): a disabled configured source falls back to the
    best enabled one, so the ordinary deployment — synthetic disabled on
    purpose, real sources enabled — shows a green "Data source: Real Data
    (PostgreSQL)" badge instead of a ⛔ banner blocking both tabs. The
    original request is kept under ``requested`` for provenance.
    """
    policy = active_policy()
    name = _resolved_name()
    requested = _requested_name()
    status = {
        "active": name,
        "requested": requested,
        "fell_back": bool(requested) and requested != name,
        "allowed": policy.is_enabled(name),
        "certifiable": policy.is_certifiable(name),
        "refusal": policy.refusal_for(name),
        "sources": policy.describe(),
    }
    if status["fell_back"]:
        status["fallback_note"] = (
            f"Requested source '{requested}' is disabled — running on '{name}' instead."
        )
    return status


def _resolved_name() -> str:
    """The source this app instance actually runs on (post-fallback).

    There is no default. An instance that never resolved a source has none to
    guard, and ``refusal_for("")`` reads that as unknown — the app refuses
    rather than inventing a source to run on.
    """
    try:
        return str(current_app.config.get("BACKTEST_SOURCE") or "")
    except RuntimeError:  # outside an app context
        return ""


def _requested_name() -> str:
    """What the deployment asked for, before any fallback (for provenance)."""
    try:
        return str(
            current_app.config.get("BACKTEST_SOURCE_REQUESTED")
            or current_app.config.get("BACKTEST_SOURCE")
            or ""
        )
    except RuntimeError:
        return ""


def guard_source() -> Optional[Tuple[Any, int]]:
    """Return a refusal response if the resolved source is not allowed.

    With the fallback in place this only fires when NO enabled source exists
    at all (``resolve_source`` kept the requested name as a last resort) —
    the app refuses to run rather than pretending a source is fine.
    """
    refusal = active_policy().refusal_for(_resolved_name())
    if not refusal:
        return None
    return (
        jsonify(
            {
                "error": refusal,
                "data_source": data_source_status(),
                "code": "data_source_disabled",
            }
        ),
        409,
    )
