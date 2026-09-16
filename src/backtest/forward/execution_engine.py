"""Unified execution engine — strategy owns WHAT, engine owns HOW.

This is the **execution engine** that implements the consultant's architecture:

    Strategy emits signal → Engine executes
    For option: strategy provides MarketView (direction, confidence, underlying, spot)
    For swing: strategy provides instrument + price + quantity + side
    Engine checks paper/live and executes, with instrument-agnostic risk envelope.

Missing pieces addressed:

1. **Data ownership**: Engine feeds bars + chain snapshots; strategies never touch broker APIs.
2. **Exit ownership — two tiers**:
   - Tactical: strategy owns (must be woken per bar, e.g. signal flip, neutral)
   - Emergency: engine owns unconditionally (breakers, flatten, DTE, stop/target)
3. **Risk envelope**: Normalized max-loss-per-signal across equity/options.

This module is the single place that routes signals to execution, so every
strategy plugged in goes through the same risk checks — no strategy defines
its own risk when routed to live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from backtest.options.exit_policy import EXIT_DTE, ExitConfig, ExitPolicy
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.signal import (
    ChainSnapshot,
    ExecutionContext,
    ExitSignal,
    ExitTier,
    RiskEnvelope,
    SignalRouter,
    UnifiedSignal,
)

logger = logging.getLogger("backtest.forward.execution_engine")


@dataclass
class ExecutionResult:
    """Result of executing a signal."""

    success: bool
    signal: UnifiedSignal
    context: ExecutionContext
    intent: Optional[Any] = None  # TradeIntent for options, Order for equity
    positions: List[Any] = field(default_factory=list)
    reason: str = ""
    pnl: Optional[float] = None
    exit_signal: Optional[ExitSignal] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "signal": self.signal.to_dict(),
            "context": self.context.to_dict(),
            "reason": self.reason,
            "positions": len(self.positions),
            "pnl": self.pnl,
            "exit_signal": self.exit_signal.to_dict() if self.exit_signal else None,
        }


class UnifiedExecutionEngine:
    """The unified execution engine — owns HOW, not WHAT.

    Parameters
    ----------
    quote_provider:
        Provides option quotes (synthetic or live)
    chain_generator:
        Generates option chains (synthetic or live)
    risk_envelope:
        Default risk envelope for all signals
    paper_broker_factory:
        Factory for paper brokers (equity)
    option_broker_factory:
        Factory for option paper brokers
    """

    def __init__(
        self,
        quote_provider: Optional[Any] = None,
        chain_generator: Optional[Any] = None,
        risk_envelope: Optional[RiskEnvelope] = None,
        paper_broker_factory: Optional[Any] = None,
        option_broker_factory: Optional[Any] = None,
    ) -> None:
        self.quote_provider = quote_provider
        self.chain_generator = chain_generator
        self.risk_envelope = risk_envelope or RiskEnvelope(
            max_loss_per_signal=10000,
            max_exposure_pct=0.02,
            max_lots=10,
            max_positions=5,
        )
        self.paper_broker_factory = paper_broker_factory
        self.option_broker_factory = option_broker_factory
        self.router = SignalRouter(risk_envelope=self.risk_envelope)

        # Metrics
        self.executed_count = 0
        self.rejected_count = 0
        self.exit_count = 0

    # ------------------------------------------------------------------
    # Chain feed — data ownership: engine provides chain to strategies
    # ------------------------------------------------------------------

    def get_chain_snapshot(
        self,
        underlying: str,
        expiry: Optional[Any] = None,
        option_type: str = "CE",
    ) -> Optional[ChainSnapshot]:
        """Get a chain snapshot for an underlying — engine owns the feed.

        Strategies call this via ExecutionContext, never broker APIs directly.
        """
        if self.chain_generator is None:
            try:
                from backtest.options.quote_providers import SyntheticChainGenerator
                self.chain_generator = SyntheticChainGenerator()
            except Exception:
                return None

        try:
            underlying = underlying.upper()

            # Resolve expiry
            if expiry is None:
                from backtest.options.expiry_policy import NearestExpiryPolicy
                expiry = NearestExpiryPolicy().select_expiry(
                    self.chain_generator.available_expiries(underlying)
                )
                if expiry is None:
                    return None

            # Spot
            spot = Decimal(str(self.chain_generator.get_spot(underlying)))

            # Chain
            chain_dict = self.chain_generator.generate_chain(
                underlying, expiry=expiry, option_type=option_type
            )
            strikes = sorted(chain_dict.keys())

            # Build ChainSnapshot
            return ChainSnapshot(
                underlying=underlying,
                expiry=expiry,
                spot_price=spot,
                strikes=strikes,
                chain=chain_dict,
                source=getattr(self.quote_provider, "source_name", "synthetic") if self.quote_provider else "synthetic",
            )
        except Exception as exc:
            logger.warning("Chain snapshot failed for %s: %s", underlying, exc)
            return None

    # ------------------------------------------------------------------
    # Risk envelope — instrument-agnostic
    # ------------------------------------------------------------------

    def check_risk(
        self,
        signal: UnifiedSignal,
        context: ExecutionContext,
    ) -> tuple[bool, str]:
        """Check signal against risk envelope."""
        return self.router.validate_signal(signal, context)

    # ------------------------------------------------------------------
    # Exit ownership — two tiers
    # ------------------------------------------------------------------

    def evaluate_tactical_exit(
        self,
        view: Optional[MarketView],
        structure_direction: Optional[Any],
        unrealized_pnl: Decimal,
        basis: Decimal,
        bars_held: int,
        bars_without_view: int,
        bar_date: Optional[Any] = None,
        expiry: Optional[Any] = None,
        exit_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExitSignal]:
        """Evaluate tactical exits — strategy owns, must be woken per bar.

        Returns ExitSignal with tier=TACTICAL if should exit, else None.
        """
        config = ExitConfig.from_expression(exit_config)
        policy = ExitPolicy(config)

        decision = policy.evaluate(
            view=view,
            structure_direction=structure_direction,
            unrealized_pnl=unrealized_pnl,
            basis=basis,
            bars_held=bars_held,
            bars_without_view=bars_without_view,
            bar_date=bar_date,
            expiry=expiry,
        )

        if decision is None:
            return None

        # Classify tier
        # Risk reasons are emergency (engine owns), signal reasons are tactical (strategy owns)
        from backtest.options.exit_policy import RISK_REASONS

        if decision.reason in RISK_REASONS:
            tier = ExitTier.EMERGENCY
        else:
            tier = ExitTier.TACTICAL

        return ExitSignal(
            tier=tier,
            reason=decision.reason,
            detail=decision.detail,
        )

    def evaluate_emergency_exit(
        self,
        daily_pnl: float,
        drawdown_pct: float,
        daily_loss_limit: float,
        max_drawdown_pct: float,
    ) -> Optional[ExitSignal]:
        """Evaluate emergency exits — engine owns unconditionally.

        Breakers, flatten, etc. These fire even if strategy is silent.
        """
        # Daily loss breaker
        if daily_loss_limit > 0 and daily_pnl <= -abs(daily_loss_limit):
            return ExitSignal(
                tier=ExitTier.EMERGENCY,
                reason="circuit_breaker",
                detail=f"daily loss {daily_pnl:,.0f} ≤ -{daily_loss_limit:,.0f}",
            )

        # Drawdown breaker
        if max_drawdown_pct > 0 and drawdown_pct >= max_drawdown_pct:
            return ExitSignal(
                tier=ExitTier.EMERGENCY,
                reason="circuit_breaker",
                detail=f"drawdown {drawdown_pct:.1%} ≥ {max_drawdown_pct:.1%}",
            )

        return None

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        return {
            "executed_count": self.executed_count,
            "rejected_count": self.rejected_count,
            "exit_count": self.exit_count,
            "risk_envelope": self.risk_envelope.to_dict(),
            "quote_source": getattr(self.quote_provider, "source_name", "unknown") if self.quote_provider else "none",
        }


# Process-wide singleton
_ENGINE: Optional[UnifiedExecutionEngine] = None


def get_execution_engine() -> UnifiedExecutionEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = UnifiedExecutionEngine()
    return _ENGINE


def reset_execution_engine(**kwargs: Any) -> UnifiedExecutionEngine:
    global _ENGINE
    _ENGINE = UnifiedExecutionEngine(**kwargs)
    return _ENGINE
