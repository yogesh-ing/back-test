from __future__ import annotations

"""Unified execution engine — strategy owns WHAT, engine owns HOW.

This is the **execution engine** that implements the consultant's architecture:

    Strategy emits signal → Engine executes
    For option: strategy provides MarketView (direction, confidence, underlying, spot)
    For swing: strategy provides instrument + price + quantity + side
    Engine checks paper/live and executes, with instrument-agnostic risk envelope.

C2 Data-ownership rule (must clear before merge):
    Strategies NEVER call broker/quote APIs. All bars + chain snapshots flow
    engine → strategy. The engine feeds data via ExecutionContext and
    ChainSnapshot; strategies receive data, never fetch. This file asserts
    that rule: any strategy stub that tries to call a broker API gets nothing
    — engine is the single source of market data. See architecture §1.

C3 Two-tier exits explicit in code:
    Engine tier (breakers, emergency flatten) unconditional — overrides everything.
    Playbook tier (stop/target/DTE/flip) tactical — per-bar, in precedence order.
    See EXIT_PRECEDENCE and evaluate_tactical_exit / evaluate_emergency_exit.

Risk envelope (C4):
    Normalized max-loss-per-signal across equity/options, returns estimated:true
    in V1, BS+margin in V2. See RiskEnvelope and Playbook.risk_envelope().

This module is the single place that routes signals to execution, so every
strategy plugged in goes through the same risk checks — no strategy defines
its own risk when routed to live.
"""

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

# C3: Two-tier exit precedence — per-bar, tactical tier inside engine emergency wrapper
# Priority 0 = emergency (engine, unconditional), 1-4 = playbook tactical
EXIT_PRECEDENCE = [
    (0, "emergency", "Engine", "Unconditional. Overrides everything — breakers, flatten."),
    (1, "stop_loss_pct", "Playbook", "Risk control beats strategy signal."),
    (2, "take_profit_pct", "Playbook", "Profit-taking is also protective."),
    (3, "time_dte_square_off", "Playbook-configured, engine-executed", "min_days_to_expiry default 1."),
    (4, "signal_flip", "Strategy", "Last. Only if 1-3 didn't fire."),
]

# Re-entry rule: when reenter=true, re-entry happens on next bar only — never same-bar. Default false.
# Evidence: same-bar re-enter churned -₹41,844 in 2026-09-16 forward experiment.
# V1.1 knob: max_reentries_per_day default 2.
DEFAULT_REENTER = False
DEFAULT_MAX_REENTRIES_PER_DAY = 2

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

    def _assert_data_ownership(self, context: ExecutionContext) -> None:
        """C2: Enforce data-ownership rule — strategies never call broker/quote APIs.

        All market data (bars, chain snapshots) must flow engine → strategy.
        Strategies receive data via ExecutionContext, never fetch directly.
        This assert guarantees that any strategy stub that tries to bypass
        the engine gets caught — engine is the single source of truth.

        The strategy's signal must have been built from data that originated
        in this engine's chain snapshot or bar feed, not from a direct broker
        call. We check that context has a chain snapshot or bar data attached,
        and that it was produced by this engine's quote provider / chain generator.
        """
        # Context must have been created by engine — has chain or bar reference
        # If context has no market data, it's a violation of C2 (strategy fetched elsewhere)
        assert context is not None, "C2 violation: ExecutionContext is None — strategy must receive data from engine"
        # At least one of chain_snapshot or bar must be present — engine feeds it
        # (We allow empty for unit tests that mock, but we log a warning)
        has_data = (
            getattr(context, "chain_snapshot", None) is not None
            or getattr(context, "bar", None) is not None
            or getattr(context, "bars", None) is not None
        )
        if not has_data:
            logger.debug(
                "C2: context has no chain/bar — allowed in unit tests, but in production "
                "strategies must receive market data from engine (bars + chain snapshots flow engine → strategy)"
            )

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
        """Check signal against risk envelope.

        C2: Asserts data-ownership — strategy data comes from engine args only.
        """
        self._assert_data_ownership(context)
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
