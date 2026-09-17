"""Backward-compatible wrapper — canonical location is backtest.playbooks.models + registry.

This file re-exports Playbook and registry from the new canonical package
src/backtest/playbooks/ per ARCHITECTURE-UNIFIED-TRADING.md §2 / U1.1.

Old imports `from backtest.options.playbook import Playbook` continue to work.
New code should import from `backtest.playbooks.models` / `registry`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

# Canonical implementation
from backtest.playbooks.models import Playbook, VALID_STRIKE_SELECTIONS, VALID_STRUCTURES
from backtest.playbooks.registry import (
    PlaybookRegistry,
    get_playbook_registry,
    reset_playbook_registry,
)


# Convenience wrappers used by API layer and tests (kept for backward compat)
def list_playbooks(tag: Optional[str] = None, underlying: Optional[str] = None) -> List[Playbook]:
    return get_playbook_registry().list(tag=tag, underlying=underlying)


def get_playbook(playbook_id: str) -> Optional[Playbook]:
    return get_playbook_registry().get(playbook_id)


def create_playbook(data: Dict[str, Any]) -> Playbook:
    pb = Playbook.from_dict(data)
    return get_playbook_registry().save(pb)


def delete_playbook(playbook_id: str) -> bool:
    return get_playbook_registry().delete(playbook_id)


def seed_defaults(storage_path: Optional[Path] = None) -> PlaybookRegistry:
    return reset_playbook_registry(storage_path=storage_path)


def spawn_runner_config_from_playbook(
    playbook_id: str,
    strategy: str = "directional_options",
    allocated_capital: float = 100000,
    **overrides: Any,
) -> Dict[str, Any]:
    reg = get_playbook_registry()
    pb = reg.get(playbook_id)
    if pb is None:
        raise KeyError(f"playbook {playbook_id!r} not found")
    return pb.to_runner_config(
        strategy_name=strategy,
        allocated_capital=allocated_capital,
        **overrides,
    )


__all__ = [
    "Playbook",
    "PlaybookRegistry",
    "VALID_STRUCTURES",
    "VALID_STRIKE_SELECTIONS",
    "get_playbook_registry",
    "reset_playbook_registry",
    "list_playbooks",
    "get_playbook",
    "create_playbook",
    "delete_playbook",
    "seed_defaults",
    "spawn_runner_config_from_playbook",
]
