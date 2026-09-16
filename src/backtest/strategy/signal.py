"""Unified signal contract — strategy owns WHAT, engine owns HOW.

This is the **correct architecture** the consultant identified:

    Strategy emits signal → Engine executes
    For option: strategy provides MarketView (direction, confidence)
    For swing: strategy provides instrument + price + sizing
    Execution engine checks paper/live and executes.

Missing pieces added:

1. **Data ownership**: Engine feeds bars + chain snapshots; strategies never
   touch broker APIs. ChainSnapshot is provided by the engine.
2. **Exit ownership — two tiers**:
   - Tactical exits: strategy owns (must be woken per bar, e.g. signal flip)
   - Emergency exits: engine owns unconditionally (breakers, flatten, DTE)
3. **Instrument-agnostic risk envelope**: Swing risk = ₹ stop distance;
   option risk = premium × lots × lot_size with SPAN margin. Router normalizes
   max-loss-per-signal and exposure caps across both.

Usage::

    from backtest.strategy.signal import (
        UnifiedSignal, SignalType, ExecutionContext,
        ChainSnapshot, RiskEnvelope,
    )

    # Strategy side: emit a signal (WHAT)
    signal = UnifiedSignal.option_view(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
        metadata={"rsi": 28.5},
    )

    # Engine side: execute it (HOW)
    context = ExecutionContext(
        mode="paper",
        chain=chain_snapshot,  # from engine's chain feed
        risk_envelope=RiskEnvelope(max_loss_per_signal=5000),
    )
    trade_intent = engine.execute_signal(signal, context)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from backtest.strategy.intent import Direction, MarketView

logger = logging.getLogger("backtest.strategy.signal")


# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------

class SignalType(str, Enum):
    """What kind of signal the strategy emitted."""

    EQUITY_ENTRY = "equity_entry"  # swing: buy/sell equity
    EQUITY_EXIT = "equity_exit"    # swing: exit equity
    OPTION_VIEW = "option_view"    # option: directional view → expression layer builds structure
    OPTION_DIRECT = "option_direct"  # option: strategy directly names strikes/legs (advanced)


# ---------------------------------------------------------------------------
# Chain snapshot — data ownership (engine provides, strategy consumes)
# ---------------------------------------------------------------------------

@dataclass
class ChainSnapshot:
    """A snapshot of the option chain for one underlying/expiry.

    **Data ownership**: The engine owns the chain feed (mStock API or
    synthetic generator). Strategies NEVER call broker APIs directly.
    The engine provides this snapshot to the strategy via ExecutionContext.

    This is the missing piece that makes plug-and-play actually work —
    otherwise it's plug-and-pray (strategy names strikes that don't exist).

    Parameters
    ----------
    underlying:
        Index (NIFTY, BANKNIFTY)
    expiry:
        Expiry date
    spot_price:
        Current underlying spot
    strikes:
        Sorted list of available strikes
    chain:
        Full chain: strike → contract details (LTP, bid/ask, IV, Greeks)
    timestamp:
        When this snapshot was taken
    """

    underlying: str
    expiry: date
    spot_price: Decimal
    strikes: List[Decimal]
    chain: Dict[Decimal, Dict[str, Any]] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str = "synthetic"  # synthetic | live:mstock

    def has_strike(self, strike: Decimal) -> bool:
        return strike in self.strikes or any(
            abs(float(s) - float(strike)) < 0.01 for s in self.strikes
        )

    def nearest_strike(self, target: Decimal) -> Optional[Decimal]:
        """Find nearest available strike to target."""
        if not self.strikes:
            return None
        return min(self.strikes, key=lambda s: abs(float(s) - float(target)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "spot_price": float(self.spot_price),
            "strikes": [float(s) for s in self.strikes],
            "chain_size": len(self.chain),
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Risk envelope — instrument-agnostic risk normalization
# ---------------------------------------------------------------------------

@dataclass
class RiskEnvelope:
    """Normalized risk limits per signal — works for equity AND options.

    **Instrument-agnostic risk envelope**: The router must normalize
    max-loss-per-signal and exposure caps across both instrument types,
    or every strategy plugged in defines its own risk — unacceptable when
    the engine routes to live.

    Equity: risk = stop distance × quantity
    Options: risk = premium × lots × lot_size + SPAN margin (for shorts)

    Parameters
    ----------
    max_loss_per_signal:
        Max loss in ₹ for this signal (e.g. 5000)
    max_exposure_pct:
        Max % of capital to risk on this signal (e.g. 0.02 = 2%)
    max_lots:
        Max lots per leg (options) or max quantity (equity)
    max_positions:
        Max open positions for this strategy
    """

    max_loss_per_signal: Optional[float] = None  # ₹
    max_exposure_pct: Optional[float] = None  # 0.02 = 2%
    max_lots: Optional[int] = None
    max_positions: Optional[int] = None
    allowed_structures: Optional[List[str]] = None

    def check_option_signal(
        self,
        estimated_premium: float,
        quantity: int,
        lot_size: int,
        current_positions: int = 0,
    ) -> tuple[bool, str]:
        """Check if an option signal fits the envelope."""
        # Position cap
        if self.max_positions is not None and current_positions >= self.max_positions:
            return False, f"max positions {self.max_positions} reached"

        # Lots cap
        if self.max_lots is not None and quantity > self.max_lots:
            return False, f"quantity {quantity} > max lots {self.max_lots}"

        # Loss cap — premium × lots × lot_size is max loss for debit structures
        if self.max_loss_per_signal is not None:
            max_loss = estimated_premium * quantity * lot_size
            if max_loss > self.max_loss_per_signal:
                return False, (
                    f"estimated max loss ₹{max_loss:,.0f} > "
                    f"limit ₹{self.max_loss_per_signal:,.0f} "
                    f"(premium ₹{estimated_premium:,.0f} × {quantity} lots × {lot_size})"
                )

        # Structure allowlist
        # (checked by caller — needs structure_type)

        return True, "ok"

    def check_equity_signal(
        self,
        quantity: float,
        price: float,
        stop_distance: Optional[float] = None,
        current_positions: int = 0,
    ) -> tuple[bool, str]:
        """Check if an equity signal fits the envelope."""
        if self.max_positions is not None and current_positions >= self.max_positions:
            return False, f"max positions {self.max_positions} reached"

        if self.max_lots is not None and quantity > self.max_lots:
            return False, f"quantity {quantity} > max {self.max_lots}"

        if self.max_loss_per_signal is not None and stop_distance is not None:
            max_loss = stop_distance * quantity
            if max_loss > self.max_loss_per_signal:
                return False, (
                    f"estimated max loss ₹{max_loss:,.0f} > "
                    f"limit ₹{self.max_loss_per_signal:,.0f}"
                )

        return True, "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_loss_per_signal": self.max_loss_per_signal,
            "max_exposure_pct": self.max_exposure_pct,
            "max_lots": self.max_lots,
            "max_positions": self.max_positions,
            "allowed_structures": self.allowed_structures,
        }


# ---------------------------------------------------------------------------
# Execution context — what the engine provides to execute a signal
# ---------------------------------------------------------------------------

@dataclass
class ExecutionContext:
    """Everything the engine provides to execute a signal (HOW).

    **Data ownership**: Engine owns bars + chain + quotes; strategy never
    touches broker APIs.

    Parameters
    ----------
    mode:
        paper or live — determines routing
    source:
        Data source tag (synthetic, mstock, etc.)
    chain:
        Option chain snapshot (for option signals)
    risk_envelope:
        Normalized risk limits
    current_positions:
        Number of open positions (for cap checks)
    spot_price:
        Current spot (for risk calculations)
    bar_timestamp:
        Bar that triggered the signal
    """

    mode: str = "paper"  # paper | live
    source: str = "synthetic"
    chain: Optional[ChainSnapshot] = None
    risk_envelope: Optional[RiskEnvelope] = None
    current_positions: int = 0
    spot_price: Optional[Decimal] = None
    bar_timestamp: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "source": self.source,
            "chain": self.chain.to_dict() if self.chain else None,
            "risk_envelope": self.risk_envelope.to_dict() if self.risk_envelope else None,
            "current_positions": self.current_positions,
            "spot_price": float(self.spot_price) if self.spot_price else None,
            "bar_timestamp": str(self.bar_timestamp) if self.bar_timestamp else None,
        }


# ---------------------------------------------------------------------------
# Exit ownership — two tiers
# ---------------------------------------------------------------------------

class ExitTier(str, Enum):
    """Who owns the exit decision."""

    TACTICAL = "tactical"  # Strategy owns — must be woken per bar (signal flip, neutral, etc.)
    EMERGENCY = "emergency"  # Engine owns unconditionally — breakers, flatten, DTE, stop/target


@dataclass
class ExitSignal:
    """An exit decision — who owns it and why.

    **Exit ownership — two tiers**:
    - Tactical: strategy owns (must be woken per bar). E.g. signal flip, neutral bars.
    - Emergency: engine owns unconditionally. E.g. breakers, flatten, DTE, stop loss.

    Today's churn disaster came from exit policy, not entry logic — so we make
    ownership explicit.
    """

    tier: ExitTier
    reason: str  # stop_loss, take_profit, signal_flip, signal_neutral, time_stop, auto_square_off, manual, circuit_breaker, emergency_flatten
    detail: str = ""
    structure_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def is_emergency(self) -> bool:
        return self.tier == ExitTier.EMERGENCY

    def is_tactical(self) -> bool:
        return self.tier == ExitTier.TACTICAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "reason": self.reason,
            "detail": self.detail,
            "structure_id": self.structure_id,
            "timestamp": self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# Unified signal — the contract
# ---------------------------------------------------------------------------

@dataclass
class UnifiedSignal:
    """A strategy's signal — the WHAT, not the HOW.

    This is the **unified signal contract**: strategy emits signal,
    execution engine executes it. Works for both equity and options.

    For options:
    - Strategy provides MarketView (direction, confidence, underlying, spot)
    - Engine's expression layer provides strikes/legs via selector + chain
    - Advanced: strategy can directly provide strikes (OPTION_DIRECT)

    For equity (swing):
    - Strategy provides instrument + price + quantity + side
    - Engine handles sizing + paper/live routing

    Parameters
    ----------
    signal_type:
        What kind of signal (equity_entry, equity_exit, option_view, option_direct)
    direction:
        BULLISH, BEARISH, NEUTRAL
    confidence:
        0.0-1.0 conviction
    underlying:
        Symbol (NIFTY for options, RELIANCE for equity)
    spot_price:
        Current spot/underlying price
    strike_info:
        For OPTION_DIRECT: explicit strikes/legs the strategy wants
    equity_info:
        For EQUITY_ENTRY/EXIT: instrument, quantity, price, side
    market_view:
        For OPTION_VIEW: the MarketView that triggers expression layer
    metadata:
        Arbitrary extra data (RSI value, SMA gap, etc.)
    timestamp:
        Bar timestamp that produced this signal
    strategy_name:
        Which strategy emitted it
    """

    signal_type: SignalType
    direction: Any  # Direction enum
    confidence: float = 0.5
    underlying: str = "NIFTY"
    spot_price: Decimal = Decimal("0")
    strike_info: Optional[Dict[str, Any]] = None
    equity_info: Optional[Dict[str, Any]] = None
    market_view: Optional[MarketView] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[Any] = None
    strategy_name: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}")

    @classmethod
    def option_view(
        cls,
        direction: Any,
        confidence: float = 0.8,
        underlying: str = "NIFTY",
        spot_price: Decimal | float = Decimal("0"),
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        strategy_name: str = "",
    ) -> "UnifiedSignal":
        """Create an option view signal — strategy says WHAT direction, engine builds HOW."""
        from backtest.strategy.intent import MarketView

        spot = Decimal(str(spot_price)) if not isinstance(spot_price, Decimal) else spot_price
        mv = MarketView(
            direction=direction,
            confidence=confidence,
            underlying=underlying,
            spot_price=spot,
            bar_timestamp=timestamp,
            metadata=metadata or {},
        )
        return cls(
            signal_type=SignalType.OPTION_VIEW,
            direction=direction,
            confidence=confidence,
            underlying=underlying,
            spot_price=spot,
            market_view=mv,
            metadata=metadata or {},
            timestamp=timestamp,
            strategy_name=strategy_name,
        )

    @classmethod
    def option_direct(
        cls,
        direction: Any,
        strikes: List[Decimal],
        structure_type: str,
        expiry: date,
        underlying: str = "NIFTY",
        spot_price: Decimal | float = Decimal("0"),
        quantity: int = 1,
        confidence: float = 0.8,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        strategy_name: str = "",
    ) -> "UnifiedSignal":
        """Create a direct option signal — strategy names strikes explicitly (advanced).

        Use this when the strategy has its own chain analysis and wants to
        specify exact strikes. The engine still validates against ChainSnapshot
        and enforces risk envelope.
        """
        spot = Decimal(str(spot_price)) if not isinstance(spot_price, Decimal) else spot_price
        return cls(
            signal_type=SignalType.OPTION_DIRECT,
            direction=direction,
            confidence=confidence,
            underlying=underlying,
            spot_price=spot,
            strike_info={
                "strikes": strikes,
                "structure_type": structure_type,
                "expiry": expiry,
                "quantity": quantity,
            },
            metadata=metadata or {},
            timestamp=timestamp,
            strategy_name=strategy_name,
        )

    @classmethod
    def equity_entry(
        cls,
        underlying: str,
        side: str,  # BUY or SELL
        quantity: float,
        price: float,
        direction: Optional[Any] = None,
        confidence: float = 0.8,
        stop_price: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        strategy_name: str = "",
    ) -> "UnifiedSignal":
        """Create an equity entry signal."""
        from backtest.strategy.intent import Direction as Dir

        dir_val = direction
        if dir_val is None:
            dir_val = Dir.BULLISH if side.upper() == "BUY" else Dir.BEARISH

        spot = Decimal(str(price))
        return cls(
            signal_type=SignalType.EQUITY_ENTRY,
            direction=dir_val,
            confidence=confidence,
            underlying=underlying,
            spot_price=spot,
            equity_info={
                "side": side.upper(),
                "quantity": quantity,
                "price": price,
                "stop_price": stop_price,
            },
            metadata=metadata or {},
            timestamp=timestamp,
            strategy_name=strategy_name,
        )

    @classmethod
    def equity_exit(
        cls,
        underlying: str,
        reason: str = "signal",
        confidence: float = 0.8,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        strategy_name: str = "",
    ) -> "UnifiedSignal":
        """Create an equity exit signal."""
        from backtest.strategy.intent import Direction as Dir

        return cls(
            signal_type=SignalType.EQUITY_EXIT,
            direction=Dir.NEUTRAL,
            confidence=confidence,
            underlying=underlying,
            spot_price=Decimal("0"),
            equity_info={"reason": reason},
            metadata=metadata or {},
            timestamp=timestamp,
            strategy_name=strategy_name,
        )

    def is_option(self) -> bool:
        return self.signal_type in (SignalType.OPTION_VIEW, SignalType.OPTION_DIRECT)

    def is_equity(self) -> bool:
        return self.signal_type in (SignalType.EQUITY_ENTRY, SignalType.EQUITY_EXIT)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_type": self.signal_type.value,
            "direction": str(getattr(self.direction, "value", self.direction)),
            "confidence": self.confidence,
            "underlying": self.underlying,
            "spot_price": float(self.spot_price),
            "strike_info": self.strike_info,
            "equity_info": self.equity_info,
            "metadata": self.metadata,
            "timestamp": str(self.timestamp) if self.timestamp else None,
            "strategy_name": self.strategy_name,
        }


# ---------------------------------------------------------------------------
# Execution router — the HOW (instrument-agnostic)
# ---------------------------------------------------------------------------

class SignalRouter:
    """Routes UnifiedSignal to the right execution path — paper/live, equity/option.

    This is the **execution engine** the consultant described:
    - Strategy owns WHAT (signal)
    - Engine owns HOW + paper/live routing
    - Risk envelope normalized across instrument types

    Parameters
    ----------
    portfolio_manager:
        The portfolio manager that owns runners and buckets
    risk_envelope:
        Default risk envelope (can be overridden per signal)
    """

    def __init__(
        self,
        portfolio_manager: Optional[Any] = None,
        risk_envelope: Optional[RiskEnvelope] = None,
    ) -> None:
        self.portfolio_manager = portfolio_manager
        self.risk_envelope = risk_envelope or RiskEnvelope(
            max_loss_per_signal=10000,
            max_exposure_pct=0.02,
            max_lots=10,
            max_positions=5,
        )

    def validate_signal(
        self,
        signal: UnifiedSignal,
        context: ExecutionContext,
    ) -> tuple[bool, str]:
        """Validate a signal against chain + risk envelope.

        Returns (allowed, reason).
        """
        envelope = context.risk_envelope or self.risk_envelope

        if signal.is_option():
            # Data ownership check: does the strike exist in chain?
            if signal.signal_type == SignalType.OPTION_DIRECT and context.chain:
                strikes = signal.strike_info.get("strikes", []) if signal.strike_info else []
                for strike in strikes:
                    if not context.chain.has_strike(strike):
                        nearest = context.chain.nearest_strike(strike)
                        return False, (
                            f"strike {strike} not in chain for {signal.underlying} "
                            f"(nearest: {nearest}, available: {len(context.chain.strikes)} strikes)"
                        )

            # Risk envelope check
            if envelope:
                # Estimate premium — in production, use Black-Scholes with IV from chain
                estimated_premium = float(signal.spot_price) * 0.02  # 2% ATM estimate
                qty = 1
                if signal.strike_info:
                    qty = signal.strike_info.get("quantity", 1)
                # Try to get quantity from context/metadata
                if context.metadata.get("quantity"):
                    qty = context.metadata["quantity"]

                allowed, reason = envelope.check_option_signal(
                    estimated_premium=estimated_premium,
                    quantity=qty,
                    lot_size=50,  # NIFTY lot size — should come from instrument registry
                    current_positions=context.current_positions,
                )
                if not allowed:
                    return False, reason

        elif signal.is_equity():
            if envelope and signal.equity_info:
                qty = signal.equity_info.get("quantity", 0)
                price = signal.equity_info.get("price", 0)
                stop = signal.equity_info.get("stop_price")
                stop_dist = abs(price - stop) if stop else None

                allowed, reason = envelope.check_equity_signal(
                    quantity=qty,
                    price=price,
                    stop_distance=stop_dist,
                    current_positions=context.current_positions,
                )
                if not allowed:
                    return False, reason

        return True, "ok"

    def route(
        self,
        signal: UnifiedSignal,
        context: ExecutionContext,
    ) -> Dict[str, Any]:
        """Route a signal — validate, then return execution plan.

        Does NOT execute — returns what WOULD be executed, so callers can
        audit/log before committing. Execution happens via the runner's
        OptionsBridge or PaperBroker.

        Returns
        -------
        {
            "allowed": bool,
            "reason": str,
            "execution_plan": {
                "mode": "paper" | "live",
                "instrument_type": "equity" | "option",
                "action": "open" | "close",
                "details": {...}
            }
        }
        """
        allowed, reason = self.validate_signal(signal, context)

        if not allowed:
            return {
                "allowed": False,
                "reason": reason,
                "execution_plan": None,
            }

        # Build execution plan
        if signal.is_option():
            plan = {
                "mode": context.mode,
                "instrument_type": "option",
                "action": "open",
                "underlying": signal.underlying,
                "direction": str(getattr(signal.direction, "value", signal.direction)),
                "confidence": signal.confidence,
                "chain_source": context.chain.source if context.chain else "unknown",
                "risk_envelope": (context.risk_envelope or self.risk_envelope).to_dict(),
            }
            if signal.signal_type == SignalType.OPTION_DIRECT:
                plan["strikes"] = signal.strike_info
            else:
                plan["market_view"] = signal.market_view.to_dict() if hasattr(signal.market_view, "to_dict") else str(signal.market_view)

        else:
            plan = {
                "mode": context.mode,
                "instrument_type": "equity",
                "action": "open" if signal.signal_type == SignalType.EQUITY_ENTRY else "close",
                "underlying": signal.underlying,
                "equity_info": signal.equity_info,
                "risk_envelope": (context.risk_envelope or self.risk_envelope).to_dict(),
            }

        return {
            "allowed": True,
            "reason": "ok",
            "execution_plan": plan,
        }
