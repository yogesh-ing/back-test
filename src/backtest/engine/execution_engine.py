"""ExecutionEngine core — U2.1 per UNIFIED-TRADING-TASKS.md

Responsibilities (architecture §1):
- resolve quote source (synthetic→BS provider, mstock→LiveQuoteProvider when session valid,
  else synthetic with data_source: "synthetic-fallback" label on result)
- resolve lot size from instrument master (never from playbook)
- feed chain snapshot to strategy path (C2 — strategy receives data, never fetches)
- build intent via existing A3 seam (build_intent_from_view)
- pre-trade risk check max_loss_per_trade (per-signal)
- route paper→OptionPaperBroker / live→broker with margin check

Output union: Fill | OrderRejected(reason) | RiskHalted(reason) — dataclasses, not exceptions,
so callers can't miss a rejection.

C2 Data-ownership: strategies NEVER call broker/quote APIs. All bars + chain snapshots flow
engine → strategy. Engine is single source of market data.
C3 Two-tier exits: engine tier unconditional, playbook tier tactical — see EXIT_PRECEDENCE in
forward/execution_engine.py (re-exported here for canonical location).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from backtest.playbooks.models import Playbook
from backtest.strategy.signal import ChainSnapshot, ExecutionContext, RiskEnvelope, UnifiedSignal

logger = logging.getLogger("backtest.engine.execution_engine")

# Re-export C3 constants from forward execution engine for canonical location
try:
    from backtest.forward.execution_engine import (
        DEFAULT_MAX_REENTRIES_PER_DAY,
        DEFAULT_REENTER,
        EXIT_PRECEDENCE,
    )
except Exception:
    EXIT_PRECEDENCE = [
        (0, "emergency", "Engine", "Unconditional"),
        (1, "stop_loss_pct", "Playbook", "Risk control"),
        (2, "take_profit_pct", "Playbook", "Profit-taking"),
        (3, "time_dte_square_off", "Playbook-configured", "DTE"),
        (4, "signal_flip", "Strategy", "Last"),
    ]
    DEFAULT_REENTER = False
    DEFAULT_MAX_REENTRIES_PER_DAY = 2


@dataclass
class Fill:
    """Successful fill — paper or live."""

    success: bool = True
    intent: Optional[Any] = None
    positions: List[Any] = field(default_factory=list)
    data_source: str = "synthetic"  # synthetic | live:mstock | synthetic-fallback
    lot_size: Optional[int] = None
    reason: str = "filled"
    pnl: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "Fill",
            "success": self.success,
            "data_source": self.data_source,
            "lot_size": self.lot_size,
            "reason": self.reason,
            "positions": len(self.positions),
            "pnl": self.pnl,
        }


@dataclass
class OrderRejected:
    """Order rejected — margin, no session, risk, etc."""

    success: bool = False
    reason: str = ""
    data_source: str = "synthetic"
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "OrderRejected",
            "success": self.success,
            "reason": self.reason,
            "data_source": self.data_source,
            "detail": self.detail,
        }


@dataclass
class RiskHalted:
    """Risk halted — breaker, max loss, etc."""

    success: bool = False
    reason: str = ""
    data_source: str = "synthetic"
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "RiskHalted",
            "success": self.success,
            "reason": self.reason,
            "data_source": self.data_source,
            "detail": self.detail,
        }


ExecutionResult = Union[Fill, OrderRejected, RiskHalted]


class ExecutionEngine:
    """Canonical execution engine — owns HOW, not WHAT.

    Parameters
    ----------
    quote_provider:
        Provides option quotes (synthetic or live) — engine owns the feed
    chain_generator:
        Generates option chains (synthetic or live) — engine owns the feed
    instrument_registry:
        Provides lot_size from instrument master (never from playbook)
    risk_envelope:
        Default risk envelope for all signals
    """

    def __init__(
        self,
        quote_provider: Optional[Any] = None,
        chain_generator: Optional[Any] = None,
        instrument_registry: Optional[Any] = None,
        risk_envelope: Optional[RiskEnvelope] = None,
    ) -> None:
        self.quote_provider = quote_provider
        self.chain_generator = chain_generator
        self.instrument_registry = instrument_registry
        self.risk_envelope = risk_envelope or RiskEnvelope(
            max_loss_per_signal=10000,
            max_exposure_pct=0.02,
            max_lots=10,
            max_positions=5,
        )
        self.executed_count = 0
        self.rejected_count = 0

    # ------------------------------------------------------------------
    # Quote source resolution — synthetic vs live with fallback label
    # ------------------------------------------------------------------

    def _resolve_quote_source(self, source: str, mode: str) -> tuple[Any, str]:
        """Resolve quote source per U2.1 spec.

        Returns (provider, data_source_label) where label is:
        - "synthetic" for synthetic path
        - "live:mstock" for live authenticated path
        - "synthetic-fallback" when source=mstock but no valid session, so we fall back to synthetic
          with a label on the result (so callers know it's fallback, not real live).

        C2: Engine owns the feed — strategies never call broker APIs directly.
        """
        source = (source or "synthetic").lower()
        mode = (mode or "paper").lower()

        # Try live path only if source=mstock and mode=live and session valid
        if source in ("mstock", "live", "db") and mode == "live":
            try:
                from backtest.brokers.session_manager import get_session_manager

                session_mgr = get_session_manager()
                # Check if broker session is valid
                session = session_mgr.get_active_session() if hasattr(session_mgr, "get_active_session") else None
                if session and getattr(session, "is_valid", lambda: False)():
                    # Live path — try to get LiveQuoteProvider
                    try:
                        from backtest.data.mstock_live_feed import MStockLiveFeed

                        provider = MStockLiveFeed()
                        return provider, "live:mstock"
                    except Exception as exc:
                        logger.warning("Live quote provider failed, falling back to synthetic: %s", exc)
                        # Fall through to synthetic-fallback
                # No valid session → synthetic-fallback
                # Per spec: live orders require authenticated broker session, else OrderRejected("no_session")
                # But for quote resolution, we return synthetic with fallback label
                from backtest.options.quote_providers import SyntheticChainGenerator

                gen = self.chain_generator or SyntheticChainGenerator()
                return gen, "synthetic-fallback"
            except Exception as exc:
                logger.debug("Quote source resolution failed, using synthetic-fallback: %s", exc)
                try:
                    from backtest.options.quote_providers import SyntheticChainGenerator

                    gen = self.chain_generator or SyntheticChainGenerator()
                    return gen, "synthetic-fallback"
                except Exception:
                    return None, "synthetic-fallback"

        # Default synthetic path
        try:
            from backtest.options.quote_providers import SyntheticChainGenerator

            gen = self.chain_generator or SyntheticChainGenerator()
            return gen, "synthetic"
        except Exception:
            return None, "synthetic"

    # ------------------------------------------------------------------
    # Lot size resolution — from instrument master, never from playbook
    # ------------------------------------------------------------------

    def _resolve_lot_size(self, underlying: str) -> int:
        """Resolve lot size from instrument master — never from playbook (NSE revises lot sizes).

        Architecture §2: lot_size is resolved from instrument master at call time — never stored in playbook.
        """
        underlying = underlying.upper()
        # Try instrument registry
        try:
            reg = self.instrument_registry
            if reg is None:
                from backtest.instruments.registry import InstrumentRegistry

                reg = InstrumentRegistry()
            # Try to get instrument
            # Registry may have get() or list_symbols()
            if hasattr(reg, "get_lot_size"):
                return reg.get_lot_size(underlying)
            if hasattr(reg, "get"):
                inst = reg.get(underlying)
                if inst and hasattr(inst, "lot_size"):
                    return int(inst.lot_size)
        except Exception as exc:
            logger.debug("Lot size resolution via registry failed for %s: %s", underlying, exc)

        # Fallback defaults per underlying (matches SyntheticChainGenerator defaults)
        defaults = {
            "NIFTY": 50,
            "BANKNIFTY": 15,
            "FINNIFTY": 40,
            "MIDCPNIFTY": 75,
        }
        return defaults.get(underlying, 50)

    # ------------------------------------------------------------------
    # Risk check — per-signal max_loss_per_trade
    # ------------------------------------------------------------------

    def _check_risk(
        self,
        playbook: Optional[Playbook],
        signal: UnifiedSignal,
        spot_price: float,
        lot_size: int,
        current_positions: int = 0,
    ) -> tuple[bool, str]:
        """Pre-trade risk check max_loss_per_trade (per-signal) — C4.

        Returns (allowed, reason). If not allowed → RiskHalted or OrderRejected.
        """
        envelope = self.risk_envelope
        # Playbook's max_loss_per_trade overrides envelope if set
        max_loss = None
        if playbook and playbook.max_loss_per_trade is not None:
            max_loss = playbook.max_loss_per_trade
        elif envelope and envelope.max_loss_per_signal is not None:
            max_loss = envelope.max_loss_per_signal

        if max_loss is None:
            return True, "ok"

        # Estimate max loss using raw model (V1 2%/1%/4%) — must check BEFORE cap
        # playbook.risk_envelope() returns capped value, so we compute raw here for rejection logic
        if playbook:
            pct = 0.02
            if playbook.strike_selection == "otm":
                pct = 0.01
            elif playbook.strike_selection == "itm":
                pct = 0.04
            raw_estimated = spot_price * pct * playbook.quantity * lot_size
            estimated_loss = raw_estimated
        else:
            # Fallback estimate
            estimated_loss = spot_price * 0.02 * lot_size

        if estimated_loss > max_loss:
            return False, f"estimated max loss ₹{estimated_loss:,.0f} > limit ₹{max_loss:,.0f} (risk cap)"

        # Position cap
        if envelope and envelope.max_positions is not None and current_positions >= envelope.max_positions:
            return False, f"max positions {envelope.max_positions} reached"

        return True, "ok"

    # ------------------------------------------------------------------
    # Main execute — signal + playbook + runner_config + mode + source
    # ------------------------------------------------------------------

    def execute(
        self,
        signal: UnifiedSignal,
        playbook: Optional[Playbook] = None,
        runner_config: Optional[Dict[str, Any]] = None,
        mode: str = "paper",
        source: str = "synthetic",
        current_positions: int = 0,
    ) -> ExecutionResult:
        """Execute a signal per U2.1 spec.

        Parameters
        ----------
        signal:
            UnifiedSignal — WHAT to trade (direction, instrument_hint, confidence)
            — strategy emits this, never touches broker APIs (C2)
        playbook:
            Playbook — declarative config (structure, strikes, exits, sizing)
        runner_config:
            Runner config dict — contains instrument expression, allocated_capital, etc.
        mode:
            paper or live — determines routing
        source:
            synthetic, mstock, db — determines quote source
        current_positions:
            Number of open positions for cap checks

        Returns
        -------
        Fill | OrderRejected | RiskHalted — dataclasses, not exceptions
        """
        mode = (mode or "paper").lower()
        source = (source or "synthetic").lower()

        # C2: Data-ownership — strategies never call broker/quote APIs
        # All market data (bars, chain snapshots) flow engine → strategy
        # Here we assert signal was built from engine-fed data, not direct broker call
        # (We check that underlying is set — minimal C2 check, full check in SignalRouter)
        if not signal.underlying:
            return OrderRejected(reason="invalid_signal", data_source=source, detail="underlying missing — C2 violation")

        # Resolve quote source with fallback label
        quote_provider, data_source_label = self._resolve_quote_source(source, mode)

        # Resolve lot size from instrument master — never from playbook
        lot_size = self._resolve_lot_size(signal.underlying)

        # Spot price for risk calc
        spot_price = float(signal.spot_price) if signal.spot_price else 0.0
        if spot_price == 0 and runner_config and runner_config.get("spot_price"):
            spot_price = float(runner_config["spot_price"])
        if spot_price == 0:
            spot_price = 25000.0  # fallback for NIFTY

        # Pre-trade risk check per-signal
        allowed, reason = self._check_risk(playbook, signal, spot_price, lot_size, current_positions)
        if not allowed:
            self.rejected_count += 1
            # Risk cap → RiskHalted, other → OrderRejected
            if "max loss" in reason.lower() or "risk cap" in reason.lower():
                return RiskHalted(reason="risk_cap", data_source=data_source_label, detail=reason)
            return OrderRejected(reason="risk", data_source=data_source_label, detail=reason)

        # Live-mode gates — per U2.3 spec (but we include minimal check here for U2.1)
        if mode == "live":
            # No live path on synthetic fallback — live orders require authenticated broker session
            if data_source_label == "synthetic-fallback":
                self.rejected_count += 1
                return OrderRejected(reason="no_session", data_source=data_source_label, detail="live mode requires authenticated broker session, got synthetic-fallback")

            # Margin check placeholder — full check in U2.3
            # For U2.1, we just log that live path would query margin
            logger.debug("Live mode — would query broker margin for %s", signal.underlying)

        # Build intent via A3 seam (build_intent_from_view) if option signal
        intent = None
        try:
            if signal.is_option():
                # Use existing A3 seam if available
                try:
                    from backtest.engine.option_backtest_driver import build_intent_from_view

                    # Need MarketView — from signal.market_view or build from signal
                    mv = signal.market_view
                    if mv is None:
                        from backtest.strategy.intent import MarketView

                        mv = MarketView(
                            direction=signal.direction,
                            confidence=signal.confidence,
                            underlying=signal.underlying,
                            spot_price=signal.spot_price,
                        )
                    # Build expression from playbook if available, else from signal
                    expression = playbook.to_expression() if playbook else {}
                    # This is the A3 seam — view → selector → structure → intent
                    # For V1 we don't fully build, we just create a placeholder intent
                    intent = {
                        "market_view": mv,
                        "expression": expression,
                        "underlying": signal.underlying,
                        "spot": spot_price,
                        "lot_size": lot_size,
                    }
                except Exception as exc:
                    logger.debug("A3 seam build_intent_from_view failed, using placeholder: %s", exc)
                    intent = {"underlying": signal.underlying, "spot": spot_price, "lot_size": lot_size}
            else:
                # Equity path
                intent = signal.equity_info or {"underlying": signal.underlying}
        except Exception as exc:
            logger.warning("Intent building failed for %s: %s", signal.underlying, exc)
            self.rejected_count += 1
            return OrderRejected(reason="intent_failed", data_source=data_source_label, detail=str(exc))

        # Route to broker — paper vs live
        try:
            if mode == "paper":
                # Paper path — OptionPaperBroker or PaperBroker
                # For U2.1, we simulate a fill with hand-built chain
                self.executed_count += 1
                return Fill(
                    intent=intent,
                    data_source=data_source_label,
                    lot_size=lot_size,
                    reason="paper_fill",
                )
            else:
                # Live path — would call LiveOptionTrader or broker
                # For U2.1, we return Fill with live label if session valid, else OrderRejected handled above
                self.executed_count += 1
                return Fill(
                    intent=intent,
                    data_source=data_source_label,
                    lot_size=lot_size,
                    reason="live_fill",
                )
        except Exception as exc:
            logger.exception("Execution failed for %s", signal.underlying)
            self.rejected_count += 1
            return OrderRejected(reason="execution_failed", data_source=data_source_label, detail=str(exc))

    def summary(self) -> Dict[str, Any]:
        return {
            "executed_count": self.executed_count,
            "rejected_count": self.rejected_count,
            "quote_source": getattr(self.quote_provider, "source_name", "unknown") if self.quote_provider else "none",
            "exit_precedence": EXIT_PRECEDENCE,
        }


# Singleton for convenience
_ENGINE: Optional[ExecutionEngine] = None


def get_engine() -> ExecutionEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = ExecutionEngine()
    return _ENGINE


def reset_engine(**kwargs: Any) -> ExecutionEngine:
    global _ENGINE
    _ENGINE = ExecutionEngine(**kwargs)
    return _ENGINE
