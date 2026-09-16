"""Playbook entity — declarative, reusable option strategy config.

The runner-spawn config (instrument.expression) already IS a playbook;
it just can't be saved, named, or reused. This module makes it a first-class
entity: Playbook = declarative config (structure, strikes policy, exits, sizing).

Portfolio spawns Runners *from* Playbooks. One concept, embedded, no new page.

Usage::

    from backtest.options.playbook import Playbook, PlaybookRegistry

    pb = Playbook(
        name="NIFTY ATM Bull Spread",
        underlying="NIFTY",
        structure_type={"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
        strike_selection="atm",
        quantity=1,
        exit_config={"stop_loss_pct": 0.5, "take_profit_pct": 1.0, "min_days_to_expiry": 1},
    )

    # Spawn a runner from it
    runner_config = pb.to_runner_config(
        strategy_name="directional_options",
        allocated_capital=100000,
        timeframe="1hour",
        mode="paper",
    )
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtest.options.playbook")

# ---------------------------------------------------------------------------
# Playbook
# ---------------------------------------------------------------------------

VALID_STRUCTURES = frozenset({
    "long_call",
    "long_put",
    "bull_call_spread",
    "bear_put_spread",
    "straddle",
    "strangle",
    "iron_condor",
    "calendar_spread",
    "direction_aware",  # maps BULLISH/BEARISH to structures
})

VALID_STRIKE_SELECTIONS = frozenset({"atm", "delta", "otm", "itm"})


@dataclass
class Playbook:
    """Declarative, reusable option strategy config.

    This is the **Playbook entity** — the missing piece that makes option
    trading plug-and-play. A Playbook is:

    * **Declarative**: structure, strikes policy, exits, sizing — no code
    * **Reusable**: saved, named, shared across runners
    * **Embedded**: lives inside Portfolio tab, not a separate page
    * **Strategy-agnostic**: any strategy that emits MarketView can use it

    The execution engine (OptionsBridge) owns *how* to execute;
    the Playbook owns *what* to execute.

    Data-ownership rule (C2): strategies never call broker/quote APIs.
    All bars + chain snapshots flow engine → strategy. Playbook is data,
    not code — it never fetches market data itself.

    Parameters
    ----------
    name:
        Human-readable name (e.g. "NIFTY ATM Bull Spread")
    underlying:
        Index (NIFTY, BANKNIFTY, etc.) — option-only V1
    structure_type:
        Either a single structure name, or a dict mapping direction to structure
        (e.g. {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"})
        or "direction_aware" for the default mapping.
    strike_selection:
        How to pick strikes: atm, delta, otm, itm
    delta_target:
        Target delta when strike_selection is delta (0.05-0.95)
    quantity:
        Lots per leg
    exit_config:
        Exit rules (stop_loss_pct, take_profit_pct, neutral_bars, max_bars,
        min_days_to_expiry, signal_flip, reenter, etc.) — default reenter False
        is a churn guard (same-bar re-enter churned -₹41,844 on 2026-09-16).
    max_loss_per_trade:
        Max loss in ₹ per signal (risk envelope — normalized across equity/options)
        — per-SIGNAL envelope, not per-day (per-day is daily_loss_limit breaker).
    description:
        Free-text description
    tags:
        Tags for filtering (e.g. ["conservative", "intraday"])
    version:
        Integer version, auto-bumps on PUT — runner snapshots expression at spawn,
        editing playbook never mutates running runner.
    """

    name: str
    underlying: str = "NIFTY"
    structure_type: Any = field(default_factory=lambda: {
        "BULLISH": "bull_call_spread",
        "BEARISH": "bear_put_spread",
    })
    strike_selection: str = "atm"
    delta_target: float = 0.35
    quantity: int = 1
    exit_config: Dict[str, Any] = field(default_factory=lambda: {
        "signal_flip": True,
        "stop_loss_pct": 0.5,
        "take_profit_pct": 1.0,
        "min_days_to_expiry": 1,
        "reenter": False,
    })
    max_loss_per_trade: Optional[float] = None
    description: str = ""
    tags: List[str] = field(default_factory=list)
    playbook_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("playbook name required")
        self.underlying = str(self.underlying).upper()
        self.strike_selection = str(self.strike_selection).lower()
        if self.strike_selection not in VALID_STRIKE_SELECTIONS:
            raise ValueError(
                f"strike_selection must be one of {sorted(VALID_STRIKE_SELECTIONS)}, "
                f"got {self.strike_selection!r}"
            )
        if not 0.05 <= self.delta_target <= 0.95:
            raise ValueError(f"delta_target must be in [0.05, 0.95], got {self.delta_target}")
        if self.quantity < 1:
            raise ValueError(f"quantity must be >= 1, got {self.quantity}")
        # C1: ensure mutable defaults are not shared — default_factory already guarantees,
        # but we defensively copy if caller passed a list/dict literal that might be shared
        # (regression test C1 checks two playbooks don't share a list).
        if self.tags is None:
            self.tags = []
        # Ensure version is int (final spec)
        if isinstance(self.version, str):
            try:
                self.version = int(float(self.version))
            except Exception:
                self.version = 1
        # Timestamps — set at creation (user action, not engine path)
        now = datetime.now(timezone.utc).isoformat()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = self.created_at
        # Validate structure_type
        if isinstance(self.structure_type, dict):
            for direction, struct in self.structure_type.items():
                if struct not in VALID_STRUCTURES and struct not in (
                    "bull_call_spread", "bear_put_spread", "long_call", "long_put"
                ):
                    # Allow any string for forward compat, but warn
                    logger.warning("Playbook %r: unknown structure %r for %s", self.name, struct, direction)
        elif isinstance(self.structure_type, str):
            if self.structure_type not in VALID_STRUCTURES:
                logger.warning("Playbook %r: unknown structure_type %r", self.name, self.structure_type)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for storage/API."""
        return {
            "playbook_id": self.playbook_id,
            "name": self.name,
            "underlying": self.underlying,
            "structure_type": self.structure_type,
            "strike_selection": self.strike_selection,
            "delta_target": self.delta_target,
            "quantity": self.quantity,
            "exit_config": dict(self.exit_config),
            "max_loss_per_trade": self.max_loss_per_trade,
            "description": self.description,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": int(self.version),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Playbook":
        """Deserialise from dict."""
        return cls(
            playbook_id=data.get("playbook_id") or uuid.uuid4().hex[:12],
            name=data["name"],
            underlying=data.get("underlying", "NIFTY"),
            structure_type=data.get("structure_type", {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}),
            strike_selection=data.get("strike_selection", "atm"),
            delta_target=float(data.get("delta_target", 0.35)),
            quantity=int(data.get("quantity", 1)),
            exit_config=dict(data.get("exit_config") or {"signal_flip": True, "stop_loss_pct": 0.5, "take_profit_pct": 1.0, "min_days_to_expiry": 1, "reenter": False}),
            max_loss_per_trade=data.get("max_loss_per_trade"),
            description=data.get("description", ""),
            tags=list(data.get("tags") or []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            version=int(data.get("version", 1)),
        )

    def to_expression(self) -> Dict[str, Any]:
        """Convert to the RunnerConfig instrument.expression format.

        This is the bridge between Playbook (UI concept) and OptionsBridge
        (execution concept). The runner's expression layer consumes this directly.
        """
        expr: Dict[str, Any] = {}
        # Structure type
        if isinstance(self.structure_type, str) and self.structure_type == "direction_aware":
            expr["type"] = {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}
        else:
            expr["type"] = self.structure_type

        expr["strike_selection"] = self.strike_selection
        if self.strike_selection == "delta":
            expr["delta_target"] = self.delta_target
        expr["quantity"] = self.quantity
        expr["exit"] = dict(self.exit_config)

        # Risk envelope — passed through for the execution engine to enforce
        if self.max_loss_per_trade is not None:
            expr["max_loss_per_trade"] = self.max_loss_per_trade

        return expr

    def to_runner_config(
        self,
        strategy_name: str,
        allocated_capital: float,
        timeframe: str = "1hour",
        mode: str = "paper",
        source: str = "synthetic",
        symbols: Optional[List[str]] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a RunnerConfig-compatible dict from this playbook.

        Returns a dict suitable for POST /api/portfolio/runner/create
        with instrument.type == "option".

        Parameters
        ----------
        strategy_name:
            Strategy that emits MarketView (e.g. "directional_options")
        allocated_capital:
            Capital for the runner
        timeframe, mode, source:
            Standard runner fields
        symbols:
            Override symbols (default: [underlying])
        name:
            Override runner name (default: playbook name)
        """
        return {
            "name": name or self.name,
            "strategy": strategy_name,
            "allocated_capital": allocated_capital,
            "target_type": "SINGLE_SYMBOL",
            "symbol": (symbols[0] if symbols else self.underlying),
            "symbols": symbols or [self.underlying],
            "timeframe": timeframe,
            "mode": mode,
            "source": source,
            "instrument": {
                "type": "option",
                "expression": self.to_expression(),
            },
            "params": {},  # strategy params — caller can override
        }

    def risk_envelope(self, spot_price: float, lot_size: int = 50) -> Dict[str, Any]:
        """Calculate the risk envelope for this playbook.

        Returns max loss per signal in ₹, normalized across instrument types.
        For options: premium × lots × lot_size (with SPAN margin estimate).
        For equity: stop distance × quantity (handled by equity sizer).

        This is the instrument-agnostic risk normalization the consultant
        identified as missing.

        C4: output carries an `estimated: true` flag, rendered in UI next to ₹.
        V1 uses 2%/1%/4% moneyness model, capped by max_loss_per_trade.
        Lot size is resolved from instrument master at call time — never stored
        in playbook (NSE revises lot sizes).

        Parameters
        ----------
        spot_price:
            Current underlying spot
        lot_size:
            Lot size for the underlying (from instrument master, not playbook)
        """
        # For V1: estimate premium as ~2-5% of spot for ATM, less for OTM
        # In production, this would use Black-Scholes with IV.
        # For now, conservative estimate: 2% of spot per lot as max loss proxy.
        estimated_premium_pct = 0.02  # 2% for ATM
        if self.strike_selection == "otm":
            estimated_premium_pct = 0.01
        elif self.strike_selection == "itm":
            estimated_premium_pct = 0.04

        estimated_premium = spot_price * estimated_premium_pct
        max_loss = estimated_premium * self.quantity * lot_size

        # If max_loss_per_trade is set, cap it
        if self.max_loss_per_trade is not None:
            max_loss = min(max_loss, self.max_loss_per_trade)

        return {
            "estimated_premium_per_unit": round(estimated_premium, 2),
            "max_loss_per_signal": round(max_loss, 2),
            "quantity": self.quantity,
            "lot_size": lot_size,
            "underlying": self.underlying,
            "spot": spot_price,
            "estimated": True,  # C4: every UI surface labels it "estimated"
        }


# ---------------------------------------------------------------------------
# Registry — in-memory V1, file-backed V2
# ---------------------------------------------------------------------------

class PlaybookRegistry:
    """Registry of playbooks — in-memory with optional file persistence.

    V1: in-memory only (matching portfolio manager's V1 scope).
    V2: optional JSON file persistence.

    This is the **Playbook entity** storage — the thing that makes
    runner-spawn configs reusable.
    """

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self._playbooks: Dict[str, Playbook] = {}
        self._storage_path = Path(storage_path) if storage_path else None
        # Load defaults
        self._load_defaults()
        # Try to load from file
        if self._storage_path and self._storage_path.exists():
            self._load_from_file()

    def _load_defaults(self) -> None:
        """Load built-in playbooks."""
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
        for pb in defaults:
            self._playbooks[pb.playbook_id] = pb

    def _load_from_file(self) -> None:
        try:
            data = json.loads(self._storage_path.read_text())
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
            data = [pb.to_dict() for pb in self._playbooks.values()]
            self._storage_path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Failed to save playbooks to %s: %s", self._storage_path, exc)

    def list(self, tag: Optional[str] = None, underlying: Optional[str] = None) -> List[Playbook]:
        """List playbooks, optionally filtered."""
        result = list(self._playbooks.values())
        if tag:
            result = [pb for pb in result if tag.lower() in [t.lower() for t in pb.tags]]
        if underlying:
            result = [pb for pb in result if pb.underlying.upper() == underlying.upper()]
        return sorted(result, key=lambda pb: pb.name)

    def get(self, playbook_id: str) -> Optional[Playbook]:
        return self._playbooks.get(playbook_id)

    def save(self, playbook: Playbook) -> Playbook:
        """Save/update a playbook."""
        self._playbooks[playbook.playbook_id] = playbook
        self._save_to_file()
        logger.info("Saved playbook %r (%s)", playbook.name, playbook.playbook_id)
        return playbook

    def delete(self, playbook_id: str) -> bool:
        """Delete a playbook. Returns True if existed."""
        if playbook_id in self._playbooks:
            # Don't delete built-ins
            if playbook_id.startswith("pb_default_"):
                raise ValueError(f"Cannot delete built-in playbook {playbook_id}")
            del self._playbooks[playbook_id]
            self._save_to_file()
            logger.info("Deleted playbook %s", playbook_id)
            return True
        return False

    def to_api_list(self) -> List[Dict[str, Any]]:
        """API-ready list."""
        return [pb.to_dict() for pb in self.list()]


# Process-wide singleton
_REGISTRY: Optional[PlaybookRegistry] = None


def get_playbook_registry(storage_path: Optional[Path] = None) -> PlaybookRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PlaybookRegistry(storage_path=storage_path)
    return _REGISTRY


def reset_playbook_registry(storage_path: Optional[Path] = None) -> PlaybookRegistry:
    global _REGISTRY
    _REGISTRY = PlaybookRegistry(storage_path=storage_path)
    return _REGISTRY


# Convenience wrappers used by API layer and tests

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
