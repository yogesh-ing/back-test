from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Strategy

_REGISTRY: dict[str, type["Strategy"]] = {}


def register(cls: type["Strategy"]) -> type["Strategy"]:
    if not getattr(cls, "name", ""):
        raise ValueError("strategy name required")
    if cls.name in _REGISTRY:
        raise ValueError(f"duplicate strategy name: {cls.name}")
    _REGISTRY[cls.name] = cls
    return cls


def _discover() -> None:
    import backtest.strategies  # noqa: F401

    pkg_path = backtest.strategies.__path__
    seen = set()
    for _, modname, _ in pkgutil.iter_modules(pkg_path):
        full_name = f"backtest.strategies.{modname}"
        if full_name in seen:
            continue
        seen.add(full_name)
        importlib.import_module(full_name)


def list_strategies() -> list[str]:
    _discover()
    return sorted(_REGISTRY)


def get_strategy(name: str) -> type[Strategy]:
    _discover()
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown strategy: {name}. Available: {available}")
    return _REGISTRY[name]
