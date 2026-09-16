"""Playbook dataclass — final spec per ARCHITECTURE-UNIFIED-TRADING.md §2

Option-only V1, version: int, max_loss_per_trade, no lot_size stored.
Three methods: to_expression(), to_runner_config(), risk_envelope() with estimated:true
and 2%/1%/4% moneyness premium model, capped by max_loss_per_trade.

C1: tags: list = field(default_factory=list), exit_config: dict = field(default_factory=lambda: {...})
C4: risk_envelope() returns estimated: true
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtest.playbooks.models")

VALID_STRUCTURES = frozenset({
    "long_call",
    "long_put",
    "bull_call_spread",
    "bear_put_spread",
    "straddle",
    "strangle",
    "iron_condor",
    "calendar_spread",
    "direction_aware",
})

VALID_STRIKE_SELECTIONS = frozenset({"atm", "delta", "otm", "itm"})


@dataclass
class Playbook:
    """Declarative, reusable option strategy config — final spec.

    Data-ownership rule (C2): Playbook is data, not code — it never fetches
    market data itself. All bars + chain snapshots flow engine → strategy.

    Versioning & reproducibility (Q5): integer version bumps on every PUT.
    Runner snapshots to_expression() at spawn time; editing never mutates
    running runner. Snapshot is reproducibility guarantee — no version history
    table in V1.

    Scope (Q1): option-only V1. Equity playbooks V1.1 after ≥10 playbook-spawned runners.
    """

    playbook_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  # uuid4 at CREATION (user action, not engine path)
    name: str = ""
    underlying: str = "NIFTY"  # option-only V1
    structure_type: Any = field(
        default_factory=lambda: {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}
    )
    strike_selection: str = "atm"  # atm | delta | otm | itm
    delta_target: float = 0.35
    quantity: int = 1
    exit_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "signal_flip": True,
            "stop_loss_pct": 0.5,
            "take_profit_pct": 1.0,
            "min_days_to_expiry": 1,
            "reenter": False,  # default false — churn guard, -₹41,844 evidence
        }
    )
    max_loss_per_trade: Optional[float] = None  # per-SIGNAL ₹ envelope
    description: str = ""
    tags: List[str] = field(default_factory=list)
    version: int = 1  # auto-bump on PUT
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("playbook name required")
        self.underlying = str(self.underlying).upper()
        self.strike_selection = str(self.strike_selection).lower()
        if self.strike_selection not in VALID_STRIKE_SELECTIONS:
            raise ValueError(
                f"strike_selection must be one of {sorted(VALID_STRIKE_SELECTIONS)}, got {self.strike_selection!r}"
            )
        if not 0.05 <= self.delta_target <= 0.95:
            raise ValueError(f"delta_target must be in [0.05, 0.95], got {self.delta_target}")
        if self.quantity < 1:
            raise ValueError(f"quantity must be >= 1, got {self.quantity}")
        # C1: defensive — ensure mutable defaults not shared (field(default_factory) already guarantees)
        if self.tags is None:
            self.tags = []
        if isinstance(self.version, str):
            try:
                self.version = int(float(self.version))
            except Exception:
                self.version = 1
        now = datetime.now(timezone.utc).isoformat()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = self.created_at
        # Validate structure_type (warn, don't fail for forward compat)
        if isinstance(self.structure_type, dict):
            for direction, struct in self.structure_type.items():
                if struct not in VALID_STRUCTURES:
                    logger.warning("Playbook %r: unknown structure %r for %s", self.name, struct, direction)
        elif isinstance(self.structure_type, str):
            if self.structure_type not in VALID_STRUCTURES:
                logger.warning("Playbook %r: unknown structure_type %r", self.name, self.structure_type)

    def to_dict(self) -> Dict[str, Any]:
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
        return cls(
            playbook_id=data.get("playbook_id") or uuid.uuid4().hex[:12],
            name=data["name"],
            underlying=data.get("underlying", "NIFTY"),
            structure_type=data.get("structure_type", {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}),
            strike_selection=data.get("strike_selection", "atm"),
            delta_target=float(data.get("delta_target", 0.35)),
            quantity=int(data.get("quantity", 1)),
            exit_config=dict(
                data.get("exit_config")
                or {"signal_flip": True, "stop_loss_pct": 0.5, "take_profit_pct": 1.0, "min_days_to_expiry": 1, "reenter": False}
            ),
            max_loss_per_trade=data.get("max_loss_per_trade"),
            description=data.get("description", ""),
            tags=list(data.get("tags") or []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            version=int(data.get("version", 1)),
        )

    def to_expression(self) -> Dict[str, Any]:
        """The instrument.expression block for runner create — bridge to OptionsBridge."""
        expr: Dict[str, Any] = {}
        if isinstance(self.structure_type, str) and self.structure_type == "direction_aware":
            expr["type"] = {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}
        else:
            expr["type"] = self.structure_type
        expr["strike_selection"] = self.strike_selection
        if self.strike_selection == "delta":
            expr["delta_target"] = self.delta_target
        expr["quantity"] = self.quantity
        expr["exit"] = dict(self.exit_config)
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
        """Full spawn payload — POST /api/portfolio/runner/create accepts this."""
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
            "params": {},
        }

    def risk_envelope(self, spot_price: float, lot_size: int = 50) -> Dict[str, Any]:
        """₹ max-loss normalization; returns estimated: true (C4).

        Lot size is resolved from instrument master at call time — never stored
        in playbook (NSE revises lot sizes). V1 2%/1%/4% moneyness model,
        capped by max_loss_per_trade. V2 will use BS+margin behind same interface.
        """
        estimated_premium_pct = 0.02  # ATM
        if self.strike_selection == "otm":
            estimated_premium_pct = 0.01
        elif self.strike_selection == "itm":
            estimated_premium_pct = 0.04

        estimated_premium = spot_price * estimated_premium_pct
        max_loss = estimated_premium * self.quantity * lot_size
        if self.max_loss_per_trade is not None:
            max_loss = min(max_loss, self.max_loss_per_trade)

        return {
            "estimated_premium_per_unit": round(estimated_premium, 2),
            "max_loss_per_signal": round(max_loss, 2),
            "quantity": self.quantity,
            "lot_size": lot_size,
            "underlying": self.underlying,
            "spot": spot_price,
            "estimated": True,  # C4
        }
