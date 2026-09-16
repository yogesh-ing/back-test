"""Playbook registry — thread-safe singleton, 3 seeded defaults, optional PLAYBOOKS_PATH JSON.

Delete blocks pb_default_* IDs. Mirrors the existing pattern in options/playbook.py
but is the canonical location per ARCHITECTURE-UNIFIED-TRADING.md §2 / UNIFIED-TRADING-TASKS.md U1.1.

U1.1 requires: thread-safe singleton _REGISTRY, 3 seeded defaults (bull call spread / bear put spread / long call),
optional PLAYBOOKS_PATH JSON load/save.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from backtest.playbooks.models import Playbook

logger = logging.getLogger("backtest.playbooks.registry")


class PlaybookRegistry:
    """Thread-safe registry — in-memory V1, file-backed V2 via PLAYBOOKS_PATH env."""

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._playbooks: Dict[str, Playbook] = {}
        self._storage_path = Path(storage_path) if storage_path else None
        self._load_defaults()
        if self._storage_path and self._storage_path.exists():
            self._load_from_file()

    def _load_defaults(self) -> None:
        defaults = [
            Playbook(
                name="NIFTY ATM Bull Spread (Conservative)",
                underlying="NIFTY",
                structure_type={"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
                strike_selection="atm",
                quantity=1,
                exit_config={
                    "signal_flip": True,
                    "stop_loss_pct": 0.5,
                    "take_profit_pct": 1.0,
                    "min_days_to_expiry": 1,
                    "reenter": False,
                },
                max_loss_per_trade=5000,
                description="Conservative ATM spreads — 50% stop, 100% target, square off 1d before expiry",
                tags=["conservative", "nifty", "spread"],
                playbook_id="pb_default_bull_spread",
            ),
            Playbook(
                name="NIFTY ATM Long Call/Put (Directional)",
                underlying="NIFTY",
                structure_type={"BULLISH": "long_call", "BEARISH": "long_put"},
                strike_selection="atm",
                quantity=1,
                exit_config={
                    "signal_flip": True,
                    "stop_loss_pct": 0.3,
                    "take_profit_pct": 1.5,
                    "min_days_to_expiry": 0,
                    "reenter": False,
                },
                max_loss_per_trade=3000,
                description="Directional long options — 30% stop, 150% target, ride to expiry",
                tags=["directional", "nifty", "long"],
                playbook_id="pb_default_long",
            ),
            Playbook(
                name="BANKNIFTY Delta 35 Spread",
                underlying="BANKNIFTY",
                structure_type={"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
                strike_selection="delta",
                delta_target=0.35,
                quantity=1,
                exit_config={
                    "signal_flip": True,
                    "neutral_bars": 3,
                    "stop_loss_pct": 0.5,
                    "take_profit_pct": 1.0,
                    "min_days_to_expiry": 1,
                    "reenter": False,
                },
                max_loss_per_trade=7000,
                description="Delta-based spreads for BANKNIFTY — exits after 3 flat bars",
                tags=["banknifty", "delta", "spread"],
                playbook_id="pb_default_banknifty_delta",
            ),
        ]
        with self._lock:
            for pb in defaults:
                self._playbooks[pb.playbook_id] = pb

    def _load_from_file(self) -> None:
        try:
            data = json.loads(self._storage_path.read_text())
            with self._lock:
                for item in data:
                    try:
                        pb = Playbook.from_dict(item)
                        self._playbooks[pb.playbook_id] = pb
                    except Exception as exc:
                        logger.warning("Skipping invalid playbook in file: %s", exc)
            logger.info("Loaded %d playbooks from %s", len(data), self._storage_path)
        except Exception as exc:
            logger.warning("Failed to load playbooks from %s: %s", self._storage_path, exc)

    def _save_to_file(self) -> None:
        if not self._storage_path:
            return
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = [pb.to_dict() for pb in self._playbooks.values()]
            self._storage_path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Failed to save playbooks to %s: %s", self._storage_path, exc)

    def list(self, tag: Optional[str] = None, underlying: Optional[str] = None) -> List[Playbook]:
        with self._lock:
            result = list(self._playbooks.values())
        if tag:
            result = [pb for pb in result if tag.lower() in [t.lower() for t in pb.tags]]
        if underlying:
            result = [pb for pb in result if pb.underlying.upper() == underlying.upper()]
        return sorted(result, key=lambda pb: pb.name)

    def get(self, playbook_id: str) -> Optional[Playbook]:
        with self._lock:
            return self._playbooks.get(playbook_id)

    def save(self, playbook: Playbook) -> Playbook:
        """Save/update — bumps version if existing."""
        with self._lock:
            existing = self._playbooks.get(playbook.playbook_id)
            if existing:
                # Auto-bump version on PUT per architecture §2
                playbook.version = int(existing.version) + 1
                playbook.created_at = existing.created_at
                from datetime import datetime, timezone

                playbook.updated_at = datetime.now(timezone.utc).isoformat()
            self._playbooks[playbook.playbook_id] = playbook
        self._save_to_file()
        logger.info("Saved playbook %r (%s) v%d", playbook.name, playbook.playbook_id, playbook.version)
        return playbook

    def delete(self, playbook_id: str) -> bool:
        with self._lock:
            if playbook_id in self._playbooks:
                if playbook_id.startswith("pb_default_"):
                    raise ValueError(f"Cannot delete built-in playbook {playbook_id}")
                del self._playbooks[playbook_id]
                self._save_to_file()
                logger.info("Deleted playbook %s", playbook_id)
                return True
        return False

    def to_api_list(self) -> List[Dict]:
        return [pb.to_dict() for pb in self.list()]


# Process-wide singleton
_REGISTRY: Optional[PlaybookRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def _resolve_storage_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """U1.4: Load registry from PLAYBOOKS_PATH at startup; skip silently when unset."""
    import os

    if explicit is not None:
        return Path(explicit)
    env_path = os.getenv("PLAYBOOKS_PATH")
    if env_path:
        return Path(env_path)
    return None


def get_playbook_registry(storage_path: Optional[Path] = None) -> PlaybookRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            resolved = _resolve_storage_path(storage_path)
            _REGISTRY = PlaybookRegistry(storage_path=resolved)
        return _REGISTRY


def reset_playbook_registry(storage_path: Optional[Path] = None) -> PlaybookRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        resolved = _resolve_storage_path(storage_path)
        _REGISTRY = PlaybookRegistry(storage_path=resolved)
        return _REGISTRY
