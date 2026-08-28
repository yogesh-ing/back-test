from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import Strategy

from backtest.logging_config import get_logger

logger = get_logger(__name__)

_REGISTRY: dict[str, type["Strategy"]] = {}


def register(cls: type["Strategy"]) -> type["Strategy"]:
    if not getattr(cls, "name", ""):
        raise ValueError("strategy name required")
    if cls.name in _REGISTRY:
        raise ValueError(f"duplicate strategy name: {cls.name}")
    _REGISTRY[cls.name] = cls
    logger.info("registered strategy %r (%s)", cls.name, cls.__name__)
    return cls


def _discover() -> None:
    """Import every module under ``backtest.strategies``.

    A broken or invalid file logs a warning and is skipped — it never crashes
    the app (Task 1.2 acceptance). Validation-level errors (missing name, bad
    param schema) are caught later in :func:`get_all` via ``Strategy.validate``.
    """
    import backtest.strategies  # noqa: F401

    pkg_path = backtest.strategies.__path__
    logger.debug("discovery: scanning %s", list(pkg_path))
    seen: set[str] = set()
    for _, modname, _ in pkgutil.iter_modules(pkg_path):
        full_name = f"backtest.strategies.{modname}"
        if full_name in seen:
            continue
        seen.add(full_name)
        try:
            importlib.import_module(full_name)
        except Exception as exc:  # noqa: BLE001 — must not crash discovery
            logger.warning("Skipping strategy module %s: %s: %s",
                           full_name, exc.__class__.__name__, exc)
            logger.debug("Strategy module %s failed to import", full_name, exc_info=True)


def list_strategies() -> list[str]:
    _discover()
    return sorted(_REGISTRY)


def get_strategy(name: str) -> type[Strategy]:
    _discover()
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown strategy: {name}. Available: {available}")
    return _REGISTRY[name]


# ---------------------------------------------------------------------------
# Catalog API (Task 1.2) — powers GET /api/strategies and dynamic param forms
# ---------------------------------------------------------------------------


def get_all() -> list[dict[str, Any]]:
    """Full strategy catalogue as plain dicts.

    Each entry is ``{name, description, version, author, params}`` where
    ``params`` is the normalised schema. Invalid strategies are skipped with a
    logged warning rather than raising.
    """
    _discover()
    catalogue: list[dict[str, Any]] = []
    skipped = 0
    for name in sorted(_REGISTRY):
        cls = _REGISTRY[name]
        try:
            cls.validate()
        except Exception as exc:  # noqa: BLE001 — skip invalid, keep going
            skipped += 1
            logger.warning("Skipping invalid strategy %s: %s", name, exc)
            continue
        catalogue.append(
            {
                "name": cls.name,
                "description": getattr(cls, "description", "") or "",
                "version": getattr(cls, "version", "") or "",
                "author": getattr(cls, "author", "") or "",
                "params": cls.param_schema(),
            }
        )
    logger.info("catalogue: %d strategies (%s)%s", len(catalogue),
                ", ".join(c["name"] for c in catalogue) or "none",
                f", {skipped} skipped" if skipped else "")
    return catalogue


def get_params(name: str) -> dict[str, dict[str, Any]]:
    """Normalised param schema for a single strategy.

    Raises :class:`KeyError` (→ HTTP 404) when the strategy is unknown.
    """
    cls = get_strategy(name)  # raises KeyError if missing
    return cls.param_schema()
